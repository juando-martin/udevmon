"""Config loading, validation and placeholder handling."""

from __future__ import annotations

import os
import re
import shlex
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .events import KNOWN_KEYS, VALID_KINDS
from .matching import WEAK, strength

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "udevmon" / "config.toml"


@dataclass(frozen=True)
class Asset:
    name: str
    match: dict[str, str]
    description: str = ""


@dataclass(frozen=True)
class Action:
    name: str
    asset: str
    on: tuple[str, ...]
    run: tuple[str, ...]
    timeout: float = 60.0


@dataclass(frozen=True)
class Settings:
    debounce: float = 1.0
    udev_filters: tuple[str, ...] = ("usb/usb_device",)
    max_workers: int = 4


@dataclass
class Config:
    settings: Settings = field(default_factory=Settings)
    assets: dict[str, Asset] = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# Only {lower_snake_name} is a placeholder. "{{" / "}}" are literal braces, and anything
# else (shell ${1:-x}, ${HOME}, awk '{print $1}', a "$"-prefixed {name}) is left alone.
_TOKEN = re.compile(r"\{\{|\}\}|(?<!\$)\{([a-z_][a-z0-9_]*)\}")


def placeholders(arg: str) -> list[str]:
    """Placeholder names used in ``arg``."""
    return [m.group(1) for m in _TOKEN.finditer(arg) if m.group(1)]


def expand(argv, props) -> list[str]:
    """Expand ``~`` and ``{name}`` placeholders; KeyError if one is missing."""
    def fill(m):
        if m.group(1):
            return props[m.group(1)]       # single pass: values are never re-scanned
        return m.group(0)[0]

    out = []
    for arg in argv:
        arg = os.path.expanduser(arg)
        missing = [n for n in placeholders(arg) if n not in props]
        if missing:
            raise KeyError(", ".join(missing))
        out.append(_TOKEN.sub(fill, arg))
    return out


_TOP = {"daemon", "assets", "actions"}
_DAEMON = {"debounce", "udev_filters", "max_workers"}
_ASSET = {"description", "match"}
_ACTION = {"name", "asset", "on", "run", "timeout"}


def parse(data: dict) -> Config:
    problems: list[str] = []
    cfg = Config()

    def unknown(where: str, got, allowed):
        for k in got:
            if k not in allowed:
                problems.append(f"{where}: unknown key {k!r}")

    unknown("top level", data, _TOP)

    d = data.get("daemon", {})
    unknown("[daemon]", d, _DAEMON)
    try:
        debounce = float(d.get("debounce", 1.0))
        workers = int(d.get("max_workers", 4))
        filters = d.get("udev_filters", ["usb/usb_device"])
        if debounce < 0 or workers < 1:
            raise ValueError
        if not (isinstance(filters, list) and all(isinstance(f, str) and f for f in filters)):
            raise ValueError
        cfg.settings = Settings(debounce, tuple(filters), workers)
    except (TypeError, ValueError):
        problems.append("[daemon]: invalid debounce/udev_filters/max_workers")

    for name, a in (data.get("assets") or {}).items():
        where = f"[assets.{name}]"
        if not NAME_RE.match(name):
            problems.append(f"{where}: invalid name")
            continue
        if not isinstance(a, dict):
            problems.append(f"{where}: must be a table")
            continue
        unknown(where, a, _ASSET)
        m = a.get("match")
        if not isinstance(m, dict) or not m:
            problems.append(f"{where}: 'match' must be a non-empty table")
            continue
        if not all(isinstance(v, (str, int)) and not isinstance(v, bool) for v in m.values()):
            problems.append(f"{where}: match values must be strings")
            continue
        match = {str(k): str(v) for k, v in m.items()}
        cfg.assets[name] = Asset(name, match, str(a.get("description", "")))
        if strength(match) == WEAK:
            cfg.warnings.append(f"asset {name!r}: weak match {match} (matches every "
                                "device/filesystem with these values)")

    seen: set[str] = set()
    for i, a in enumerate(data.get("actions") or [], 1):
        where = f"[[actions]] #{i}"
        if not isinstance(a, dict):
            problems.append(f"{where}: must be a table")
            continue
        unknown(where, a, _ACTION)
        asset = a.get("asset")
        if asset not in cfg.assets:
            problems.append(f"{where}: unknown asset {asset!r}")
            continue
        name = a.get("name") or f"{asset}-{i}"
        where = f"action {name!r}"
        if not isinstance(name, str) or not NAME_RE.match(name):
            problems.append(f"{where}: invalid name")
            continue
        if name in seen:
            problems.append(f"{where}: duplicate name")
            continue
        seen.add(name)
        on = a.get("on")
        on = (on,) if isinstance(on, str) else tuple(on or ())
        bad = [k for k in on if k not in VALID_KINDS]
        if not on or bad:
            problems.append(f"{where}: 'on' must list kinds from {sorted(VALID_KINDS)}"
                            + (f" (bad: {bad})" if bad else ""))
            continue
        run = a.get("run")
        try:
            if isinstance(run, str):
                run = shlex.split(run)
            if not (isinstance(run, list) and run and all(isinstance(x, str) for x in run)):
                raise ValueError("'run' must be a non-empty argv list or string")
            names = {n for arg in run for n in placeholders(arg)}
        except ValueError as e:
            problems.append(f"{where}: {e}")
            continue
        for kind in on:
            missing = names - KNOWN_KEYS[kind]
            if missing:
                problems.append(f"{where}: placeholder(s) {sorted(missing)} not available "
                                f"for event {kind!r}")
        try:
            timeout = float(a.get("timeout", 60))
            if timeout <= 0:
                raise ValueError
        except (TypeError, ValueError):
            problems.append(f"{where}: invalid timeout")
            continue
        cfg.actions.append(Action(name, asset, on, tuple(run), timeout))

    if problems:
        raise ConfigError(problems)
    return cfg


def check_permissions(path: Path) -> None:
    st = path.stat()
    if st.st_uid != os.getuid():
        raise ConfigError([f"{path} is not owned by you; refusing to load"])
    if st.st_mode & 0o022:
        raise ConfigError([f"{path} is group/world-writable; refusing to load "
                           "(run: chmod go-w on it)"])


def load(path: Path, *, must_exist: bool = False) -> Config:
    if not path.exists():
        if must_exist:
            raise ConfigError([f"{path} does not exist"])
        return Config()
    check_permissions(path)
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"{path}: {e}"]) from e
    return parse(data)
