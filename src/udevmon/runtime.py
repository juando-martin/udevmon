"""Files udevmon keeps in the user's runtime directory (cleared at logout or reboot)."""

from __future__ import annotations

import os
from pathlib import Path


def runtime_dir() -> Path:
    """``$XDG_RUNTIME_DIR`` (else ``/tmp/udevmon-UID``), created if missing."""
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/udevmon-{os.getuid()}"
    Path(base).mkdir(mode=0o700, exist_ok=True)
    return Path(base)


def lock_path() -> Path:
    """The lock file a running daemon holds; it records its pid and config."""
    return runtime_dir() / "udevmon.lock"


def pause_path() -> Path:
    """The pause flag file (``udevmon pause``)."""
    return runtime_dir() / "udevmon.paused"


def socket_path() -> Path:
    """The running daemon's control socket (``udevmon trigger`` talks to it)."""
    return runtime_dir() / "udevmon.sock"
