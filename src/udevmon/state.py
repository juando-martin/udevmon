"""Per-instance state folders: a private place where the actions of one plug (or one mount)
can leave notes for each other, e.g. what a connect script found out for its disconnect.

``$XDG_RUNTIME_DIR/udevmon/state/<instance>``, mode 0700. Created before the first action
of an instance runs, removed after the actions of its last event have finished, and the
whole tree is wiped when the daemon starts and stops (spec 7.5).
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from .runtime import runtime_dir

_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def root() -> Path:
    return runtime_dir() / "udevmon" / "state"


def path_for(instance: str) -> Path:
    """The folder of ``instance`` (anything outside ``A-Za-z0-9._-`` becomes ``_``)."""
    return root() / (_UNSAFE.sub("_", instance).lstrip(".") or "_")


def ensure(instance: str) -> Path:
    p = path_for(instance)
    p.mkdir(mode=0o700, parents=True, exist_ok=True)
    return p


def remove(instance: str) -> None:
    shutil.rmtree(path_for(instance), ignore_errors=True)


def wipe() -> None:
    """Remove every state folder (stale ones after a crash, and all at a normal stop)."""
    shutil.rmtree(root(), ignore_errors=True)
