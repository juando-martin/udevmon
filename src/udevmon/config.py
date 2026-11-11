# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Config loading, validation and placeholder handling."""

from __future__ import annotations

import os
import re
import shlex
import shutil
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from .events import KNOWN_KEYS, TRUSTED_PLACEHOLDERS, VALID_KINDS, VOLATILE_KEYS, is_raw, is_variable
from .matching import STRONG, WEAK, has_wildcard, matches, strength

try:
    import tomllib
except ModuleNotFoundError:                 # Python < 3.11: same parser, packaged as tomli
    import tomli as tomllib

#: What a device, tag, action or lock name may contain.
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
#: What NAME may be in a device variable (``{var_NAME}``).
VAR_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class ConfigError(Exception):
    """An invalid configuration, or an edit that cannot be made.

    ``problems`` holds each message; ``str()`` joins them with semicolons.
    """
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("; ".join(problems))


def default_config_path() -> Path:
    """``$XDG_CONFIG_HOME/udevmon/config.toml`` (``~/.config`` by default). Reads nothing."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "udevmon" / "config.toml"


@dataclass(frozen=True)
class Device:
    """A named thing (spec 5): a name, how to recognise it, and its explicit tags."""
    name: str                       #: its name, the key of ``[devices.NAME]``
    match: dict[str, str]           #: property -> exact value; all must hold
    description: str = ""           #: free text for people
    tags: tuple[str, ...] = ()      #: its explicit tags
    status: str = "active"          #: ``active``, ``disabled`` or ``ignored`` (spec 5.7)
    exclude_tags: tuple[str, ...] = ()   #: rule tags this device opts out of (spec 5.4)
    #: ``{var_NAME}`` values for its actions (spec 5.8)
    vars: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Tag:
    """A label for a set of things. ``match`` (a rule) may be empty: explicit-only tag."""
    name: str                       #: its name, the key of ``[tags.NAME]``
    #: the rule, property -> accepted values (wildcards allowed); empty for explicit-only
    match: dict[str, tuple[str, ...]] = field(default_factory=dict)
    description: str = ""           #: free text for people


@dataclass(frozen=True)
class Action:
    """One ``[[actions]]`` entry: the command ``run`` for the events ``on`` (spec 6).

    Exactly one of ``devices`` and ``tags`` is non-empty.
    """
    name: str                       #: its own name, or one made from its target
    devices: tuple[str, ...]        #: the devices it targets (``device``)
    tags: tuple[str, ...]           #: the tags it targets (``tag``)
    on: tuple[str, ...]             #: the event kinds it runs for
    run: tuple[str, ...]            #: the command, with placeholders not yet filled in
    timeout: float = 60.0           #: seconds before the command is stopped
    #: whether an unnamed thing can run this tag action; None if not stated (acts as False)
    allow_unnamed: bool | None = None
    enabled: bool = True            #: False after ``actions disable``
    tag_mode: str = "any"           #: whether a tag action needs ``any`` of the tags, or ``all``
    detach: bool = False            #: start the command and do not wait for it (spec 7.2)
    lock: str | None = None         #: actions sharing a lock name never overlap (spec 7.3)
    retries: int = 0                #: run again after a non-zero exit, up to N more times (7.4)
    retry_delay: float = 1.0        #: seconds between attempts
    order: int = 50                 #: lower runs first within one event; on a tie, config order
    notify_on_failure: bool = False  #: a desktop notification when the last attempt fails

    @property
    def targets(self) -> tuple[str, ...]:
        """Its devices, else its tags."""
        return self.devices or self.tags


@dataclass(frozen=True)
class Settings:
    """The ``[daemon]`` table."""
    debounce: float = 1.0           #: seconds within which a repeat of an event is dropped
    #: ``subsystem/devtype`` pairs that udev reports
    udev_filters: tuple[str, ...] = ("usb/usb_device",)
    max_workers: int = 4                    #: commands that may run at the same time
    script_dir: tuple[str, ...] = ()        #: folders searched for a command given by bare name


@dataclass
class Config:
    """A loaded configuration: the main file and its drop-ins, merged.

    Built by :func:`load`, which also fills ``warnings`` and ``notes`` (what ``check``
    reports). ``sources`` says which file defines each device, tag and action.
    """
    settings: Settings = field(default_factory=Settings)    #: the ``[daemon]`` table
    devices: dict[str, Device] = field(default_factory=dict)    #: name -> device
    tags: dict[str, Tag] = field(default_factory=dict)  #: name -> declared tag
    actions: list[Action] = field(default_factory=list)     #: in the order they were read
    warnings: list[str] = field(default_factory=list)   #: problems that do not stop loading
    notes: list[str] = field(default_factory=list)      #: things worth knowing, not problems
    main: Path | None = None            #: the main config file (config.toml)
    files: list[Path] = field(default_factory=list)     #: the files read, in order
    #: ``("device" | "tag" | "action", name)`` -> the file that defines it
    sources: dict[tuple[str, str], Path] = field(default_factory=dict)

    def source(self, kind: str, name: str) -> Path | None:
        """The file that defines this device, declared tag or action."""
        return self.sources.get((kind, name))

    def label(self, kind: str, name: str) -> str:
        """Where the entry comes from, for display: "" for the main file, else the
        drop-in file as ``config.d/NAME.toml``."""
        path = self.source(kind, name)
        if path is None or self.main is None or path == self.main:
            return ""
        return file_label(self.main, path)

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
_DAEMON = {"debounce", "udev_filters", "max_workers", "script_dir"}
_DEVICE = {"description", "match", "tags", "status", "exclude_tags", "vars"}
_TAG = {"description", "match"}
_ACTION = {"name", "device", "tag", "on", "run", "timeout", "allow_unnamed", "enabled",
           "tag_mode", "detach", "lock", "retries", "retry_delay", "order",
           "notify_on_failure"}
#: An action's ``order`` when it states none.
DEFAULT_ORDER = 50


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


def parse(data: dict, origins: dict | None = None) -> Config:
    """Validate the configuration ``data`` (the main file and drop-ins already merged).

    ``origins`` labels the entries that come from a drop-in file, for the messages:
    ("device" | "tag", name) -> label, and ("action", index) -> (label, index in its file).
    """
    problems: list[str] = []
    cfg = Config()
    origins = origins or {}

    def at(kind: str, name: str) -> str:
        label = origins.get((kind, name))
        return f"{label}: " if label else ""

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
        dirs = d.get("script_dir", [])
        dirs = [dirs] if isinstance(dirs, str) else dirs
        if not (isinstance(dirs, list) and all(isinstance(x, str) and x for x in dirs)):
            raise ValueError
        cfg.settings = Settings(debounce, tuple(filters), workers, tuple(dict.fromkeys(dirs)))
    except (TypeError, ValueError):
        problems.append("[daemon]: invalid debounce/udev_filters/max_workers/script_dir")

    # ---- devices
    for name, a in (data.get("devices") or {}).items():
        where = f"{at('device', name)}[devices.{name}]"
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
        variables: dict[str, str] = {}
        if "vars" in a:
            raw = a["vars"]
            if not isinstance(raw, dict):
                problems.append(f"{where}: 'vars' must be a table of NAME = \"value\"")
                continue
            bad = [k for k in raw if not VAR_RE.match(str(k))]
            if bad:
                problems.append(f"{where}: variable name(s) {bad} must be lowercase letters, "
                                "digits and _, not starting with a digit")
                continue
            variables = {str(k): _scalar_str(v) for k, v in raw.items()}
            if any(v is None for v in variables.values()):
                problems.append(f"{where}: variable values must be strings")
                continue
        exclude: tuple[str, ...] = ()
        if "exclude_tags" in a:
            exclude = _names(a["exclude_tags"], f"{where}: 'exclude_tags'", problems) or ()
            both = sorted(set(exclude) & set(tags))
            if both:
                problems.append(f"{where}: {both} is in both 'tags' and 'exclude_tags'")
                continue
        status = a.get("status", "")
        if status in ("", "active"):                    # absent or empty: the normal state
            status = "active"
        elif status not in ("disabled", "ignored"):
            problems.append(f"{where}: 'status' must be \"disabled\", \"ignored\" or \"active\" "
                            f"(or left out), not {status!r}")
            continue
        cfg.devices[name] = Device(name, match, str(a.get("description", "")), tags, status,
                                   exclude, variables)
        if strength(match) == WEAK:
            cfg.warnings.append(f"device {name!r}: weak match {match} (matches every "
                                "device/filesystem with these values)")
        wild = sorted(k for k, v in match.items() if has_wildcard(v))
        if wild:
            cfg.warnings.append(f"device {name!r}: {wild} contain(s) * or ?, which are not "
                                "wildcards in a device match (only in a tag rule); they are "
                                "compared literally")
        volatile = sorted(set(match) & VOLATILE_KEYS)
        if volatile:
            cfg.warnings.append(f"device {name!r}: match uses volatile key(s) {volatile} "
                                "that change between plugs")

    # ---- tags (rules)
    for name, t in (data.get("tags") or {}).items():
        where = f"{at('tag', name)}[tags.{name}]"
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
        label, local = origins.get(("action", i), ("", i))
        prefix = f"{label}: " if label else ""
        where = f"{prefix}[[actions]] #{local}"
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
        where = f"{prefix}action {name!r}"
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
        tag_mode = a.get("tag_mode", "any")
        if tag_mode not in ("any", "all"):
            problems.append(f"{where}: 'tag_mode' must be \"any\" or \"all\", not {tag_mode!r}")
            continue
        if "tag_mode" in a and devices:
            problems.append(f"{where}: 'tag_mode' only applies to actions that use 'tag'")
            continue
        detach = a.get("detach", False)
        if not isinstance(detach, bool):
            problems.append(f"{where}: 'detach' must be true or false")
            continue
        if detach and "timeout" in a:
            problems.append(f"{where}: 'timeout' has no meaning with 'detach' (a detached "
                            "command is not waited for)")
            continue
        retries, retry_delay = a.get("retries", 0), a.get("retry_delay", 1.0)
        if not (isinstance(retries, int) and not isinstance(retries, bool) and retries >= 0):
            problems.append(f"{where}: 'retries' must be a whole number, 0 or more")
            continue
        if not (isinstance(retry_delay, (int, float)) and not isinstance(retry_delay, bool)
                and retry_delay > 0):
            problems.append(f"{where}: 'retry_delay' must be a number of seconds, more than 0")
            continue
        if detach and ("retries" in a or "retry_delay" in a):
            problems.append(f"{where}: 'retries' and 'retry_delay' have no meaning with "
                            "'detach' (a detached command's result is not waited for)")
            continue
        notify = a.get("notify_on_failure", False)
        if not isinstance(notify, bool):
            problems.append(f"{where}: 'notify_on_failure' must be true or false")
            continue
        if notify and detach:
            problems.append(f"{where}: 'notify_on_failure' has no meaning with 'detach' (a "
                            "detached command's result is not waited for)")
            continue
        order = a.get("order", DEFAULT_ORDER)
        if not (isinstance(order, int) and not isinstance(order, bool)):
            problems.append(f"{where}: 'order' must be a whole number (default "
                            f"{DEFAULT_ORDER}; lower runs first)")
            continue
        lock = a.get("lock")
        if "lock" in a and not (isinstance(lock, str) and NAME_RE.match(lock)):
            problems.append(f"{where}: 'lock' must be a name (letters, digits, _ . -)")
            continue
        enabled = a.get("enabled", True)
        if not isinstance(enabled, bool):
            problems.append(f"{where}: 'enabled' must be true or false")
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
        defined = {k for d in cfg.devices.values() for k in d.vars}
        unknown_vars = sorted(n for n in names if is_variable(n) and n[4:] not in defined)
        if unknown_vars:
            problems.append(f"{where}: {{{unknown_vars[0]}}} is not defined: no device has a "
                            f"variable {unknown_vars[0][4:]!r} (set one with `devices set NAME "
                            f"--var {unknown_vars[0][4:]}=VALUE`)")
        for kind in on:
            missing = ({n for n in names if not is_variable(n) and not is_raw(n)}
                       - KNOWN_KEYS[kind] - {"device", "tag", "state_dir"})
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
        cfg.actions.append(Action(name, devices, tags, on, tuple(run), timeout, allow, enabled,
                                  tag_mode, detach, lock, retries, float(retry_delay), order,
                                  notify))

    if problems:
        raise ConfigError(problems)
    _advise(cfg, origins)
    return cfg


def _advise(cfg: Config, origins: dict | None = None) -> None:
    """Warnings and notes that do not stop the config from loading (spec 6.2).

    ``origins`` is :func:`parse`'s: the drop-in label of each entry not in the main file.
    """
    origins = origins or {}
    carried = {t for d in cfg.devices.values() for t in d.tags}
    used = {t for a in cfg.actions for t in a.tags}
    for d in cfg.devices.values():
        if d.status == "disabled":
            cfg.notes.append(f"device {d.name!r} is disabled: its own actions do not run "
                             "(actions for its tags still do)")
        elif d.status == "ignored":
            cfg.notes.append(f"device {d.name!r} is ignored: nothing runs for it")
    for a in cfg.actions:
        if not a.enabled:
            cfg.notes.append(f"action {a.name!r} is disabled and never runs")
        elif a.devices and all(cfg.devices[n].status != "active" for n in a.devices):
            cfg.notes.append(f"action {a.name!r} only targets disabled or ignored devices, "
                             "so it never runs")
    for a in cfg.actions:
        if not a.tags:
            continue
        if a.tag_mode == "all" and len(a.tags) == 1:
            cfg.notes.append(f"action {a.name!r}: tag_mode = \"all\" with one tag is the same as "
                             "the default")
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
        used_vars = sorted(n[4:] for n in names if is_variable(n))
        if used_vars and a.allow_unnamed:
            cfg.warnings.append(f"action {a.name!r}: uses variable(s) {used_vars}, which unnamed "
                                "things do not have; it is skipped for them")
        if a.allow_unnamed:
            if "device" in names:
                cfg.warnings.append(f"action {a.name!r}: uses {{device}}, which does not exist "
                                    "for unnamed things; it is skipped for them")
            hw = sorted(n for n in names - TRUSTED_PLACEHOLDERS if not is_variable(n))
            if hw:
                cfg.warnings.append(f"action {a.name!r}: allow_unnamed is true and it uses "
                                    f"hardware-supplied value(s) {hw} that the maker of an "
                                    "unvetted device controls")
            for t in rule_tags:
                if strength(cfg.tags[t].match, wild=True) != STRONG:
                    cfg.notes.append(f"action {a.name!r}: tag {t!r} has a rule that is not "
                                     "strong; any hardware presenting these properties can "
                                     "trigger this action")
    for a in cfg.actions:
        used_vars = sorted({n[4:] for arg in a.run for n in placeholders(arg) if is_variable(n)})
        if not used_vars:
            continue
        targets = ([cfg.devices[n] for n in a.devices] if a.devices else
                   [d for d in cfg.devices.values() if set(d.tags) & set(a.tags)])
        for d in targets:
            lacking = [v for v in used_vars if v not in d.vars]
            if lacking:
                cfg.warnings.append(f"action {a.name!r} uses variable(s) {lacking} that device "
                                    f"{d.name!r} does not define; it is skipped for that device")
    for t in sorted(carried - used):
        cfg.notes.append(f"tag {t!r} is set on devices but no action uses it")
    for d in cfg.devices.values():
        if "port_path" in d.match:
            cfg.notes.append(f"device {d.name!r} matches by port_path: it only matches while "
                             "plugged into that USB port (another port or a hub stops it), and "
                             "any device of the same model in that port matches")
    for t in cfg.tags.values():
        if "port_path" in t.match:
            cfg.notes.append(f"tag rule {t.name!r} uses port_path: it only selects things in "
                             "that USB port")
    for d in cfg.devices.values():
        for t in d.exclude_tags:
            if t not in cfg.tags or not cfg.tags[t].match:
                cfg.notes.append(f"device {d.name!r} excludes tag {t!r}, which has no rule, so "
                                 "there is nothing to opt out of (explicit tags are not "
                                 "affected)")
            elif not _rule_can_match(cfg.tags[t].match, d.match):
                cfg.notes.append(f"device {d.name!r} excludes tag {t!r}, whose rule can never "
                                 "match it (they need different values), so there is nothing "
                                 "to opt out of")
    _overlap_notes(cfg, origins)
    _lock_notes(cfg)


def _rule_can_match(rule: Mapping, match: Mapping[str, str]) -> bool:
    """False when a tag ``rule`` and a device ``match`` need different values for a key
    both name, so no thing can satisfy both; True otherwise."""
    return all(matches({k: rule[k]}, {k: match[k]}, wild=True) for k in rule if k in match)


def _overlap_notes(cfg: Config, origins: dict) -> None:
    """Notes for two devices one of which matches everything the other does (spec 6.2)."""
    devices = list(cfg.devices.values())
    for i, a in enumerate(devices):
        for b in devices[i + 1:]:
            a_in_b = a.match.items() <= b.match.items()
            b_in_a = b.match.items() <= a.match.items()
            if not (a_in_b or b_in_a):
                continue
            if a_in_b and b_in_a:
                what = f"devices {a.name!r} and {b.name!r} have the same match, so they always " \
                       "match together"
                ignored = [x.name for x in (a, b) if x.status == "ignored"]
            else:
                broad, narrow = (a, b) if a_in_b else (b, a)
                what = f"device {narrow.name!r} never matches alone: its match contains " \
                       f"all of {broad.name!r}'s, so whatever it matches, {broad.name!r} does too"
                ignored = [broad.name] if broad.status == "ignored" else []
                if narrow.status == "ignored":
                    continue                     # ignoring some of a broad device's things
            if ignored:
                cfg.notes.append(f"{what}; {ignored[0]!r} is ignored, so nothing ever runs for "
                                 f"{(b if ignored[0] == a.name else a).name!r}")
                continue
            note = f"{what}; tag actions get {{device}} = {a.name!r}, the first in file order"
            files = [origins.get(("device", x.name)) or "the main file" for x in (a, b)]
            if files[0] != files[1]:
                note += f" ({files[0]}, then {files[1]}: the drop-in file names decide it)"
            cfg.notes.append(note)


def _lock_notes(cfg: Config) -> None:
    """A note for a ``lock`` name only one action uses (spec 6.2), with a likely typo."""
    import difflib
    users: dict[str, list[str]] = {}
    for a in cfg.actions:
        if a.lock:
            users.setdefault(a.lock, []).append(a.name)
    for lock, names in users.items():
        if len(names) != 1:
            continue
        note = (f"action {names[0]!r} is the only one with lock {lock!r}, so it only keeps "
                "that action's own runs from overlapping")
        close = difflib.get_close_matches(lock, [k for k in users if k != lock], n=1)
        if close:
            note += f" (a typo for {close[0]!r}?)"
        cfg.notes.append(note)


#: Folders searched for a command given by bare name, after ``script_dir``.
SYSTEM_PATHS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")


def find_program(name: str, dirs) -> str | None:
    """The first executable file called ``name`` in ``dirs`` (``~`` expanded), or None."""
    for d in dirs:
        candidate = os.path.join(os.path.expanduser(d), name)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def resolve_program(settings: Settings, program: str) -> str | None:
    """Where a command given by bare name is found when ``script_dir`` is set: the folders
    in order, then only the standard system paths (the daemon's own PATH is ignored, so
    it behaves the same under systemd and in a terminal). None when it is not found.

    Only meaningful when ``settings.script_dir`` is set and ``program`` is a bare name.
    """
    return find_program(program, (*settings.script_dir, *SYSTEM_PATHS))


def uses_script_dir(settings: Settings, program: str) -> bool:
    """Whether ``program`` is looked up through ``script_dir`` (a bare name, no placeholder)."""
    return bool(settings.script_dir) and "/" not in program and "{" not in program


def _optional_missing(settings: Settings) -> list[str]:
    """``script_dir`` entries that do not exist while another entry is a folder: optional
    folders (an example's, say) that this machine does not have, worth only a note."""
    dirs = settings.script_dir
    if not any(os.path.isdir(os.path.expanduser(d)) for d in dirs):
        return []
    return [d for d in dirs if not os.path.lexists(os.path.expanduser(d))]


def command_notes(cfg: Config) -> list[str]:
    """Notes about the folders and programs found on this machine (``check`` prints them,
    the daemon logs them at the normal level)."""
    return [f"script_dir {d!r} does not exist; it is skipped (another script_dir folder does)"
            for d in _optional_missing(cfg.settings)]


def command_problems(cfg: Config, path: str | None = None) -> list[str]:
    """Actions whose program cannot be run, judged with PATH (default: this process's).

    The daemon calls this with its own environment, which for a systemd service
    is often shorter than a login shell's PATH.
    """
    problems = []
    quiet = set(_optional_missing(cfg.settings))
    for d in cfg.settings.script_dir:
        if d not in quiet and not os.path.isdir(os.path.expanduser(d)):
            problems.append(f"script_dir {d!r} is not a folder")
    for a in cfg.actions:
        prog = os.path.expanduser(a.run[0])
        if "{" in prog:                          # decided at run time
            continue
        if uses_script_dir(cfg.settings, prog):
            if resolve_program(cfg.settings, prog) is None:
                problems.append(f"action {a.name!r}: command {prog!r} was not found in "
                                "script_dir or the standard system paths")
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
    """Raise :class:`ConfigError` unless ``path`` is the user's and not group or world writable."""
    st = path.stat()
    if st.st_uid != os.getuid():
        raise ConfigError([f"{path} is not owned by you; refusing to load"])
    if st.st_mode & 0o022:
        raise ConfigError([f"{path} is group/world-writable; refusing to load "
                           "(run: chmod go-w on it)"])


# -- drop-in files -------------------------------------------------------------------
#
# Next to config.toml, config.d/*.toml are read after it, in file-name order, with the
# same schema except [daemon]. Their devices, tags and actions join the main file's; a
# name defined in two files is an error. Actions run in that order: the main file's,
# then each drop-in's.

def dropin_dir(main: Path) -> Path:
    """The folder of drop-in files for ``main``: ``config.d`` next to ``config.toml``."""
    return main.with_suffix(".d")


def file_label(main: Path, path: Path) -> str:
    """``path`` as shown to the user: the main file's name, or ``config.d/NAME.toml``."""
    if path == main:
        return main.name
    if path.parent == dropin_dir(main):
        return f"{path.parent.name}/{path.name}"
    return str(path)


#: The names of the files in ``config.d`` that are read (others, such as backups, are not).
DROPIN_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*\.toml$")


def dropin_files(main: Path) -> list[Path]:
    """The drop-in files of ``main``, in the order they are read (by file name).

    Hidden files and names other than ``NAME.toml`` (an editor's backup, udevmon's own
    ``.bak``) are not read.
    """
    folder = dropin_dir(main)
    if not folder.is_dir():
        return []
    return sorted((p for p in folder.iterdir() if DROPIN_RE.match(p.name) and p.is_file()),
                  key=lambda p: p.name)


def dropin_path(main: Path, name: str, what: str = "--file") -> Path:
    """The file meant by ``--file NAME``: the main file, or a drop-in given as ``NAME``,
    ``NAME.toml``, ``config.d/NAME.toml`` or its full path. ConfigError for anything
    else (a file that would not be read); its message starts with ``what`` and ``name``."""
    folder = dropin_dir(main)
    given = Path(os.path.expanduser(name))
    if given.is_absolute():
        if given == main:
            return main
        if given.parent != folder:
            raise ConfigError([f"{what} {name}: only {main} or a file in {folder} is read"])
        given = Path(given.name)
    if str(given) in (main.name, str(main)):
        return main
    if len(given.parts) == 2 and given.parts[0] == folder.name:
        given = Path(given.parts[1])
    file = given.name if given.suffix == ".toml" else given.name + ".toml"
    if len(given.parts) != 1 or not DROPIN_RE.match(file):
        raise ConfigError([f"{what} {name}: give a drop-in as NAME or {folder.name}/NAME.toml "
                           "(letters, digits, _ . -)"])
    return folder / file


def _read_toml(path: Path) -> dict:
    check_permissions(path)
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError([f"{path}: {e}"]) from e


def read_files(main: Path):
    """Read ``main`` and its drop-ins.

    Returns (merged data, origins for ``parse``, the files read, sources: (kind, name) ->
    file for devices and tags, the file of each action by position).
    """
    files = ([main] if main.exists() else []) + dropin_files(main)
    folder = dropin_dir(main)
    if len(files) > (1 if main.exists() else 0):
        check_permissions(folder)
    data: dict = {}
    origins: dict = {}
    sources: dict = {}
    action_files: list[Path] = []
    problems: list[str] = []
    for path in files:
        raw = _read_toml(path)
        label = file_label(main, path)
        if path == main:
            data = {k: v for k, v in raw.items() if k not in ("devices", "tags", "actions")}
        elif "daemon" in raw:
            problems.append(f"{label}: [daemon] is only read from {main.name}")
        for key in raw:
            if path != main and key not in _TOP:
                problems.append(f"{label}: top level: unknown key {key!r}")
        for key, kind in (("devices", "device"), ("tags", "tag")):
            tables = raw.get(key)
            if tables is None:
                continue
            if not isinstance(tables, dict):
                problems.append(f"{label}: '{key}' must be a table")
                continue
            merged = data.setdefault(key, {})
            for name, table in tables.items():
                if name in merged:
                    problems.append(f"{kind} {name!r} is defined in both "
                                    f"{file_label(main, sources[(kind, name)])} and {label}")
                    continue
                merged[name] = table
                sources[(kind, name)] = path
                if path != main:
                    origins[(kind, name)] = label
        actions = raw.get("actions")
        if actions is None:
            continue
        if not isinstance(actions, list):
            problems.append(f"{label}: 'actions' must be an array of tables ([[actions]])")
            continue
        merged = data.setdefault("actions", [])
        for local, action in enumerate(actions, 1):
            name = action.get("name") if isinstance(action, dict) else None
            if isinstance(name, str) and sources.get(("action", name), path) != path:
                problems.append(f"action {name!r} is defined in both "
                                f"{file_label(main, sources[('action', name)])} and {label}")
                continue
            merged.append(action)
            action_files.append(path)
            if isinstance(name, str):
                sources[("action", name)] = path
            if path != main:
                origins[("action", len(merged))] = (label, local)
    if problems:
        raise ConfigError(problems)
    sources = {k: v for k, v in sources.items() if k[0] != "action"}
    return data, origins, files, sources, action_files


def load(path: Path, *, must_exist: bool = False) -> Config:
    """The configuration of ``path`` (``config.toml``) and its drop-ins (``config.d/*.toml``).

    Only reads files. A missing main file with no drop-ins gives an empty configuration,
    unless ``must_exist``.

    Raises:
        ConfigError: a file is missing (``must_exist``), unsafe (:func:`check_permissions`)
            or invalid; every problem found is listed.
    """
    path = Path(path)
    if not path.exists() and not dropin_files(path):
        if must_exist:
            raise ConfigError([f"{path} does not exist"])
        return Config(main=path)
    data, origins, files, sources, action_files = read_files(path)
    cfg = parse(data, origins)
    cfg.main, cfg.files, cfg.sources = path, files, sources
    for action, file in zip(cfg.actions, action_files):
        cfg.sources[("action", action.name)] = file
    return cfg
