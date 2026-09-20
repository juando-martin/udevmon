"""Comment-preserving edits to the config file (append / remove whole blocks)."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

from . import config as cfgmod


def _s(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _key(k: str) -> str:
    return k if re.fullmatch(r"[A-Za-z0-9_-]+", k) else _s(k)


def _list(items) -> str:
    return "[" + ", ".join(_s(i) for i in items) + "]"


def _name_or_list(items) -> str:
    return _s(items[0]) if len(items) == 1 else _list(items)


def render_device(name, match, description="", tags=()) -> str:
    lines = [f"[devices.{name}]"]
    if description:
        lines.append(f"description = {_s(description)}")
    inner = ", ".join(f"{_key(k)} = {_s(v)}" for k, v in match.items())
    lines.append(f"match = {{ {inner} }}")
    if tags:
        lines.append(f"tags = {_list(tags)}")
    return "\n".join(lines) + "\n"


def render_tag(name, match, description="") -> str:
    """``match`` maps key -> str | list[str]."""
    lines = [f"[tags.{name}]"]
    if description:
        lines.append(f"description = {_s(description)}")
    if match:
        inner = ", ".join(f"{_key(k)} = {_name_or_list(v) if not isinstance(v, str) else _s(v)}"
                          for k, v in match.items())
        lines.append(f"match = {{ {inner} }}")
    return "\n".join(lines) + "\n"


def render_action(name, devices, tags, on, run, timeout=None, allow_unnamed=None) -> str:
    lines = ["[[actions]]", f"name = {_s(name)}"]
    lines.append(f"device = {_name_or_list(devices)}" if devices
                 else f"tag = {_name_or_list(tags)}")
    lines += [f"on = {_list(on)}", f"run = {_list(run)}"]
    if timeout is not None:
        lines.append(f"timeout = {timeout:g}")
    if allow_unnamed is not None:
        lines.append(f"allow_unnamed = {'true' if allow_unnamed else 'false'}")
    return "\n".join(lines) + "\n"


def _read(path: Path) -> str:
    """The file's text exactly as stored (no newline translation, so CRLF survives)."""
    with open(path, newline="", encoding="utf-8") as f:
        return f.read()


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _edit(path: Path, new_text: str, verify) -> None:
    """Write ``new_text`` (keeping a .bak); roll back if it fails validation."""
    old = _read(path) if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if old is not None:
        shutil.copy2(path, path.with_name(path.name + ".bak"))
    _write(path, new_text)
    try:
        verify(cfgmod.load(path))
    except Exception:
        if old is None:
            path.unlink()
        else:
            _write(path, old)
        raise


def _append(path: Path, block: str) -> str:
    old = path.read_text().rstrip("\n") if path.exists() else ""
    return (old + "\n\n" if old else "") + block


def _check_name(kind: str, name: str) -> None:
    if not cfgmod.NAME_RE.match(name):
        raise cfgmod.ConfigError([f"invalid {kind} name {name!r} (use letters, digits, _ . -)"])


def add_device(path: Path, name, match, description="", tags=()) -> None:
    _check_name("device", name)
    for t in tags:
        _check_name("tag", t)
    existing = cfgmod.load(path)
    if name in existing.devices:
        raise cfgmod.ConfigError([f"device {name!r} already exists"])
    _edit(path, _append(path, render_device(name, match, description, list(dict.fromkeys(tags)))),
          lambda c: c.devices[name])


def add_tag(path: Path, name, match, description="") -> None:
    _check_name("tag", name)
    existing = cfgmod.load(path)
    if name in existing.tags:
        raise cfgmod.ConfigError([f"tag {name!r} already declared"])
    _edit(path, _append(path, render_tag(name, match, description)), lambda c: c.tags[name])


def add_action(path: Path, name, devices, tags, on, run, timeout=None, allow_unnamed=None) -> str:
    existing = cfgmod.load(path)
    if not name:
        base = f"{(devices or tags)[0]}-{'-'.join(on)}"
        name, n = base, 2
        taken = {a.name for a in existing.actions}
        while name in taken:
            name, n = f"{base}-{n}", n + 1
    elif name in {a.name for a in existing.actions}:
        raise cfgmod.ConfigError([f"action {name!r} already exists"])
    _edit(path, _append(path, render_action(name, devices, tags, on, run, timeout, allow_unnamed)),
          lambda c: [a for a in c.actions if a.name == name][0])
    return name


# -- edits to existing entries ---------------------------------------------------------
#
# These go through tomlkit, which keeps comments and layout whatever way the file was
# written by hand (sub-tables, multi-line arrays, commented headers, ...). The `add`
# commands above keep appending text in the CLI's own style. Every edit is still
# re-parsed with the strict reader and rolled back if the result is not what was asked.

def _tomlkit():
    try:
        import tomlkit
    except ImportError:
        raise cfgmod.ConfigError(["changing an existing entry needs the 'tomlkit' package "
                                  "(pip install tomlkit); adding new ones does not"]) from None
    return tomlkit


def _edited(path: Path, change) -> str:
    """The file's text after ``change(doc)`` has been applied to its tomlkit document."""
    tk = _tomlkit()
    doc = tk.parse(_read(path))
    try:
        change(doc)
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise cfgmod.ConfigError([f"could not find that entry in {path} ({type(e).__name__}); "
                                  "edit the file by hand"]) from None
    return tk.dumps(doc)


def remove_device(path: Path, name: str) -> None:
    cfg = cfgmod.load(path, must_exist=True)
    if name not in cfg.devices:
        raise cfgmod.ConfigError([f"no such device {name!r}"])
    users = [a.name for a in cfg.actions if name in a.devices]
    if users:
        raise cfgmod.ConfigError([f"device {name!r} is used by action(s): {', '.join(users)}"])

    def verify(c):
        if name in c.devices:
            raise cfgmod.ConfigError([f"removal of {name!r} did not take effect"])
    _edit(path, _edited(path, lambda doc: doc["devices"].__delitem__(name)), verify)


def remove_tag(path: Path, name: str) -> None:
    """Remove a declared ``[tags.NAME]`` block (explicit tags on devices are untouched)."""
    cfg = cfgmod.load(path, must_exist=True)
    if name not in cfg.tags:
        raise cfgmod.ConfigError([f"no declared tag {name!r} (tags set only on devices have "
                                  "no block; use `devices untag`)"])
    users = [a.name for a in cfg.actions if name in a.tags]
    if users:
        raise cfgmod.ConfigError([f"tag {name!r} is used by action(s): {', '.join(users)}"])

    def verify(c):
        if name in c.tags:
            raise cfgmod.ConfigError([f"removal of {name!r} did not take effect"])
    _edit(path, _edited(path, lambda doc: doc["tags"].__delitem__(name)), verify)


def remove_action(path: Path, name: str) -> None:
    cfg = cfgmod.load(path, must_exist=True)
    if name not in {a.name for a in cfg.actions}:
        raise cfgmod.ConfigError([f"no such action {name!r}"])

    def change(doc):
        actions = doc["actions"]
        for i, action in enumerate(actions):
            if action.get("name") == name:                  # explicit names only
                del actions[i]
                return
        raise cfgmod.ConfigError([f"action {name!r} has no explicit name in the file; "
                                  "edit config.toml by hand to remove it"])

    def verify(c):
        if name in {a.name for a in c.actions}:
            raise cfgmod.ConfigError([f"removal of {name!r} did not take effect"])
    _edit(path, _edited(path, change), verify)


def set_device_tags(path: Path, name: str, add=(), remove=()) -> None:
    """Add/remove explicit tags on a device, keeping however its ``tags`` array is written."""
    cfg = cfgmod.load(path, must_exist=True)
    device = cfg.devices.get(name)
    if device is None:
        raise cfgmod.ConfigError([f"no such device {name!r}"])
    for t in add:
        _check_name("tag", t)
    missing = [t for t in remove if t not in device.tags]
    if missing:
        raise cfgmod.ConfigError([f"device {name!r} has no explicit tag(s) {missing} "
                                  "(tags that come from a rule cannot be removed here)"])
    new_tags = [t for t in device.tags if t not in remove]
    new_tags += [t for t in dict.fromkeys(add) if t not in new_tags]

    def change(doc):
        table = doc["devices"][name]
        if not new_tags:
            table.pop("tags", None)                         # never leave an emptied array behind
            return
        current = table.get("tags")
        if hasattr(current, "append") and hasattr(current, "remove"):
            for t in remove:                                # edit in place: keeps its layout
                current.remove(t)
            for t in new_tags:
                if t not in current:
                    current.append(t)
        else:                                               # no tags yet (or a bare string)
            array = _tomlkit().array()
            for t in new_tags:
                array.append(t)
            table["tags"] = array

    def verify(c):
        if list(c.devices[name].tags) != new_tags:
            raise cfgmod.ConfigError([f"tag edit on {name!r} did not take effect"])
    _edit(path, _edited(path, change), verify)
