# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""The JSON that ``--json`` prints for the configuration and for ``check``.

Every entry says which file defines it (``file``, an absolute path) and, for an entry
of a drop-in, the name ``--file`` takes for it (``dropin``; None for the main file),
so a tool can tell its own entries from the user's.
"""

from __future__ import annotations

from . import config as cfgmod
from .matching import matches, strength


def where(cfg: cfgmod.Config, kind: str, name: str) -> dict:
    """``{"file": PATH, "dropin": NAME or None}`` for one entry."""
    path = cfg.source(kind, name) or cfg.main
    dropin = None
    if path is not None and cfg.main is not None and path != cfg.main \
            and path.parent == cfgmod.dropin_dir(cfg.main):
        dropin = path.name[:-len(".toml")]
    return {"file": str(path) if path is not None else None, "dropin": dropin}


def rule_tags(cfg: cfgmod.Config, device: cfgmod.Device) -> list[str]:
    """Rule tags the device's match keys alone satisfy (minus its explicit tags and its
    opt-outs). A present thing can get more from its other properties."""
    return [t.name for t in cfg.tags.values()
            if t.match and t.name not in device.tags and t.name not in device.exclude_tags
            and matches(t.match, device.match, wild=True)]


def device_actions(cfg: cfgmod.Config, device: cfgmod.Device) -> list[str]:
    """Actions that target the device by name or through one of its explicit tags."""
    return [x.name for x in cfg.actions
            if device.name in x.devices or set(x.tags) & set(device.tags)]


def device_entry(cfg: cfgmod.Config, device: cfgmod.Device) -> dict:
    """One device, for ``devices list --json`` and ``devices show --json``."""
    return {"name": device.name, "description": device.description,
            "match": dict(device.match), "strength": strength(device.match),
            "status": device.status, "tags": list(device.tags),
            "rule_tags": rule_tags(cfg, device), "exclude_tags": list(device.exclude_tags),
            "vars": dict(device.vars), "actions": device_actions(cfg, device),
            **where(cfg, "device", device.name)}


def tag_entry(cfg: cfgmod.Config, name: str) -> dict:
    """One tag, declared or only used by devices and actions, for ``tags list --json``."""
    tag = cfg.tags.get(name)
    rule = {k: list(v) for k, v in tag.match.items()} if tag else {}
    return {"name": name, "description": tag.description if tag else "",
            "declared": tag is not None, "rule": rule,
            "strength": strength(tag.match, wild=True) if tag and tag.match else None,
            "vars": dict(tag.vars) if tag else {},
            "members": [d.name for d in cfg.devices.values() if name in d.tags],
            "actions": [x.name for x in cfg.actions if name in x.tags],
            **(where(cfg, "tag", name) if tag else {"file": None, "dropin": None})}


def action_entry(cfg: cfgmod.Config, action: cfgmod.Action) -> dict:
    """One action, for ``actions list --json``. Every field is given, defaults included,
    with ``program``: the file the daemon would start (``config.program_lookup``, which
    ``program_lookup`` names)."""
    lookup, program = cfgmod.program_lookup(cfg.settings, action)
    return {"name": action.name, "devices": list(action.devices), "tags": list(action.tags),
            "tag_mode": action.tag_mode, "on": list(action.on), "run": list(action.run),
            "timeout": action.timeout, "allow_unnamed": action.allow_unnamed,
            "enabled": action.enabled, "detach": action.detach, "lock": action.lock,
            "retries": action.retries, "retry_delay": action.retry_delay,
            "retry_backoff": action.retry_backoff,
            "order": action.order, "notify_on_failure": action.notify_on_failure,
            "env": dict(action.env), "cwd": action.cwd,
            "script_dir": list(action.script_dir), "program": program,
            "program_lookup": lookup, **where(cfg, "action", action.name)}


def file_entries(cfg: cfgmod.Config) -> list[dict]:
    """The files read, in order, with how many entries each defines."""
    out = []
    for f in cfg.files:
        count = {kind: sum(1 for (k, _), src in cfg.sources.items() if k == kind and src == f)
                 for kind in ("device", "tag", "action")}
        dropin = None if f == cfg.main else f.name[:-len(".toml")]
        out.append({"file": str(f), "dropin": dropin, "devices": count["device"],
                    "tags": count["tag"], "actions": count["action"]})
    return out
