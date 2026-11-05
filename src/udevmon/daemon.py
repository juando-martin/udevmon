"""The long-running daemon: sources -> engine -> runner, on one GLib main loop."""

from __future__ import annotations

import fcntl
import logging
import os
import signal
from pathlib import Path

from . import config as cfgmod
from . import state
from .actions import ActionRunner
from .engine import Engine
from .events import (CONNECTED, MOUNTED, SHUTDOWN, STARTUP, TRIGGER_KINDS, UNMOUNTED, Endpoint,
                     Event, describe)
from .runtime import lock_path  # noqa: F401  (also used as daemon.lock_path)
from .sources.udev import UdevSource
from .sources.udisks import UdisksSource

log = logging.getLogger("udevmon.daemon")


def _is_udevmon(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return b"udevmon" in f.read()
    except OSError:
        return False


def running_daemon() -> tuple[int, str | None] | None:
    """(pid, config path it runs with) of the running daemon, or None.

    The lock file holds the PID, then the config path on a second line (older
    daemons wrote only the PID, so the config can be unknown).
    """
    try:
        first, _, rest = lock_path().read_text().partition("\n")
        pid = int(first.strip())
    except (OSError, ValueError):
        return None
    if not _is_udevmon(pid):
        return None
    return pid, (rest.strip() or None)


def running_pid() -> int | None:
    """PID of a running daemon, or None."""
    found = running_daemon()
    return found[0] if found else None


def _fits(kind: str, endpoint: Endpoint) -> bool:
    """Whether ``udevmon trigger`` can fire this kind for this present thing."""
    if kind in (STARTUP, SHUTDOWN):
        return True
    if kind == CONNECTED:
        return not endpoint.is_filesystem
    if kind == MOUNTED:
        return endpoint.is_filesystem and bool(endpoint.props.get("mountpoint"))
    return endpoint.is_filesystem                    # unmounted


def _same_file(a, b) -> bool:
    return os.path.realpath(os.path.expanduser(str(a))) == os.path.realpath(os.path.expanduser(str(b)))


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
        os.write(fd, f"{os.getpid()}\n{os.path.realpath(self.config_path)}\n".encode())
        return fd                       # keep open for the process lifetime

    def banner(self) -> str:
        """The line logged when the daemon starts."""
        from . import pause
        return (f"udevmon running: {len(self.cfg.devices)} device(s), "
                f"{len(self.cfg.actions)} action(s)" + (" [dry-run]" if self.dry_run else "")
                + (" [PAUSED]" if pause.is_paused() else ""))

    def _emit(self, event: Event) -> None:
        try:
            self.engine.handle(event)
        except Exception:
            log.exception("error dispatching %s", event.kind)

    def fire_shutdown(self) -> None:
        """Emit a ``shutdown`` event for every device still present."""
        for s in self.sources:
            try:
                for endpoint in s.present():
                    self.engine.handle(Event(SHUTDOWN, endpoint))
            except Exception:
                log.exception("error firing shutdown events")

    def present_things(self) -> list[Endpoint]:
        return [e for s in self.sources for e in s.present()]

    def control(self, req: dict):
        """Answer a request from the control socket (on the main loop): (reply, wait)."""
        from . import pause
        from .matching import resolve
        if req.get("cmd") != "trigger":
            return {"ok": False, "message": f"unknown request {req.get('cmd')!r}"}, None
        name, kind = req.get("device"), req.get("event") or STARTUP
        if kind not in TRIGGER_KINDS:
            return {"ok": False, "message": f"cannot trigger {kind!r}: only "
                    f"{', '.join(TRIGGER_KINDS)} (the others say the thing is gone)"}, None
        device = self.cfg.devices.get(name) if isinstance(name, str) else None
        if device is None:
            return {"ok": False, "message": f"no device {name!r} in the configuration the "
                    "daemon runs with"}, None
        if device.status == "ignored":
            return {"ok": False, "message": f"device {name!r} is ignored: nothing runs for "
                    "it"}, None
        if pause.is_paused():
            return {"ok": False, "message": "automation is paused: nothing runs "
                    "(`udevmon resume` first)"}, None
        things = [e for e in self.present_things()
                  if any(d.name == name for d in resolve(self.cfg, e.props).named)]
        if not things:
            return {"ok": False, "message": f"device {name!r} is not present"}, None
        fitting = [e for e in things if _fits(kind, e)]
        if not fitting:
            what = ("a filesystem that is mounted" if kind == MOUNTED else "a filesystem"
                    if kind == UNMOUNTED else "a USB device")
            return {"ok": False, "message": f"{kind!r} needs {what}; device {name!r} is "
                    f"{'; '.join(describe(e) for e in things)}"}, None
        results, futures = [], []
        for endpoint in fitting:
            if kind == UNMOUNTED:                       # as after a clean unmount
                endpoint = Endpoint(endpoint.source, endpoint.key,
                                    {**endpoint.props, "surprise": "0"})
            event = Event(kind, endpoint)
            jobs, skipped = self.engine.trigger(event)
            if jobs:
                futures.append(self.runner.submit(event, jobs))
            results.append({"thing": describe(endpoint),
                            "actions": [j.action.name for j in jobs],
                            "skipped": [[x.action.name, x.reason] for x in skipped]})
        reply = {"ok": True, "event": kind, "device": name, "things": results}
        if not (req.get("wait") and futures):
            return reply, None

        def wait():
            return {"statuses": [list(r) for f in futures for r in f.result()]}
        return reply, wait

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
        for w in [*new.warnings, *cfgmod.command_problems(new)]:
            log.warning(w)
        for n in cfgmod.command_notes(new):
            log.info(n)
        log.info("config reloaded: %d device(s), %d action(s)", len(new.devices), len(new.actions))
        return True

    def run(self) -> int:
        from gi.repository import GLib
        self._lock_fd = self._acquire_lock()
        try:
            self.cfg = cfgmod.load(self.config_path)
        except cfgmod.ConfigError as e:
            log.error("invalid config: %s", e)
            return 1
        for w in [*self.cfg.warnings, *cfgmod.command_problems(self.cfg)]:
            log.warning(w)
        for n in cfgmod.command_notes(self.cfg):
            log.info(n)
        state.wipe()                             # folders left by a crash or an earlier run
        self.runner = ActionRunner(self.cfg.settings.max_workers, self.dry_run)
        self.engine = Engine(self.cfg, self.runner)
        loop = GLib.MainLoop()
        for sig, cb in ((signal.SIGHUP, lambda: self.reload()),
                        (signal.SIGTERM, lambda: loop.quit() or False),
                        (signal.SIGINT, lambda: loop.quit() or False)):
            GLib.unix_signal_add(GLib.PRIORITY_HIGH, sig, cb)
        self.sources = [UdevSource(self.cfg.settings.udev_filters, announce_present=True),
                        UdisksSource(announce_present=True)]
        for s in self.sources:
            s.start(self._emit)
        from .control import ControlServer
        self.control_server = ControlServer(self.control)
        try:
            self.control_server.start()
        except OSError as e:
            log.warning("cannot open the control socket (`udevmon trigger` will not work): %s",
                        e)
            self.control_server = None
        log.info("%s", self.banner())
        try:
            loop.run()
        finally:
            if self.control_server is not None:
                self.control_server.stop()
            self.fire_shutdown()
            for s in self.sources:
                s.stop()
            self.runner.shutdown(wait=True)      # let shutdown actions finish
            state.wipe()
            log.info("udevmon stopped")
        return 0


def reload_running(config_path=None) -> bool:
    """Ask the running daemon to reload; True if a signal was sent.

    With ``config_path`` only a daemon that runs with that same config is
    signalled (a daemon whose config is unknown is assumed to match).
    """
    found = running_daemon()
    if found is None:
        return False
    pid, its_config = found
    if config_path is not None and its_config is not None and not _same_file(config_path, its_config):
        return False
    try:
        os.kill(pid, signal.SIGHUP)
    except OSError:
        return False
    return True
