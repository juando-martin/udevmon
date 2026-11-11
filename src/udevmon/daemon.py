# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""The long-running daemon: sources -> engine -> runner, on one GLib main loop."""

from __future__ import annotations

import fcntl
import logging
import os
import signal
import threading
from collections import OrderedDict
from dataclasses import replace
from pathlib import Path

from . import config as cfgmod
from . import state
from .actions import ActionRunner
from .control import BadParams, Refused
from .engine import Engine, Handled
from .events import (CONNECTED, MOUNTED, SHUTDOWN, STARTUP, TRIGGER_KINDS, UNMOUNTED,
                     VALID_KINDS, Endpoint, Event, describe)
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


def endpoint_params(endpoint: Endpoint) -> dict:
    """A present thing as the control socket sends it: ``source``, ``key``, ``props``."""
    return {"source": endpoint.source, "key": endpoint.key, "props": dict(endpoint.props)}


def result_params(result) -> dict:
    """A job's ``actions.Result`` as the control socket sends it: ``action``, ``status``,
    ``failure``, ``output`` (the last lines), ``attempts``, ``event``, ``device``,
    ``time`` and ``seq`` (its event's number)."""
    return {"action": result.action, "status": result.status, "failure": result.failure,
            "output": list(result.output), "attempts": result.attempts,
            "event": result.event, "device": result.device, "time": result.time,
            "seq": result.seq}


def event_params(event: Event) -> dict:
    """An event as the control socket's ``event`` notification sends it: ``seq``,
    ``kind``, ``time`` and the thing's ``source``, ``key`` and ``props``."""
    return {"seq": event.seq, "kind": event.kind, "time": event.time,
            **endpoint_params(event.endpoint)}


def handled_params(handled, trigger: bool = False) -> dict:
    """What the engine did with an event (``engine.Handled``), as the ``handled``
    notification sends it."""
    return {"seq": handled.event.seq, "kind": handled.event.kind, "trigger": trigger,
            "outcome": handled.outcome, "devices": list(handled.devices),
            "tags": list(handled.tags),
            "actions": [{"action": j.action.name, "device": j.device, "tag": j.tag}
                        for j in handled.jobs],
            "skipped": [{"action": x.action.name, "reason": x.reason}
                        for x in handled.skipped]}


#: The params each control-socket method takes, name -> kind (``str``, ``bool``, ``number``,
#: ``int`` or ``strs``, a list of strings). A leading ``!`` makes it required and a
#: trailing ``?`` allows null. Any other param, or a wrong type, is refused (-32602).
PARAMS = {
    "hello": {"protocol": "int"},
    "status": {},
    "present": {"system": "bool"},
    "results": {"action": "str"},
    "trigger": {"device": "!str", "event": "str?", "wait": "bool"},
    "pause": {"seconds": "number?"},
    "resume": {},
    "reload": {"config": "str"},
    "subscribe": {"notify": "strs", "kinds": "strs", "devices": "strs", "tags": "strs",
                  "system": "bool"},
}

#: What ``subscribe`` can ask to be told (raw events, what the engine did, action results).
NOTIFY_KINDS = ("event", "handled", "result")

_INTEREST_KEPT = 1000         # events whose handled/result notifications are still routed

_TYPES = {"str": lambda v: isinstance(v, str),
          "bool": lambda v: isinstance(v, bool),
          "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
          "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
          "strs": lambda v: isinstance(v, list) and all(isinstance(x, str) for x in v)}
_TYPE_NAMES = {"str": "a string", "bool": "true or false", "int": "a whole number",
               "number": "a number", "strs": "a list of strings"}


def check_params(method: str, params: dict) -> None:
    """Refuse (``control.BadParams``) params that ``PARAMS`` does not list for ``method``,
    a missing required one, or one of the wrong type."""
    spec = PARAMS[method]
    for name in params:
        if name not in spec:
            takes = ", ".join(sorted(spec)) or "none"
            raise BadParams(f"{method}: unknown parameter {name!r} (it takes: {takes})")
    for name, kind in spec.items():
        required, nullable = kind.startswith("!"), kind.endswith("?")
        kind = kind.strip("!?")
        if name not in params:
            if required:
                raise BadParams(f"{method}: parameter {name!r} is required")
            continue
        value = params[name]
        if value is None and nullable:
            continue
        if not _TYPES[kind](value):
            raise BadParams(f"{method}: parameter {name!r} must be {_TYPE_NAMES[kind]}"
                            + (" or null" if nullable else ""))


def _same_file(a, b) -> bool:
    return os.path.realpath(os.path.expanduser(str(a))) == os.path.realpath(os.path.expanduser(str(b)))


class Daemon:
    """The ``udevmon run`` process: sources, engine, action runner and control socket on one
    GLib main loop.

    One per user: :meth:`run` takes the lock file in the runtime folder. With ``dry_run``
    actions are logged instead of run.
    """
    def __init__(self, config_path: Path, dry_run: bool = False):
        self.config_path = config_path
        self.dry_run = dry_run
        self.sources = []
        self._subscribers: set = set()          # control.Subscription, for every event
        self._subscribers_mutex = threading.Lock()
        self.last_results: dict = {}            # action name -> its last actions.Result
        self._seq = 0                           # the number of the last event
        # event number -> the subscribers that asked for its handled/result notifications
        self._interest: OrderedDict = OrderedDict()

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

    def _number(self, event: Event) -> Event:
        """The event with the next number (``Event.seq``)."""
        self._seq += 1
        return replace(event, seq=self._seq)

    def _emit(self, event: Event) -> None:
        event = self._number(event)
        try:
            self._route(event)
        except Exception:
            log.exception("error telling the subscribers about %s", event.kind)
        try:
            self.engine.handle(event)
        except Exception:
            log.exception("error dispatching %s", event.kind)

    def _route(self, event: Event, announce: bool = True) -> None:
        """Send the ``event`` notification to the subscribers whose filters it passes
        (unless not ``announce``: a triggered event), and remember which of them want its
        ``handled`` and ``result`` notifications."""
        from .matching import resolve
        with self._subscribers_mutex:
            subscribers = list(self._subscribers)
        if not subscribers:
            return
        res, params, later = None, None, []
        for sub in subscribers:
            f = sub.filters
            if f["kinds"] and event.kind not in f["kinds"]:
                continue
            if f["devices"] or f["tags"]:
                if res is None:
                    res = resolve(self.cfg, event.props)
                if f["devices"] and not set(f["devices"]) & {d.name for d in res.named}:
                    continue
                if f["tags"] and not set(f["tags"]) & set(res.tags):
                    continue
            if announce and "event" in f["notify"]:
                params = params or event_params(event)
                sub.publish("event", params)
            if {"handled", "result"} & set(f["notify"]):
                later.append(sub)
        if later:
            self._interest[event.seq] = later
            while len(self._interest) > _INTEREST_KEPT:
                self._interest.popitem(last=False)

    def _handled(self, handled, trigger: bool = False) -> None:
        """Tell the subscribers that asked what the engine did with the event."""
        subs = self._interest.get(handled.event.seq, ())
        params = None
        for sub in subs:
            if "handled" in sub.filters["notify"]:
                params = params or handled_params(handled, trigger)
                sub.publish("handled", params)
        if subs and not handled.jobs:
            self._interest.pop(handled.event.seq, None)   # no results will follow

    def fire_shutdown(self) -> None:
        """Emit a ``shutdown`` event for every device still present."""
        for s in self.sources:
            try:
                for endpoint in s.present():
                    self.engine.handle(self._number(Event(SHUTDOWN, endpoint)))
            except Exception:
                log.exception("error firing shutdown events")

    def present_things(self) -> list[Endpoint]:
        """Everything the sources know to be present now."""
        return [e for s in self.sources for e in s.present()]

    def record_result(self, result) -> None:
        """Keep each action's last result (``results`` on the control socket) and tell the
        subscribers that asked; called by the runner from the event's thread."""
        self.last_results[result.action] = result
        subs = self._interest.get(result.seq, ())
        params = None
        for sub in subs:
            if "result" in sub.filters["notify"]:
                params = params or result_params(result)
                sub.publish("result", params)

    #: The control socket's methods (``control.ControlServer``), each a method of this class.
    METHODS = {"hello": "_hello", "status": "_status", "present": "_present",
               "results": "_results", "trigger": "_trigger", "pause": "_pause",
               "resume": "_resume", "reload": "_reload", "subscribe": "_subscribe"}

    def control(self, method: str, params: dict):
        """Answer a control-socket request (on the main loop): ``(result, extra)``, as
        ``control.ControlServer`` takes it; raises ``control.Refused`` to refuse."""
        from .control import UnknownMethod
        if method not in self.METHODS:
            raise UnknownMethod(method)
        check_params(method, params)
        return getattr(self, self.METHODS[method])(params)

    def _hello(self, params):
        from . import __version__
        from .cli import FEATURES
        from .control import PROTOCOL
        theirs = params.get("protocol")
        if theirs is not None and theirs != PROTOCOL:
            raise Refused("protocol-mismatch", f"the daemon speaks protocol {PROTOCOL}, "
                          f"not {theirs}", protocol=PROTOCOL)
        return {"protocol": PROTOCOL, "version": __version__, "features": list(FEATURES),
                "methods": sorted(self.METHODS)}, None

    def _status(self, params):
        from . import __version__, pause
        paused, until = pause.status()
        return {"pid": os.getpid(), "version": __version__,
                "config": os.path.realpath(self.config_path), "dry_run": self.dry_run,
                "paused": paused, "until": until, "devices": len(self.cfg.devices),
                "actions": len(self.cfg.actions)}, None

    def _results(self, params):
        action = params.get("action")
        results = sorted(dict(self.last_results).values(), key=lambda r: r.time, reverse=True)
        return {"results": [result_params(r) for r in results
                            if action is None or r.action == action]}, None

    def _present_params(self, system: bool = True) -> list[dict]:
        things = []
        for s in self.sources:
            things += s.present(include_system=system) if isinstance(s, UdisksSource) \
                else s.present()
        return [endpoint_params(e) for e in things]

    def _present(self, params):
        return {"seq": self._seq,
                "things": self._present_params(params.get("system", True))}, None

    def _pause(self, params):
        from . import pause
        seconds = params.get("seconds")
        if seconds is not None and seconds <= 0:
            raise BadParams("pause: parameter 'seconds' must be above 0, or null")
        was_paused, _ = pause.status()
        until = pause.pause(seconds)
        log.info("automation paused %s (udevmon pause)", pause.describe_until(until))
        return {"paused": True, "until": until, "was_paused": was_paused}, None

    def _resume(self, params):
        from . import pause
        was_paused = pause.resume()
        if was_paused:
            log.info("automation resumed (udevmon resume)")
        return {"was_paused": was_paused}, None

    def _reload(self, params):
        config = params.get("config")
        if config is not None:
            if not _same_file(config, self.config_path):
                raise Refused("other-config", f"the daemon uses "
                              f"{os.path.realpath(self.config_path)}, not {config}")
        try:
            return self.apply_reload(), None
        except cfgmod.ConfigError as e:
            log.error("reload failed, keeping previous config: %s", e)
            raise Refused("bad-config", "the configuration has errors; the daemon keeps the "
                          "previous one", problems=list(e.problems)) from None

    def _subscribe(self, params):
        from .control import Subscription
        notify = params.get("notify", ["event"])
        bad = [n for n in notify if n not in NOTIFY_KINDS] or ([] if notify else ["none"])
        if bad:
            raise BadParams(f"subscribe: 'notify' takes {', '.join(NOTIFY_KINDS)}, not "
                            f"{bad[0]!r}")
        kinds = params.get("kinds", [])
        bad = [k for k in kinds if k not in VALID_KINDS]
        if bad:
            raise BadParams(f"subscribe: {bad[0]!r} is not an event kind")

        def closed(sub):
            with self._subscribers_mutex:
                self._subscribers.discard(sub)
        sub = Subscription(on_close=closed)
        sub.filters = {"notify": list(dict.fromkeys(notify)), "kinds": kinds,
                       "devices": params.get("devices", []), "tags": params.get("tags", [])}
        with self._subscribers_mutex:
            self._subscribers.add(sub)
        # taken on the main loop, like the events: every later event has a higher seq
        return {"seq": self._seq, "present": self._present_params(params.get("system", True))}, sub

    def _trigger(self, params):
        from . import pause
        from .matching import resolve
        name, kind = params.get("device"), params.get("event") or STARTUP
        if kind not in TRIGGER_KINDS:
            raise Refused("bad-request", f"cannot trigger {kind!r}: only "
                          f"{', '.join(TRIGGER_KINDS)} (the others say the thing is gone)")
        device = self.cfg.devices.get(name) if isinstance(name, str) else None
        if device is None:
            raise Refused("unknown-device", f"no device {name!r} in the configuration the "
                          "daemon runs with")
        if device.status == "ignored":
            raise Refused("ignored", f"device {name!r} is ignored: nothing runs for it")
        if pause.is_paused():
            raise Refused("paused", "automation is paused: nothing runs "
                          "(`udevmon resume` first)")
        things = [e for e in self.present_things()
                  if any(d.name == name for d in resolve(self.cfg, e.props).named)]
        if not things:
            raise Refused("not-present", f"device {name!r} is not present")
        fitting = [e for e in things if _fits(kind, e)]
        if not fitting:
            what = ("a filesystem that is mounted" if kind == MOUNTED else "a filesystem"
                    if kind == UNMOUNTED else "a USB device")
            raise Refused("wrong-event", f"{kind!r} needs {what}; device {name!r} is "
                          f"{'; '.join(describe(e) for e in things)}")
        results, futures = [], []
        for endpoint in fitting:
            if kind == UNMOUNTED:                       # as after a clean unmount
                endpoint = Endpoint(endpoint.source, endpoint.key,
                                    {**endpoint.props, "surprise": "0"})
            event = self._number(Event(kind, endpoint))
            self._route(event, announce=False)
            jobs, skipped = self.engine.trigger(event)
            res = resolve(self.cfg, event.props)
            self._handled(Handled(event, "ran" if jobs else "nothing-to-run",
                                  tuple(d.name for d in res.named), tuple(res.tags), jobs,
                                  skipped), trigger=True)
            if jobs:
                futures.append(self.runner.submit(event, jobs))
            results.append({"thing": describe(endpoint),
                          "actions": [j.action.name for j in jobs],
                          "skipped": [[x.action.name, x.reason] for x in skipped]})
        reply = {"event": kind, "device": name, "things": results}
        if not (params.get("wait") and futures):
            return reply, None

        def wait():
            return {"statuses": [result_params(r) for f in futures for r in f.result()]}
        return reply, wait

    def reload(self) -> bool:
        """Read the configuration again (on SIGHUP).

        An invalid one is logged and the previous one kept. Always returns True, which keeps
        the signal handler.
        """
        try:
            self.apply_reload()
        except cfgmod.ConfigError as e:
            log.error("reload failed, keeping previous config: %s", e)
        return True

    def apply_reload(self) -> dict:
        """Read the configuration again and use it; raises ``ConfigError`` (keeping the old
        one) when it is invalid. A change of ``udev_filters`` needs a restart.

        Returns ``devices`` and ``actions`` (counts), ``warnings`` (with the command
        problems) and ``restart_needed``.
        """
        new = cfgmod.load(self.config_path)
        restart = new.settings.udev_filters != self.cfg.settings.udev_filters
        if restart:
            log.warning("udev_filters changed; restart the daemon to apply")
        self.cfg = new
        self.engine.set_config(new)
        warnings = [*new.warnings, *cfgmod.command_problems(new)]
        for w in warnings:
            log.warning(w)
        for n in cfgmod.command_notes(new):
            log.info(n)
        log.info("config reloaded: %d device(s), %d action(s)", len(new.devices), len(new.actions))
        return {"devices": len(new.devices), "actions": len(new.actions),
                "warnings": warnings, "restart_needed": restart}

    def run(self) -> int:
        """Run until SIGTERM or SIGINT and return the exit status (1: invalid configuration).

        Raises ``SystemExit`` when another daemon holds the lock. Needs udev, the system bus
        (udisks2) and a GLib main loop. At the end it sends ``shutdown`` for everything still
        present, waits for those actions, and removes the state folders.
        """
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
        self.runner = ActionRunner(self.cfg.settings.max_workers, self.dry_run,
                                   on_result=self.record_result)
        self.engine = Engine(self.cfg, self.runner, on_handled=self._handled)
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
            log.warning("cannot open the control socket (`udevmon trigger` and `scan` through "
                        "the daemon will not work; `reload` falls back to SIGHUP): %s", e)
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


def reload_running(config_path=None) -> dict | None:
    """Ask the running daemon to reload its configuration.

    None when no daemon runs, or (with ``config_path``) it runs with another config (a
    daemon whose config is unknown is assumed to match). Else the daemon's answer:
    ``devices``, ``actions``, ``warnings``, ``restart_needed``, with ``via`` ``socket``;
    or only ``via`` ``signal`` when it was sent ``SIGHUP`` because its control socket does
    not answer (an older daemon, or one that could not open the socket). Raises
    ``control.Refused`` (``bad-config``, with ``problems``) when the new configuration is
    invalid; the daemon then keeps the previous one.
    """
    from . import control
    found = running_daemon()
    if found is None:
        return None
    pid, its_config = found
    if config_path is not None and its_config is not None and not _same_file(config_path, its_config):
        return None
    params = {"config": os.path.realpath(os.path.expanduser(str(config_path)))} \
        if config_path is not None else None
    try:
        return {**control.call("reload", params, timeout=60.0), "via": "socket"}
    except control.Refused as e:
        if e.error == "other-config":
            return None
        raise
    except (control.NotRunning, control.ProtocolError, OSError):
        pass
    try:
        os.kill(pid, signal.SIGHUP)
    except OSError:
        return None
    return {"via": "signal"}
