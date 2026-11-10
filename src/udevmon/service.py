"""systemd user unit installation."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

UNIT = """\
[Unit]
Description=udevmon: run actions on USB / mount events

[Service]
Type=simple
ExecStart={exe} run
ExecReload={exe} reload
Restart=on-failure
RestartSec=3
MemoryMax=100M
TimeoutStopSec=120

[Install]
WantedBy=default.target
"""


def _exe(config: Path | None) -> str:
    found = shutil.which("udevmon")
    parts = [found] if found else [sys.executable, "-m", "udevmon"]
    if config:
        parts += ["--config", str(config)]
    return " ".join(shlex.quote(p) for p in parts)


def unit_dir() -> Path:
    """Where systemd looks for the user's own units: ``$XDG_CONFIG_HOME/systemd/user``."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "systemd" / "user"


def _systemctl(*args: str) -> None:
    try:
        subprocess.run(["systemctl", "--user", *args], check=False)
    except OSError:                      # no systemctl here: nothing to tell
        pass


def uninstall(dest: Path | None) -> tuple[str, Path]:
    """Stop, disable and remove the unit; returns ("removed" | "absent" | "foreign", path).

    With ``dest`` only the file is removed (no systemctl), mirroring ``install``.
    A file that does not look like udevmon's own unit is never deleted.
    """
    real = dest is None
    unit = (dest or unit_dir()) / "udevmon.service"
    if not unit.exists():
        return "absent", unit
    text = unit.read_text(errors="replace")
    if "udevmon" not in text or "ExecStart=" not in text:
        return "foreign", unit
    if real:                             # needs the unit file, so before removing it
        _systemctl("disable", "--now", "udevmon.service")
    unit.unlink()
    if real:
        _systemctl("daemon-reload")
    return "removed", unit


def install(dest: Path | None, now: bool, config: Path | None = None) -> Path:
    """Write ``udevmon.service`` and return its path.

    Into the user's unit folder, then ``systemctl --user daemon-reload`` (and ``enable --now``
    with ``now``); or into ``dest``, without systemctl.
    """
    real = dest is None
    if dest is None:
        dest = unit_dir()
    dest.mkdir(parents=True, exist_ok=True)
    unit = dest / "udevmon.service"
    unit.write_text(UNIT.format(exe=_exe(config)))
    if real:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        if now:
            subprocess.run(["systemctl", "--user", "enable", "--now", "udevmon.service"],
                           check=False)
    return unit
