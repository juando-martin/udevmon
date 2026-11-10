# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""A global pause: a flag file that the daemon checks for every event.

While ``udevmon.paused`` exists in the runtime directory no action runs. Its content
is empty (paused until ``resume``) or the time, in seconds since the epoch, at which
the pause ends. The runtime directory is cleared at logout and reboot, so a forgotten
pause cannot outlive the session; an expired one is ignored and removed.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime

from .runtime import pause_path

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
_PART = re.compile(r"(\d+)([smhd])")


def parse_duration(text: str) -> float:
    """Seconds in ``30m``, ``2h``, ``90s``, ``1d`` or a mix such as ``1h30m``."""
    text = text.strip().lower()
    parts = _PART.findall(text)
    if not parts or "".join(n + u for n, u in parts) != text:
        raise ValueError(f"cannot read {text!r} as a duration; use a number and a unit, "
                         "like 30m, 2h, 1h30m or 90s")
    seconds = sum(int(n) * _UNITS[u] for n, u in parts)
    if seconds <= 0:
        raise ValueError("a pause must last longer than zero")
    return float(seconds)


def pause(seconds: float | None = None, now: float | None = None) -> float | None:
    """Pause; returns when it ends (epoch seconds), or None for until ``resume``."""
    expiry = None if seconds is None else (time.time() if now is None else now) + seconds
    path = pause_path()
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("" if expiry is None else f"{expiry:.3f}")
    os.replace(tmp, path)
    return expiry


def resume() -> bool:
    """End a pause; True if there was one."""
    try:
        pause_path().unlink()
    except FileNotFoundError:
        return False
    return True


def status(now: float | None = None) -> tuple[bool, float | None]:
    """(is it paused, when does it end or None)."""
    path = pause_path()
    try:
        text = path.read_text().strip()
    except FileNotFoundError:
        return False, None
    except OSError:
        return True, None                     # unreadable: someone put it there, so honour it
    try:
        expiry = float(text) if text else None
    except ValueError:
        expiry = None                         # not a time: paused until resumed
    if expiry is not None and (time.time() if now is None else now) >= expiry:
        resume()                              # over: tidy up
        return False, None
    return True, expiry


def is_paused() -> bool:
    """Whether a pause is on now. An expired ``--for`` pause is not, and its flag file is removed."""
    return status()[0]


def describe_until(expiry: float | None, now: float | None = None) -> str:
    """'until resumed', 'until 15:42', or 'until 2026-09-22 15:42' when not today."""
    if expiry is None:
        return "until you run `udevmon resume`"
    moment = datetime.fromtimestamp(expiry)
    today = datetime.fromtimestamp(time.time() if now is None else now).date()
    return "until " + moment.strftime("%H:%M" if moment.date() == today else "%Y-%m-%d %H:%M")
