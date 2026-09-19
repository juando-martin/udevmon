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


def install(dest: Path | None, now: bool, config: Path | None = None) -> Path:
    real = dest is None
    if dest is None:
        dest = Path(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")) \
            / "systemd" / "user"
    dest.mkdir(parents=True, exist_ok=True)
    unit = dest / "udevmon.service"
    unit.write_text(UNIT.format(exe=_exe(config)))
    if real:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        if now:
            subprocess.run(["systemctl", "--user", "enable", "--now", "udevmon.service"],
                           check=False)
    return unit
