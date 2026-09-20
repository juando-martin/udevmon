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


def render_device(name, match, description="") -> str:
    lines = [f"[devices.{name}]"]
    if description:
        lines.append(f"description = {_s(description)}")
    inner = ", ".join(f"{_key(k)} = {_s(v)}" for k, v in match.items())
    lines.append(f"match = {{ {inner} }}")
    return "\n".join(lines) + "\n"


def render_action(name, device, on, run, timeout=None) -> str:
    lines = ["[[actions]]", f"name = {_s(name)}", f"device = {_s(device)}",
             "on = [" + ", ".join(_s(k) for k in on) + "]",
             "run = [" + ", ".join(_s(a) for a in run) + "]"]
    if timeout is not None:
        lines.append(f"timeout = {timeout:g}")
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


def add_device(path: Path, name, match, description="") -> None:
    if not cfgmod.NAME_RE.match(name):
        raise cfgmod.ConfigError([f"invalid device name {name!r} (use letters, digits, _ . -)"])
    existing = cfgmod.load(path)
    if name in existing.devices:
        raise cfgmod.ConfigError([f"device {name!r} already exists"])
    _edit(path, _append(path, render_device(name, match, description)),
          lambda c: c.devices[name])


def add_action(path: Path, name, device, on, run, timeout=None) -> str:
    existing = cfgmod.load(path)
    if not name:
        base = f"{device}-{'-'.join(on)}"
        name, n = base, 2
        taken = {a.name for a in existing.actions}
        while name in taken:
            name, n = f"{base}-{n}", n + 1
    elif name in {a.name for a in existing.actions}:
        raise cfgmod.ConfigError([f"action {name!r} already exists"])
    _edit(path, _append(path, render_action(name, device, on, run, timeout)),
          lambda c: [a for a in c.actions if a.name == name][0])
    return name


def _remove_block(text: str, header_re: str, extra_re: str | None = None) -> str | None:
    lines = text.splitlines(keepends=True)
    start = None
    for i, line in enumerate(lines):
        if re.match(header_re, line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if re.match(r"\[", lines[j]):
            end = j
            break
    while start > 0 and lines[start - 1].strip() == "":
        start -= 1
    return "".join(lines[:start] + lines[end:])


def remove_device(path: Path, name: str) -> None:
    cfg = cfgmod.load(path, must_exist=True)
    if name not in cfg.devices:
        raise cfgmod.ConfigError([f"no such device {name!r}"])
    users = [a.name for a in cfg.actions if a.device == name]
    if users:
        raise cfgmod.ConfigError([f"device {name!r} is used by action(s): {', '.join(users)}"])
    text = path.read_text()
    new = _remove_block(text, rf"\[devices\.(?:{re.escape(name)}|\"{re.escape(name)}\")\]\s*$")
    if new is None:
        raise cfgmod.ConfigError([f"could not locate [devices.{name}] block to remove"])

    def verify(c):
        if name in c.devices:
            raise cfgmod.ConfigError([f"removal of {name!r} did not take effect"])
    _edit(path, new, verify)


def remove_action(path: Path, name: str) -> None:
    cfg = cfgmod.load(path, must_exist=True)
    if name not in {a.name for a in cfg.actions}:
        raise cfgmod.ConfigError([f"no such action {name!r}"])
    text = path.read_text()
    lines = text.splitlines(keepends=True)
    # find the [[actions]] block whose name line matches (explicit names only)
    starts = [i for i, l in enumerate(lines) if re.match(r"\[\[actions\]\]\s*$", l)]
    for s in starts:
        e = next((j for j in range(s + 1, len(lines)) if lines[j].startswith("[")), len(lines))
        if any(re.match(rf"\s*name\s*=\s*(\"{re.escape(name)}\"|'{re.escape(name)}')\s*$", l)
               for l in lines[s:e]):
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

