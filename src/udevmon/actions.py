# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Run actions as subprocesses (never via a shell)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import collections
import itertools
import threading
import time
from concurrent.futures import Future
from typing import Callable, NamedTuple

from . import notify
from . import pause as pause_flag
from . import session, state
from .config import Action, expand
from .events import SHUTDOWN, Event

log = logging.getLogger("udevmon.actions")

OUTPUT_LINES = 10        #: lines of an action's output kept for its result (the last ones)
OUTPUT_WIDTH = 300       #: characters kept of each such line


class Result(NamedTuple):
    """How one job of an event ended (its last attempt)."""
    action: str                     #: the action's name
    status: int | None              #: the exit status; None when there is none
    failure: str | None = None      #: why there is no status (timed out, not started, ...)
    output: tuple[str, ...] = ()    #: the last lines of the output (``OUTPUT_LINES``)
    attempts: int = 1               #: how many times the command ran
    event: str = ""                 #: the event kind
    device: str | None = None       #: the device it ran for, if any
    time: float = 0.0               #: when it ended, in seconds since the epoch


def build_env(event: Event, device: str | None, tag: str | None = None,
              variables=None, state_dir: str | None = None,
              program: str | None = None) -> dict[str, str]:
    """The environment for an action: the daemon's own plus ``UDEVMON_EVENT``,
    ``UDEVMON_DEVICE``, ``UDEVMON_TAG``, ``UDEVMON_<PROPERTY>``, ``UDEVMON_VAR_<NAME>``,
    ``UDEVMON_STATE_DIR`` and ``UDEVMON_STATE_ROOT`` (its parent, shared by every
    instance), and ``UDEVMON_SCRIPT_DIR``, each when it applies.

    ``UDEVMON_SCRIPT_DIR`` is the folder of ``program`` (the command's ``argv[0]``) when
    that is a path; a relative one counts from the home folder, where actions run. A bare
    name left to ``PATH`` gives none.
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
        env["UDEVMON_STATE_ROOT"] = os.path.dirname(state_dir.rstrip("/"))
    if program and "/" in program:
        path = os.path.join(os.path.expanduser("~"), os.path.expanduser(program))
        env["UDEVMON_SCRIPT_DIR"] = os.path.dirname(os.path.abspath(path))
    return env


def run_action(action: Action, event: Event, device: str | None, tag: str | None = None, *,
               dry_run: bool = False, variables=None, state_dir: str | None = None,
               outcome: dict | None = None) -> int | None:
    """Run one action; returns exit status (None if skipped/timeout-killed).

    ``outcome``, if given, gets ``failure``: how a None result failed (a timeout, a
    command that could not start), for the notification; ``skipped``: why it did not
    run (an unavailable placeholder); and ``output``: the last lines of the command's
    output (``OUTPUT_LINES``, each cut to ``OUTPUT_WIDTH``), not for a detached one.
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
        outcome["skipped"] = f"skipped: placeholder(s) {e.args[0]} unavailable"
        return None
    if dry_run:
        log.info("[dry-run] action %r would run: %s", action.name, argv)
        return None
    log.info("action %r: %s %s", action.name, "starting (detached)" if action.detach
             else "running", argv)
    env = build_env(event, device, tag, variables, state_dir, program=argv[0])
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
    tail: collections.deque = collections.deque(maxlen=OUTPUT_LINES)
    reader = threading.Thread(target=_pump, args=(action, proc, tail), daemon=True,
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
        outcome["output"] = tuple(tail)
        return None
    reader.join(timeout=_DRAIN_WAIT)      # normally the pipe is already closed; not for long
    outcome["output"] = tuple(tail)
    _log_exit(action, proc.returncode)
    return proc.returncode


_DRAIN_WAIT = 0.2      # seconds to let the reader catch up after the command has exited


def _pump(action, proc, tail=None) -> None:
    """Log the command's output line by line until its last writer closes the pipe, keeping
    the last lines in ``tail`` (a bounded deque)."""
    try:
        for line in proc.stdout:
            line = line.rstrip("\r\n")
            log.info("[%s] %s", action.name, line)
            if tail is not None:
                tail.append(line[:OUTPUT_WIDTH])
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
    """Runs the jobs of each event in a thread of its own.

    The jobs of one event run one after another; those of different events can overlap,
    unless their actions share a ``lock``. At most ``max_workers`` commands run at once:
    a slot is taken only around each attempt's command, never while a job waits for its
    lock or between retries, so a waiting job holds up no other event. With ``dry_run``
    the commands are logged, not run.
    """
    def __init__(self, max_workers: int = 4, dry_run: bool = False,
                 on_result: Callable[[Result], None] | None = None):
        self.dry_run = dry_run
        self.on_result = on_result              # called with each job's Result
        self._slots = threading.BoundedSemaphore(max_workers)
        self._locks: dict[str, FifoLock] = {}
        self._threads: set[threading.Thread] = set()
        self._threads_mutex = threading.Lock()
        self._numbers = itertools.count(1)
        self._closed = False
        self._stopping = threading.Event()      # set by shutdown(): no more retries
        self._cancelled = threading.Event()     # shutdown(wait=False): start no more commands

    def _lock(self, name: str) -> FifoLock:
        return self._locks.setdefault(name, FifoLock(name))

    def submit(self, event: Event, jobs, ended: str | None = None):
        """Run the jobs (engine.Job) sequentially in one worker.

        Called in the order the events arrived: a job whose action has a ``lock`` takes its
        ticket now, so locked jobs run in that order and never at the same time. ``ended``
        is the instance whose presence this event ends: its state folder is removed once
        the jobs are done. Returns a Future of the jobs' :class:`Result`, in order.
        """
        if self._closed:
            raise RuntimeError("cannot run actions after shutdown")
        ticketed = [(job, self._lock(job.action.lock).take() if job.action.lock else None)
                    for job in jobs]
        future: Future = Future()
        future.set_running_or_notify_cancel()
        thread = threading.Thread(target=self._thread, args=(future, event, ticketed, ended),
                                  name=f"event-{next(self._numbers)}")
        with self._threads_mutex:
            self._threads.add(thread)
        thread.start()
        return future

    def _thread(self, future, event, ticketed, ended):
        try:
            future.set_result(self._run_all(event, ticketed, ended))
        except BaseException as e:                      # a crash is logged by _run_one
            future.set_exception(e)
        finally:
            with self._threads_mutex:
                self._threads.discard(threading.current_thread())

    def _run_all(self, event, ticketed, ended=None):
        """Run the jobs in order; returns their :class:`Result`."""
        done = 0
        results = []
        try:
            for job, ticket in ticketed:
                done += 1
                result = self._run_one(event, job, ticket)
                results.append(result)
                if self.on_result is not None:
                    try:
                        self.on_result(result)
                    except Exception:
                        log.exception("recording the result of %r failed", result.action)
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
        except Exception as e:
            log.exception("action %r crashed", job.action.name)
            return Result(job.action.name, None, f"crashed: {e}", event=event.kind,
                          device=job.device, time=time.time())
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
        failure = outcome.get("failure") or outcome.get("skipped")
        if status is None and failure is None:
            failure = "dry run" if self.dry_run else ("detached" if job.action.detach
                                                      else None)
        return Result(job.action.name, status, failure, outcome.get("output", ()),
                      outcome.get("attempts", 1), event.kind, job.device, time.time())

    def _attempts(self, event, job, outcome):
        """Run the action; after a non-zero exit run it again, up to ``retries`` more times.

        Each wait is ``retry_backoff`` times the one before, starting at ``retry_delay``.
        Not retried: a timeout, a command that cannot start, a skipped or dry run (they
        return None), and nothing once automation is paused. A stopping daemon ends the
        retries, except those of a ``shutdown`` event, which go on during its exit pass.
        The lock, if any, is held across the attempts; a command slot only while each
        attempt's command runs.
        """
        action = job.action
        attempts = action.retries + 1
        delay = action.retry_delay
        # the wait that a stop interrupts: a shutdown event's retries are cut short only
        # by shutdown(wait=False)
        interrupt = self._cancelled if event.kind == SHUTDOWN else self._stopping
        for attempt in range(1, attempts + 1):
            outcome.clear()
            outcome["attempts"] = attempt
            if self._cancelled.is_set():
                log.warning("action %r: the daemon is stopping, so not started", action.name)
                outcome["skipped"] = "not started: the daemon is stopping"
                return None
            with self._slots:
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
                        action.name, attempt, attempts, status, delay)
            if interrupt.wait(delay):
                log.warning("action %r: the daemon is stopping, so not retrying", action.name)
                return status
            delay *= action.retry_backoff
        return None

    def shutdown(self, wait=False):
        """Take no more events and stop retrying (but a ``shutdown`` event's retries go on).

        With ``wait``, return once every event's jobs have finished; else return at once,
        and jobs whose command has not started yet do not start it.
        """
        self._closed = True
        self._stopping.set()
        if not wait:
            self._cancelled.set()
            return
        while True:
            with self._threads_mutex:
                threads = list(self._threads)
            if not threads:
                return
            for t in threads:
                t.join()
