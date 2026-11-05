"""Files udevmon keeps in the user's runtime directory (cleared at logout or reboot)."""

from __future__ import annotations

import os
from pathlib import Path


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/udevmon-{os.getuid()}"
    Path(base).mkdir(mode=0o700, exist_ok=True)
    return Path(base)


def lock_path() -> Path:
    return runtime_dir() / "udevmon.lock"


def pause_path() -> Path:
    return runtime_dir() / "udevmon.paused"


def socket_path() -> Path:
    """The running daemon's control socket (``udevmon trigger`` talks to it)."""
    return runtime_dir() / "udevmon.sock"
