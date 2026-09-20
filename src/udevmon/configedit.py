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


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def _edit(path: Path, new_text: str, verify) -> None:
    """Write ``new_text`` (keeping a .bak); roll back if it fails validation."""
    old = path.read_text() if path.exists() else None
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


# What a table header can look like in a hand-written file: indented, spaces inside
# the brackets, the key bare or in either kind of quotes, and a comment after it.
_TRAILER = r"\s*(?:#.*)?$"
_ANY_HEADER = re.compile(r"^\s*\[")


def _table_header(table: str, name: str) -> re.Pattern:
    """Matches the header line of ``[table.name]``."""
    n = re.escape(name)
    return re.compile(rf"^\s*\[\s*{table}\s*\.\s*(?:{n}|\"{n}\"|'{n}')\s*\]{_TRAILER}")


_ACTIONS_HEADER = re.compile(rf"^\s*\[\[\s*actions\s*\]\]{_TRAILER}")


def _block_end(lines: list[str], start: int) -> int:
    """Index of the first line after the block that starts at ``start`` (the next header)."""
    return next((j for j in range(start + 1, len(lines)) if _ANY_HEADER.match(lines[j])),
                len(lines))


def _remove_block(text: str, header: re.Pattern) -> str | None:
    lines = text.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if header.match(line)), None)
    if start is None:
        return None
    end = _block_end(lines, start)
    while start > 0 and lines[start - 1].strip() == "":
        start -= 1
    return "".join(lines[:start] + lines[end:])


def remove_device(path: Path, name: str) -> None:
    cfg = cfgmod.load(path, must_exist=True)
    if name not in cfg.devices:
        raise cfgmod.ConfigError([f"no such device {name!r}"])
    users = [a.name for a in cfg.actions if name in a.devices]
    if users:
        raise cfgmod.ConfigError([f"device {name!r} is used by action(s): {', '.join(users)}"])
    text = path.read_text()
    new = _remove_block(text, _table_header("devices", name))
    if new is None:
        raise cfgmod.ConfigError([f"could not locate [devices.{name}] block to remove"])

    def verify(c):
        if name in c.devices:
            raise cfgmod.ConfigError([f"removal of {name!r} did not take effect"])
    _edit(path, new, verify)


def remove_tag(path: Path, name: str) -> None:
    """Remove a declared ``[tags.NAME]`` block (explicit tags on devices are untouched)."""
    cfg = cfgmod.load(path, must_exist=True)
    if name not in cfg.tags:
        raise cfgmod.ConfigError([f"no declared tag {name!r} (tags set only on devices have "
                                  "no block; use `devices untag`)"])
    users = [a.name for a in cfg.actions if name in a.tags]
    if users:
        raise cfgmod.ConfigError([f"tag {name!r} is used by action(s): {', '.join(users)}"])
    new = _remove_block(path.read_text(), _table_header("tags", name))
    if new is None:
        raise cfgmod.ConfigError([f"could not locate [tags.{name}] block to remove"])

    def verify(c):
        if name in c.tags:
            raise cfgmod.ConfigError([f"removal of {name!r} did not take effect"])
    _edit(path, new, verify)


def set_device_tags(path: Path, name: str, add=(), remove=()) -> None:
    """Add/remove explicit tags on a device by editing its single ``tags = [...]`` line."""
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

    lines = path.read_text().splitlines(keepends=True)
    header = _table_header("devices", name)
    start = next((i for i, l in enumerate(lines) if header.match(l)), None)
    if start is None:
        raise cfgmod.ConfigError([f"could not locate [devices.{name}] block"])
    end = _block_end(lines, start)
    idx = next((i for i in range(start + 1, end) if re.match(r"\s*tags\s*=", lines[i])), None)
    if idx is not None and not re.match(r"\s*tags\s*=\s*\[.*\]\s*(#.*)?$", lines[idx]):
        raise cfgmod.ConfigError([f"the tags of {name!r} span several lines; edit the file by hand"])
    new_line = f"tags = {_list(new_tags)}\n"
    if idx is not None:
        lines[idx:idx + 1] = [new_line] if new_tags else []
    elif new_tags:
        last = max(i for i in range(start, end) if lines[i].strip())
        if not lines[last].endswith("\n"):
            lines[last] += "\n"
        lines.insert(last + 1, new_line)

    def verify(c):
        if list(c.devices[name].tags) != new_tags:
            raise cfgmod.ConfigError([f"tag edit on {name!r} did not take effect"])
    _edit(path, "".join(lines), verify)


def remove_action(path: Path, name: str) -> None:
    cfg = cfgmod.load(path, must_exist=True)
    if name not in {a.name for a in cfg.actions}:
        raise cfgmod.ConfigError([f"no such action {name!r}"])
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    # find the [[actions]] block whose name line matches (explicit names only)
    starts = [i for i, l in enumerate(lines) if _ACTIONS_HEADER.match(l)]
    name_line = re.compile(rf"\s*name\s*=\s*(\"{re.escape(name)}\"|'{re.escape(name)}'){_TRAILER}")
    for s in starts:
        e = _block_end(lines, s)
        if any(name_line.match(l) for l in lines[s:e]):
            b = s
            while b > 0 and lines[b - 1].strip() == "":
                b -= 1
            new = "".join(lines[:b] + lines[e:])

            def verify(c):
                if name in {a.name for a in c.actions}:
                    raise cfgmod.ConfigError([f"removal of {name!r} did not take effect"])
            _edit(path, new, verify)
            return
    raise cfgmod.ConfigError([f"action {name!r} has no explicit name in the file; "
                              "edit config.toml by hand to remove it"])

