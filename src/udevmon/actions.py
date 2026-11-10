"""Run actions as subprocesses (never via a shell)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from . import notify
from . import pause as pause_flag
from . import session, state
from .config import Action, expand
from .events import Event

log = logging.getLogger("udevmon.actions")


def build_env(event: Event, device: str | None, tag: str | None = None,
              variables=None, state_dir: str | None = None) -> dict[str, str]:
    """The environment for an action: the daemon's own plus ``UDEVMON_EVENT``,
    ``UDEVMON_DEVICE``, ``UDEVMON_TAG``, ``UDEVMON_<PROPERTY>``, ``UDEVMON_VAR_<NAME>`` and
    ``UDEVMON_STATE_DIR``, each when it applies.
    """
    env = dict(os.environ)
    env["UDEVMON_EVENT"] = event.kind
    if device:
        env["UDEVMON_DEVICE"] = device
    if tag:
        env["UDEVMON_TAG"] = tag
    for k, v in event.endpoint.props.items():
        env[f"UDEVMON_{k.upper()}"] = v
    for k, v in (variables or {}).items():
        env[f"UDEVMON_VAR_{k.upper()}"] = v
    if state_dir:
        env["UDEVMON_STATE_DIR"] = state_dir
    return env


def run_action(action: Action, event: Event, device: str | None, tag: str | None = None, *,
               dry_run: bool = False, variables=None, state_dir: str | None = None,
               outcome: dict | None = None) -> int | None:
    """Run one action; returns exit status (None if skipped/timeout-killed).

    ``outcome``, if given, gets ``failure``: how a None result failed (a timeout, a
    command that could not start), for the notification; it is left alone otherwise.
    """
    outcome = {} if outcome is None else outcome
    props = dict(event.props)
    if state_dir:
        props["state_dir"] = state_dir
    props.update({f"var_{k}": v for k, v in (variables or {}).items()})
    if device:
        props["device"] = device
    if tag:
        props["tag"] = tag
    try:
        argv = expand(action.run, props)
    except KeyError as e:
        log.error("action %r skipped: placeholder(s) %s unavailable for event %r",
                  action.name, e.args[0], event.kind)
        return None
    if dry_run:
        log.info("[dry-run] action %r would run: %s", action.name, argv)
        return None
    log.info("action %r: %s %s", action.name, "starting (detached)" if action.detach
             else "running", argv)
    env = build_env(event, device, tag, variables, state_dir)
    session.update(env)                  # DISPLAY etc., which a service may not have had at start
    try:
        proc = subprocess.Popen(
            argv, cwd=os.path.expanduser("~"), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", start_new_session=True)
    except OSError as e:
        log.error("action %r failed to start: %s", action.name, e)
        outcome["failure"] = f"could not start: {e}"
        return None
    # The output is logged as it arrives, by a reader that outlives the command if it has to:
    # a program the command leaves running in the background keeps the pipe open, and
    # waiting for the pipe (instead of the command) made the action look stuck.
    reader = threading.Thread(target=_pump, args=(action, proc), daemon=True,
                              name=f"output-{action.name}")
    reader.start()
    if action.detach:
        threading.Thread(target=_reap, args=(action, proc), daemon=True,
                         name=f"reaper-{action.name}").start()
        return None
    try:
        proc.wait(timeout=action.timeout)
    except subprocess.TimeoutExpired:
        log.error("action %r timed out after %gs; terminating", action.name, action.timeout)
        outcome["failure"] = f"timed out after {action.timeout:g} s"
        _kill_group(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group(proc, signal.SIGKILL)
            proc.wait()
        reader.join(timeout=1)
        return None
    reader.join(timeout=_DRAIN_WAIT)      # normally the pipe is already closed; not for long
    _log_exit(action, proc.returncode)
    return proc.returncode


_DRAIN_WAIT = 0.2      # seconds to let the reader catch up after the command has exited


def _pump(action, proc) -> None:
    """Log the command's output line by line until its last writer closes the pipe."""
    try:
        for line in proc.stdout:
            log.info("[%s] %s", action.name, line.rstrip("\r\n"))
    except (OSError, ValueError):                 # the pipe went away under us
        pass
    finally:
        try:
            proc.stdout.close()
        except OSError:
            pass


def _reap(action, proc) -> None:
    """Log how a detached command ended."""
    _log_exit(action, proc.wait(), "detached ")


def _log_exit(action, status: int, kind: str = "") -> None:
    log.log(logging.INFO if status == 0 else logging.WARNING,
            "%saction %r exited with status %d", kind, action.name, status)


def _kill_group(proc, sig=signal.SIGTERM):
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


class FifoLock:
    """A mutex that hands the turn out in the order the tickets were taken.

    A ticket is taken when the event is submitted (on the daemon's single main thread, so in
    the order the events arrived) and used later, by whichever worker runs the job. That
    keeps the actions of a plug ahead of those of the following unplug even when the unplug
    reaches a free worker first, which a plain lock would not.
    """

    def __init__(self, name: str):
        self.name = name
        self._cond = threading.Condition()
        self._next = 0            # the next ticket to hand out
        self._serving = 0         # the ticket whose turn it is

    def take(self) -> int:
        """Take the next ticket: a place in the queue, for :meth:`wait`."""
        with self._cond:
            ticket = self._next
            self._next += 1
            return ticket

    def wait(self, ticket: int) -> bool:
        """Block until it is ``ticket``'s turn; True if it had to wait."""
        with self._cond:
            waited = self._serving != ticket
            while self._serving != ticket:
                self._cond.wait()
            return waited

    def release(self) -> None:
        """End the current turn and let the next ticket go."""
        with self._cond:
            self._serving += 1
            self._cond.notify_all()


class ActionRunner:
    """Runs the jobs of each event on a pool of ``max_workers`` threads.

    The jobs of one event run one after another in one worker; those of different events
    can overlap, unless their actions share a ``lock``. With ``dry_run`` the commands are
    logged, not run.
    """
    def __init__(self, max_workers: int = 4, dry_run: bool = False):
        self.dry_run = dry_run
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="action")
        self._locks: dict[str, FifoLock] = {}
        self._stopping = threading.Event()      # set by shutdown(): no more retries

    def _lock(self, name: str) -> FifoLock:
        return self._locks.setdefault(name, FifoLock(name))

    def submit(self, event: Event, jobs, ended: str | None = None):
        """Run the jobs (engine.Job) sequentially in one worker.

        Called in the order the events arrived: a job whose action has a ``lock`` takes its
        ticket now, so locked jobs run in that order and never at the same time. ``ended``
        is the instance whose presence this event ends: its state folder is removed once
        the jobs are done.
        """
        ticketed = [(job, self._lock(job.action.lock).take() if job.action.lock else None)
                    for job in jobs]
        return self._pool.submit(self._run_all, event, ticketed, ended)

    def _run_all(self, event, ticketed, ended=None):
        """Run the jobs in order; returns [(action name, exit status or None)]."""
        done = 0
        results = []
        try:
            for job, ticket in ticketed:
                done += 1
                results.append((job.action.name, self._run_one(event, job, ticket)))
        except BaseException:
            for job, ticket in ticketed[done:]:            # never leave a ticket unserved
                if ticket is not None:
                    lock = self._lock(job.action.lock)
                    lock.wait(ticket)
                    lock.release()
            raise
        finally:
            if ended:
                state.remove(ended)
        return results

    def _run_one(self, event, job, ticket):
        lock = self._lock(job.action.lock) if ticket is not None else None
        if lock is not None:
            if lock.wait(ticket):
                log.info("action %r waited for lock %r", job.action.name, lock.name)
        try:
            return self._run_with_retries(event, job)
        except Exception:
            log.exception("action %r crashed", job.action.name)
            return None
        finally:
            if lock is not None:
                lock.release()

    def _state_dir(self, event) -> str | None:
        """The state folder of the event's instance, created on first use (not for a dry run)."""
        instance = event.props.get("instance")
        if not instance or self.dry_run:
            return None
        try:
            return str(state.ensure(instance))
        except OSError as e:
            log.warning("cannot create the state folder for %s: %s", instance, e)
            return None

    def _run_with_retries(self, event, job):
        """Run the action (with its retries); with ``notify_on_failure``, tell the user
        when the last attempt failed (a non-zero exit, a timeout, a failure to start)."""
        outcome: dict = {}
        status = self._attempts(event, job, outcome)
        if job.action.notify_on_failure and not self.dry_run:
            what = outcome.get("failure") or (f"exit status {status}" if status else None)
            if what:
                if outcome.get("attempts", 1) > 1:
                    what += f" (after {outcome['attempts']} attempts)"
                notify.action_failed(job.action.name, job.device, what)
        return status

    def _attempts(self, event, job, outcome):
        """Run the action; after a non-zero exit run it again, up to ``retries`` more times.

        Not retried: a timeout, a command that cannot start, a skipped or dry run (they
        return None), and nothing once automation is paused or the daemon is stopping.
        The lock, if any, is held across the attempts.
        """
        action = job.action
        attempts = action.retries + 1
        for attempt in range(1, attempts + 1):
            outcome.clear()
            outcome["attempts"] = attempt
            status = run_action(action, event, job.device, job.tag, dry_run=self.dry_run,
                                variables=job.vars, state_dir=self._state_dir(event),
                                outcome=outcome)
            if status is None or status == 0:
                return status
            if attempt == attempts:
                if action.retries:
                    log.warning("action %r gave up after %d attempts (last status %d)",
                                action.name, attempts, status)
                return status
            if pause_flag.is_paused():
                log.warning("action %r failed with status %d; automation is paused, so not "
                            "retrying", action.name, status)
                return status
            log.warning("action %r attempt %d of %d failed with status %d; retrying in %g s",
                        action.name, attempt, attempts, status, action.retry_delay)
            if self._stopping.wait(action.retry_delay):
                log.warning("action %r: the daemon is stopping, so not retrying", action.name)
                return status
        return None

    def shutdown(self, wait=False):
        """Stop retrying and shut the pool down: with ``wait``, after the running and queued jobs;
        else queued jobs are cancelled.
        """
        self._stopping.set()
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
