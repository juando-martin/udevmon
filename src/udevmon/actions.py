"""Run actions as subprocesses (never via a shell)."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from concurrent.futures import ThreadPoolExecutor

from .config import Action, expand
from .events import Event

log = logging.getLogger("udevmon.actions")


def build_env(event: Event, asset: str) -> dict[str, str]:
    env = dict(os.environ)
    env["UDEVMON_EVENT"] = event.kind
    env["UDEVMON_ASSET"] = asset
    for k, v in event.device.props.items():
        env[f"UDEVMON_{k.upper()}"] = v
    return env


def run_action(action: Action, event: Event, asset: str, *, dry_run: bool = False) -> int | None:
    """Run one action; returns exit status (None if skipped/timeout-killed)."""
    props = {**event.props, "asset": asset}
    try:
        argv = expand(action.run, props)
    except KeyError as e:
        log.error("action %r skipped: placeholder(s) %s unavailable for event %r",
                  action.name, e.args[0], event.kind)
        return None
    if dry_run:
        log.info("[dry-run] action %r would run: %s", action.name, argv)
        return None
    log.info("action %r: running %s", action.name, argv)
    try:
        proc = subprocess.Popen(
            argv, cwd=os.path.expanduser("~"), env=build_env(event, asset),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors="replace", start_new_session=True)
    except OSError as e:
        log.error("action %r failed to start: %s", action.name, e)
        return None
    try:
        out, _ = proc.communicate(timeout=action.timeout)
    except subprocess.TimeoutExpired:
        log.error("action %r timed out after %gs; terminating", action.name, action.timeout)
        _kill_group(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            _kill_group(proc, signal.SIGKILL)
            out, _ = proc.communicate()
        _log_output(action, out)
        return None
    _log_output(action, out)
    level = logging.INFO if proc.returncode == 0 else logging.WARNING
    log.log(level, "action %r exited with status %d", action.name, proc.returncode)
    return proc.returncode


def _kill_group(proc, sig=signal.SIGTERM):
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        pass


def _log_output(action, out):
    for line in (out or "").splitlines():
        log.info("[%s] %s", action.name, line)


class ActionRunner:
    def __init__(self, max_workers: int = 4, dry_run: bool = False):
        self.dry_run = dry_run
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="action")

    def submit(self, event: Event, jobs: list[tuple[Action, str]]):
        """Run (action, asset) jobs sequentially in one worker."""
        return self._pool.submit(self._run_all, event, jobs)

    def _run_all(self, event, jobs):
        for action, asset in jobs:
            try:
                run_action(action, event, asset, dry_run=self.dry_run)
            except Exception:
                log.exception("action %r crashed", action.name)

    def shutdown(self, wait=False):
        self._pool.shutdown(wait=wait, cancel_futures=True)
