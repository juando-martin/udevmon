"""The long-running daemon: sources -> engine -> runner, on one GLib main loop."""

from __future__ import annotations

import fcntl
import logging
import os
import signal
from pathlib import Path

from . import config as cfgmod
from .actions import ActionRunner
from .engine import Engine
from .events import Event
from .sources.udev import UdevSource
from .sources.udisks import UdisksSource

log = logging.getLogger("udevmon.daemon")


def lock_path() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/udevmon-{os.getuid()}"
    Path(base).mkdir(mode=0o700, exist_ok=True)
    return Path(base) / "udevmon.lock"


def running_pid() -> int | None:
    """PID of a running daemon, or None."""
    try:
        pid = int(lock_path().read_text().strip())
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            cmd = f.read()
    except (OSError, ValueError):
        return None
    return pid if b"udevmon" in cmd else None


class Daemon:
    def __init__(self, config_path: Path, dry_run: bool = False):
        self.config_path = config_path
        self.dry_run = dry_run
        self.sources = []

    def _acquire_lock(self):
        fd = os.open(lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("udevmon is already running")
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        return fd                       # keep open for the process lifetime

    def _emit(self, event: Event) -> None:
        try:
            self.engine.handle(event)
        except Exception:
            log.exception("error dispatching %s", event.kind)

    def reload(self) -> bool:
        try:
            new = cfgmod.load(self.config_path)
        except cfgmod.ConfigError as e:
            log.error("reload failed, keeping previous config: %s", e)
            return True
        if new.settings.udev_filters != self.cfg.settings.udev_filters:
            log.warning("udev_filters changed; restart the daemon to apply")
        self.cfg = new
        self.engine.set_config(new)
        for w in new.warnings:
            log.warning(w)
        log.info("config reloaded: %d asset(s), %d action(s)", len(new.assets), len(new.actions))
        return True

    def run(self) -> int:
        from gi.repository import GLib
        self._lock_fd = self._acquire_lock()
        try:
            self.cfg = cfgmod.load(self.config_path)
        except cfgmod.ConfigError as e:
            log.error("invalid config: %s", e)
            return 1
        for w in self.cfg.warnings:
            log.warning(w)
        self.runner = ActionRunner(self.cfg.settings.max_workers, self.dry_run)
        self.engine = Engine(self.cfg, self.runner)
        loop = GLib.MainLoop()
        for sig, cb in ((signal.SIGHUP, lambda: self.reload()),
                        (signal.SIGTERM, lambda: loop.quit() or False),
                        (signal.SIGINT, lambda: loop.quit() or False)):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, cb)
        self.sources = [UdevSource(self.cfg.settings.udev_filters), UdisksSource()]
        for s in self.sources:
            s.start(self._emit)
        log.info("udevmon running: %d asset(s), %d action(s)%s", len(self.cfg.assets),
                 len(self.cfg.actions), " [dry-run]" if self.dry_run else "")
        try:
            loop.run()
        finally:
            for s in self.sources:
                s.stop()
            self.runner.shutdown()
            log.info("udevmon stopped")
        return 0


def reload_running() -> bool:
    pid = running_pid()
    if pid is None:
        return False
    os.kill(pid, signal.SIGHUP)
    return True
