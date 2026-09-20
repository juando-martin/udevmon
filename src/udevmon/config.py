"""Config loading, validation and placeholder handling."""

from __future__ import annotations

import os
import re
import shlex
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .events import KNOWN_KEYS, TRUSTED_PLACEHOLDERS, VALID_KINDS, VOLATILE_KEYS
from .matching import STRONG, WEAK, strength

try:
    import tomllib
except ModuleNotFoundError:                 # Python < 3.11: same parser, packaged as tomli
    import tomli as tomllib

NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def default_config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "udevmon" / "config.toml"


@dataclass(frozen=True)
class Device:
    """A named thing (spec 5): a name, how to recognise it, and its explicit tags."""
    name: str
    match: dict[str, str]
    description: str = ""
    tags: tuple[str, ...] = ()


@dataclass(frozen=True)
class Tag:
    """A label for a set of things. ``match`` (a rule) may be empty: explicit-only tag."""
    name: str
    match: dict[str, tuple[str, ...]] = field(default_factory=dict)
    description: str = ""


@dataclass(frozen=True)
class Action:
    name: str
    devices: tuple[str, ...]        # exactly one of devices / tags is non-empty
    tags: tuple[str, ...]
    on: tuple[str, ...]
    run: tuple[str, ...]
    timeout: float = 60.0
    allow_unnamed: bool | None = None   # tag actions only; None = not stated (behaves as False)

    @property
    def targets(self) -> tuple[str, ...]:
        return self.devices or self.tags


@dataclass(frozen=True)
class Settings:
    debounce: float = 1.0
    udev_filters: tuple[str, ...] = ("usb/usb_device",)
    max_workers: int = 4


@dataclass
class Config:
    settings: Settings = field(default_factory=Settings)
    devices: dict[str, Device] = field(default_factory=dict)
    tags: dict[str, Tag] = field(default_factory=dict)
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def all_tag_names(self) -> set[str]:
        """Every tag that exists: declared, or carried by a device."""
        return set(self.tags) | {t for d in self.devices.values() for t in d.tags}


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


_TOP = {"daemon", "devices", "tags", "actions"}
_DAEMON = {"debounce", "udev_filters", "max_workers"}
_DEVICE = {"description", "match", "tags"}
_TAG = {"description", "match"}
_ACTION = {"name", "device", "tag", "on", "run", "timeout", "allow_unnamed"}


def _names(value, where: str, problems: list[str]) -> tuple[str, ...] | None:
    """A name or a non-empty list of names -> tuple, or None (problem recorded)."""
    items = [value] if isinstance(value, str) else value
    if not (isinstance(items, list) and items
            and all(isinstance(v, str) and NAME_RE.match(v) for v in items)):
        problems.append(f"{where}: must be a name or a non-empty list of names")
        return None
    return tuple(dict.fromkeys(items))          # de-duplicate, keep order


def _scalar_str(v) -> str | None:
    return str(v) if isinstance(v, (str, int)) and not isinstance(v, bool) else None


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

    # ---- devices
    for name, a in (data.get("devices") or {}).items():
        where = f"[devices.{name}]"
        if not NAME_RE.match(name):
            problems.append(f"{where}: invalid name")
            continue
        if not isinstance(a, dict):
            problems.append(f"{where}: must be a table")
            continue
        unknown(where, a, _DEVICE)
        m = a.get("match")
        if not isinstance(m, dict) or not m:
            problems.append(f"{where}: 'match' must be a non-empty table")
            continue
        match = {str(k): _scalar_str(v) for k, v in m.items()}
        if any(v is None for v in match.values()):
            problems.append(f"{where}: match values must be strings")
            continue
        tags: tuple[str, ...] = ()
        if "tags" in a:
            tags = _names(a["tags"], f"{where}: 'tags'", problems) or ()
        cfg.devices[name] = Device(name, match, str(a.get("description", "")), tags)
        if strength(match) == WEAK:
            cfg.warnings.append(f"device {name!r}: weak match {match} (matches every "
                                "device/filesystem with these values)")
        volatile = sorted(set(match) & VOLATILE_KEYS)
        if volatile:
            cfg.warnings.append(f"device {name!r}: match uses volatile key(s) {volatile} "
                                "that change between plugs")

    # ---- tags (rules)
    for name, t in (data.get("tags") or {}).items():
        where = f"[tags.{name}]"
        if not NAME_RE.match(name):
            problems.append(f"{where}: invalid name")
            continue
        if not isinstance(t, dict):
            problems.append(f"{where}: must be a table")
            continue
        unknown(where, t, _TAG)
        rule: dict[str, tuple[str, ...]] = {}
        ok = True
        m = t.get("match", {})
        if not isinstance(m, dict):
            problems.append(f"{where}: 'match' must be a table")
            continue
        for key, value in m.items():
            values = value if isinstance(value, list) else [value]
            strs = [_scalar_str(v) for v in values]
            if not values or any(v is None for v in strs):
                problems.append(f"{where}: match value for {key!r} must be a string or a "
                                "non-empty list of strings")
                ok = False
            elif key in VOLATILE_KEYS:
                problems.append(f"{where}: key {key!r} changes between plugs and cannot be "
                                "used in a rule")
                ok = False
            else:
                rule[str(key)] = tuple(strs)
        if ok:
            cfg.tags[name] = Tag(name, rule, str(t.get("description", "")))

    # ---- actions
    seen: set[str] = set()
    for i, a in enumerate(data.get("actions") or [], 1):
        where = f"[[actions]] #{i}"
        if not isinstance(a, dict):
            problems.append(f"{where}: must be a table")
            continue
        unknown(where, a, _ACTION)
        if ("device" in a) == ("tag" in a):
            problems.append(f"{where}: needs exactly one of 'device' or 'tag'")
            continue
        devices = tags = ()
        if "device" in a:
            devices = _names(a["device"], f"{where}: 'device'", problems)
            if devices is None:
                continue
            unknown_devs = [n for n in devices if n not in cfg.devices]
            if unknown_devs:
                problems.append(f"{where}: unknown device(s) {unknown_devs}")
                continue
        else:
            tags = _names(a["tag"], f"{where}: 'tag'", problems)
            if tags is None:
                continue
        name = a.get("name") or f"{(devices or tags)[0]}-{i}"
        where = f"action {name!r}"
        if not isinstance(name, str) or not NAME_RE.match(name):
            problems.append(f"{where}: invalid name")
            continue
        if name in seen:
            problems.append(f"{where}: duplicate name")
            continue
        seen.add(name)
        allow = a.get("allow_unnamed")
        if "allow_unnamed" in a:
            if devices:
                problems.append(f"{where}: 'allow_unnamed' only applies to actions that use 'tag'")
                continue
            if not isinstance(allow, bool):
                problems.append(f"{where}: 'allow_unnamed' must be true or false")
                continue
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
        if "tag" in names and not tags:
            problems.append(f"{where}: placeholder '{{tag}}' is only available for actions that "
                            "use 'tag'")
        for kind in on:
            missing = names - KNOWN_KEYS[kind] - {"device", "tag"}
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
        cfg.actions.append(Action(name, devices, tags, on, tuple(run), timeout, allow))

    if problems:
        raise ConfigError(problems)
    _advise(cfg)
    return cfg


def _advise(cfg: Config) -> None:
    """Warnings and notes that do not stop the config from loading (spec 6.2)."""
    carried = {t for d in cfg.devices.values() for t in d.tags}
    used = {t for a in cfg.actions for t in a.tags}
    for a in cfg.actions:
        if not a.tags:
            continue
        for t in a.tags:
            rule = cfg.tags[t].match if t in cfg.tags else {}
            if t not in carried and not rule:
                cfg.warnings.append(f"action {a.name!r}: tag {t!r} is carried by no device and "
                                    "has no rule, so the action never runs")
        rule_tags = [t for t in a.tags if t in cfg.tags and cfg.tags[t].match]
        names = {n for arg in a.run for n in placeholders(arg)}
        if a.allow_unnamed is None and rule_tags:
            cfg.notes.append(f"action {a.name!r}: tag {', '.join(rule_tags)} has a rule, so "
                             "things you have not named can match it; this action runs only "
                             "for named devices. Set allow_unnamed = true or false to choose.")
        if a.allow_unnamed:
            if "device" in names:
                cfg.warnings.append(f"action {a.name!r}: uses {{device}}, which does not exist "
                                    "for unnamed things; it is skipped for them")
            hw = sorted(names - TRUSTED_PLACEHOLDERS)
            if hw:
                cfg.warnings.append(f"action {a.name!r}: allow_unnamed is true and it uses "
                                    f"hardware-supplied value(s) {hw} that the maker of an "
                                    "unvetted device controls")
            for t in rule_tags:
                if strength(cfg.tags[t].match) != STRONG:
                    cfg.notes.append(f"action {a.name!r}: tag {t!r} has a rule that is not "
                                     "strong; any hardware presenting these properties can "
                                     "trigger this action")
    for t in sorted(carried - used):
        cfg.notes.append(f"tag {t!r} is set on devices but no action uses it")


def command_problems(cfg: Config, path: str | None = None) -> list[str]:
    """Actions whose program cannot be run, judged with PATH (default: this process's).

    The daemon calls this with its own environment, which for a systemd service
    is often shorter than a login shell's PATH.
    """
    problems = []
    for a in cfg.actions:
        prog = os.path.expanduser(a.run[0])
        if "{" in prog:                          # decided at run time
            continue
        if "/" in prog:
            if not os.path.exists(prog):
                what = f"{prog!r} does not exist"
            elif not (os.path.isfile(prog) and os.access(prog, os.X_OK)):
                what = f"{prog!r} is not an executable file"
            else:
                continue
        elif shutil.which(prog, path=path) is None:
            what = f"{prog!r} was not found in PATH"
        else:
            continue
        problems.append(f"action {a.name!r}: command {what}")
    return problems


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
