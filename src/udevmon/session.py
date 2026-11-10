# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Find the graphical session's variables (DISPLAY and friends) for the actions.

A systemd user service gets its environment from the user manager when it starts. The
desktop imports DISPLAY and the rest into the manager at some point during login, or never
(Cinnamon and MATE on Ubuntu 22.04, for example). The service usually starts first, as the
user manager does, at login. Then the daemon has no DISPLAY, and neither has anything an
action starts.

So the variables are looked up again for every action:

1. The user manager's environment as it is now (``systemctl --user show-environment``), which
   has whatever the desktop imported after the daemon started. Under the service this wins
   over the daemon's own copy, which may be from before the login (or an earlier session).
2. If that has no display, the environment of the newest of your own programs that has one,
   read from ``/proc``, preferring programs of your graphical login (logind) so an
   ``ssh -X`` shell is not taken for the desktop. This covers desktops that never import.
3. If there is still none but logind says a graphical login is there (the daemon was started
   at the very beginning of it), wait for one of the above, up to ``WAIT`` seconds. A login
   with a display logind knows of but no program found gets that DISPLAY on its own.

Run from a terminal with a display, the daemon's own environment is used as it is.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

log = logging.getLogger("udevmon.session")

DISPLAY_VARS = ("DISPLAY", "WAYLAND_DISPLAY")
SESSION_VARS = DISPLAY_VARS + (
    "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS", "XDG_SESSION_TYPE", "XDG_CURRENT_DESKTOP",
    "XDG_SESSION_DESKTOP", "DESKTOP_SESSION")
GRAPHICAL_TYPES = ("x11", "wayland", "mir")
WAIT = 30.0                    # seconds to wait for a graphical login that has no display yet
POLL = 0.5

_sleep, _monotonic = time.sleep, time.monotonic       # replaced by the tests
_last_note: str | None = None                         # what was last logged, to say it once
_note_lock = threading.Lock()


def _has_display(env: dict[str, str]) -> bool:
    return any(env.get(k) for k in DISPLAY_VARS)


def under_service(env: dict[str, str] | None = None) -> bool:
    """Whether the daemon was started by systemd (which sets INVOCATION_ID for a unit)."""
    return bool((os.environ if env is None else env).get("INVOCATION_ID"))


def _pick(env: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k in SESSION_VARS and v}


def _run(argv: list[str]) -> str | None:
    try:
        p = subprocess.run(argv, capture_output=True, text=True, errors="replace",
                           timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def from_manager() -> dict[str, str]:
    """The session variables in the systemd user manager's environment now."""
    out = _run(["systemctl", "--user", "show-environment"])
    env = {}
    for line in (out or "").splitlines():
        key, sep, value = line.partition("=")
        if sep:
            env[key] = _unquote(value)
    return _pick(env)


def _unquote(value: str) -> str:
    # show-environment quotes values with special characters, $'...' style
    if len(value) >= 3 and value.startswith("$'") and value.endswith("'"):
        try:
            return value[2:-1].encode("latin-1", "backslashreplace").decode("unicode_escape")
        except UnicodeDecodeError:
            return value[2:-1]
    return value


def graphical_sessions() -> dict[str, str]:
    """Your graphical logins that logind knows of: session id -> its X display ("" if none)."""
    ids = _run(["loginctl", "show-user", str(os.getuid()), "-p", "Sessions", "--value"])
    found = {}
    for sid in (ids or "").split():
        out = _run(["loginctl", "show-session", sid,
                    "-p", "Type", "-p", "State", "-p", "Class", "-p", "Display"])
        props = dict(line.partition("=")[::2] for line in (out or "").splitlines())
        if (props.get("Type") in GRAPHICAL_TYPES and props.get("Class", "user") == "user"
                and props.get("State") in ("active", "online")):
            found[sid] = props.get("Display", "")
    return found


def from_processes(proc: str = "/proc", sessions=()) -> dict[str, str]:
    """The session variables of the newest of your own programs that has a display.

    A program of one of ``sessions`` (logind ids, matched on its XDG_SESSION_ID) wins over
    any other, however new the other is.
    """
    uid, me = os.getuid(), os.getpid()
    best: tuple[tuple[bool, int], dict[str, str]] | None = None
    try:
        entries = os.listdir(proc)
    except OSError:
        return {}
    for name in entries:
        if not name.isdigit() or int(name) == me:
            continue
        base = os.path.join(proc, name)
        try:
            if os.stat(base).st_uid != uid:
                continue
            with open(os.path.join(base, "environ"), "rb") as f:
                raw = f.read()
            with open(os.path.join(base, "stat"), "rb") as f:
                stat = f.read()
        except OSError:                  # gone already, or not readable
            continue
        full = {}
        for item in raw.split(b"\0"):
            key, sep, value = item.partition(b"=")
            if sep:
                full[key.decode(errors="replace")] = value.decode(errors="replace")
        env = _pick(full)
        if not _has_display(env):
            continue
        try:                             # start time: field 22, after the "(comm)" field
            started = int(stat[stat.rindex(b")") + 2:].split()[19])
        except (ValueError, IndexError):
            started = 0
        rank = (full.get("XDG_SESSION_ID") in sessions, started)
        if best is None or rank > best[0]:
            best = (rank, env)
    return best[1] if best else {}


def _lookup(env: dict[str, str], sessions) -> tuple[dict[str, str], str | None]:
    found = from_manager()
    if _has_display(found) or _has_display(env):
        return found, "the systemd user manager" if found else None
    procs = from_processes(sessions=sessions)
    if procs:                            # its values belong together; the manager's fill gaps
        return {**found, **procs}, "a running program of yours"
    return found, None


def update(env: dict[str, str]) -> None:
    """Fill in (and, under the service, refresh) the session variables in ``env``.

    May wait up to ``WAIT`` seconds, while a graphical login is starting.
    """
    service = under_service(env)
    if _has_display(env) and not service:
        return
    found, source = _lookup(env, ())
    if not _has_display(found) and not _has_display(env):
        sessions = graphical_sessions()
        if sessions:
            _note(f"waiting up to {WAIT:g}s for the graphical session to get a display",
                  logging.INFO)
            deadline = _monotonic() + WAIT
            found, source = _lookup(env, sessions)
            while not _has_display(found) and _monotonic() < deadline:
                _sleep(POLL)
                found, source = _lookup(env, sessions)
            if not _has_display(found):
                display = next((d for d in sessions.values() if d), "")
                if display:
                    found, source = {**found, "DISPLAY": display}, "logind"
    if service and _has_display(found):
        # the manager's (or the program's) values are current; ours may be stale
        for k in DISPLAY_VARS:
            env.pop(k, None)
        env.update(found)
    else:
        for k, v in found.items():
            env.setdefault(k, v)
    if _has_display(env):
        shown = ", ".join(f"{k}={env[k]}" for k in DISPLAY_VARS if env.get(k))
        _note(f"graphical session for actions: {shown}"
              + (f" (from {source})" if source else ""), logging.INFO)
    else:
        _note("no graphical session found (no DISPLAY or WAYLAND_DISPLAY): "
              "graphical programs started by actions will fail", logging.WARNING)


def _note(note: str, level: int) -> None:
    global _last_note
    with _note_lock:
        if note == _last_note:
            return
        _last_note = note
    log.log(level, "%s", note)
