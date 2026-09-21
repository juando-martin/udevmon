"""Run actions as subprocesses (never via a shell)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from .config import Action, expand
from .events import Event

log = logging.getLogger("udevmon.actions")


def build_env(event: Event, device: str | None, tag: str | None = None,
              variables=None) -> dict[str, str]:
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
    return env


def run_action(action: Action, event: Event, device: str | None, tag: str | None = None, *,
               dry_run: bool = False, variables=None) -> int | None:
    """Run one action; returns exit status (None if skipped/timeout-killed)."""
    props = dict(event.props)
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
    try:
        proc = subprocess.Popen(
            argv, cwd=os.path.expanduser("~"), env=build_env(event, device, tag, variables),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", start_new_session=True)
    except OSError as e:
        log.error("action %r failed to start: %s", action.name, e)
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


class ActionRunner:
    def __init__(self, max_workers: int = 4, dry_run: bool = False):
        self.dry_run = dry_run
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="action")

    def submit(self, event: Event, jobs):
        """Run the jobs (engine.Job) sequentially in one worker."""
        return self._pool.submit(self._run_all, event, jobs)

    def _run_all(self, event, jobs):
        for job in jobs:
            try:
                run_action(job.action, event, job.device, job.tag, dry_run=self.dry_run,
                           variables=job.vars)
            except Exception:
                log.exception("action %r crashed", job.action.name)

    def shutdown(self, wait=False):
        self._pool.shutdown(wait=wait, cancel_futures=not wait)
