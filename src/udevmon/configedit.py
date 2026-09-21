"""Comment-preserving edits to the config file (append / remove whole blocks)."""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass, field, replace
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


def render_device(name, match, description="", tags=(), status=None, exclude_tags=(),
                  variables=None) -> str:
    lines = [f"[devices.{_key(name)}]"]
    if description:
        lines.append(f"description = {_s(description)}")
    inner = ", ".join(f"{_key(k)} = {_s(v)}" for k, v in match.items())
    lines.append(f"match = {{ {inner} }}")
    if tags:
        lines.append(f"tags = {_list(tags)}")
    if exclude_tags:
        lines.append(f"exclude_tags = {_list(exclude_tags)}")
    if variables:
        inner = ", ".join(f"{_key(k)} = {_s(v)}" for k, v in variables.items())
        lines.append(f"vars = {{ {inner} }}")
    if status:
        lines.append(f"status = {_s(status)}")
    return "\n".join(lines) + "\n"


def render_tag(name, match, description="") -> str:
    """``match`` maps key -> str | list[str]."""
    lines = [f"[tags.{_key(name)}]"]
    if description:
        lines.append(f"description = {_s(description)}")
    if match:
        inner = ", ".join(f"{_key(k)} = {_name_or_list(v) if not isinstance(v, str) else _s(v)}"
                          for k, v in match.items())
        lines.append(f"match = {{ {inner} }}")
    return "\n".join(lines) + "\n"


def render_action(name, devices, tags, on, run, timeout=None, allow_unnamed=None,
                  tag_mode=None, detach=False, lock=None, retries=0, retry_delay=None) -> str:
    lines = ["[[actions]]", f"name = {_s(name)}"]
    lines.append(f"device = {_name_or_list(devices)}" if devices
                 else f"tag = {_name_or_list(tags)}")
    lines += [f"on = {_list(on)}", f"run = {_list(run)}"]
    if timeout is not None:
        lines.append(f"timeout = {timeout:g}")
    if tag_mode:
        lines.append(f"tag_mode = {_s(tag_mode)}")
    if detach:
        lines.append("detach = true")
    if lock:
        lines.append(f"lock = {_s(lock)}")
    if retries:
        lines.append(f"retries = {retries}")
    if retry_delay is not None:
        lines.append(f"retry_delay = {retry_delay:g}")
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


def _check_variable(name: str) -> None:
    if not cfgmod.VAR_RE.match(name):
        raise cfgmod.ConfigError([f"invalid variable name {name!r} (lowercase letters, digits "
                                  "and _, not starting with a digit)"])


def add_device(path: Path, name, match, description="", tags=(), status=None,
               exclude_tags=(), variables=None) -> None:
    _check_name("device", name)
    for t in (*tags, *exclude_tags):
        _check_name("tag", t)
    for k in variables or {}:
        _check_variable(k)
    existing = cfgmod.load(path)
    if name in existing.devices:
        raise cfgmod.ConfigError([f"device {name!r} already exists"])
    _edit(path, _append(path, render_device(name, match, description,
                                             list(dict.fromkeys(tags)), status,
                                             list(dict.fromkeys(exclude_tags)), variables)),
          lambda c: c.devices[name])


def add_tag(path: Path, name, match, description="") -> None:
    _check_name("tag", name)
    existing = cfgmod.load(path)
    if name in existing.tags:
        raise cfgmod.ConfigError([f"tag {name!r} already declared"])
    _edit(path, _append(path, render_tag(name, match, description)), lambda c: c.tags[name])


def add_action(path: Path, name, devices, tags, on, run, timeout=None, allow_unnamed=None,
               tag_mode=None, detach=False, lock=None, retries=0, retry_delay=None) -> str:
    existing = cfgmod.load(path)
    if not name:
        base = f"{(devices or tags)[0]}-{'-'.join(on)}"
        name, n = base, 2
        taken = {a.name for a in existing.actions}
        while name in taken:
            name, n = f"{base}-{n}", n + 1
    elif name in {a.name for a in existing.actions}:
        raise cfgmod.ConfigError([f"action {name!r} already exists"])
    _edit(path, _append(path, render_action(name, devices, tags, on, run, timeout, allow_unnamed,
                                            tag_mode, detach, lock, retries, retry_delay)),
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


def _edited(path: Path, change, what: str = "that entry") -> str:
    """The file's text after ``change(doc)`` has been applied to its tomlkit document."""
    tk = _tomlkit()
    doc = tk.parse(_read(path))
    try:
        change(doc)
    except (KeyError, IndexError, TypeError, AttributeError) as e:
        raise cfgmod.ConfigError([f"could not find {what} in the structure of {path} "
                                  f"({type(e).__name__}: {e}); edit the file by hand"]) from None
    return tk.dumps(doc)


def _find_action(doc, name: str, verb: str):
    """(index, item) of the action with this explicit name in the tomlkit document."""
    for index, action in enumerate(doc["actions"]):
        if action.get("name") == name:
            return index, action
    raise cfgmod.ConfigError([f"action {name!r} has no explicit name in the file (its name is "
                              "made up from its target). Add a line `name = \"...\"` to it, or "
                              f"edit config.toml by hand, to {verb} it"])


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
    _edit(path, _edited(path, lambda doc: doc["devices"].__delitem__(name), f"device {name!r}"),
          verify)


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
    _edit(path, _edited(path, lambda doc: doc["tags"].__delitem__(name), f"tag {name!r}"),
          verify)


def remove_action(path: Path, name: str) -> None:
    cfg = cfgmod.load(path, must_exist=True)
    if name not in {a.name for a in cfg.actions}:
        raise cfgmod.ConfigError([f"no such action {name!r}"])

    def change(doc):
        index, _ = _find_action(doc, name, "remove")
        del doc["actions"][index]

    def verify(c):
        if name in {a.name for a in c.actions}:
            raise cfgmod.ConfigError([f"removal of {name!r} did not take effect"])
    _edit(path, _edited(path, change), verify)


def _edit_device_names(path: Path, name: str, key: str, add=(), remove=()) -> None:
    """Add/remove names in a device's ``tags`` or ``exclude_tags`` array, keeping however it is
    written."""
    attr = {"tags": "tags", "exclude_tags": "exclude_tags"}[key]
    cfg = cfgmod.load(path, must_exist=True)
    device = cfg.devices.get(name)
    if device is None:
        raise cfgmod.ConfigError([f"no such device {name!r}"])
    for t in add:
        _check_name("tag", t)
    current_names = list(getattr(device, attr))
    other = device.exclude_tags if key == "tags" else device.tags
    clash = [t for t in dict.fromkeys(add) if t in other]
    if clash and key == "tags":
        raise cfgmod.ConfigError([f"device {name!r} excludes {clash}: run `devices include "
                                  f"{name} ...` first"])
    if clash:
        raise cfgmod.ConfigError([f"device {name!r} carries {clash} as an explicit tag; an "
                                  "explicit tag is never affected by an exclusion, so remove "
                                  "the tag first (`devices untag`)"])
    missing = [t for t in remove if t not in current_names]
    if missing:
        hint = (" (tags that come from a rule cannot be removed here)" if key == "tags"
                else " (nothing to include back)")
        label = "explicit tag(s)" if key == "tags" else "exclusion(s)"
        raise cfgmod.ConfigError([f"device {name!r} has no {label} {missing}{hint}"])
    new_names = [t for t in current_names if t not in remove]
    new_names += [t for t in dict.fromkeys(add) if t not in new_names]

    def change(doc):
        table = doc["devices"][name]
        if not new_names:
            table.pop(key, None)                            # never leave an emptied array behind
            return
        current = table.get(key)
        if hasattr(current, "append") and hasattr(current, "remove"):
            for t in remove:                                # edit in place: keeps its layout
                current.remove(t)
            for t in new_names:
                if t not in current:
                    current.append(t)
        else:                                               # none yet (or a bare string)
            array = _tomlkit().array()
            for t in new_names:
                array.append(t)
            table[key] = array

    def verify(c):
        if list(getattr(c.devices[name], attr)) != new_names:
            raise cfgmod.ConfigError([f"the {key} edit on {name!r} did not take effect"])
    _edit(path, _edited(path, change), verify)


def set_device_tags(path: Path, name: str, add=(), remove=()) -> None:
    """Add/remove explicit tags on a device."""
    _edit_device_names(path, name, "tags", add, remove)


def set_device_exclusions(path: Path, name: str, add=(), remove=()) -> None:
    """Add/remove the rule tags a device opts out of (``exclude_tags``)."""
    _edit_device_names(path, name, "exclude_tags", add, remove)


def _unique(names) -> list[str]:
    return list(dict.fromkeys(names))


def set_device_status(path: Path, names, status: str | None) -> list[str]:
    """Make devices "disabled" or "ignored", or (``status=None``) active again.

    All the names are checked first, so nothing changes unless every one exists.
    Returns the names whose state actually changed.
    """
    cfg = cfgmod.load(path, must_exist=True)
    names = _unique(names)
    missing = [n for n in names if n not in cfg.devices]
    if missing:
        raise cfgmod.ConfigError([f"no such device(s): {', '.join(map(repr, missing))}"])
    want = status or "active"
    changed = [n for n in names if cfg.devices[n].status != want]

    def change(doc):
        for n in names:
            table = doc["devices"][n]
            if status is None:
                table.pop("status", None)               # active is the absence of the key
            else:
                table["status"] = status

    def verify(c):
        wrong = [n for n in names if c.devices[n].status != want]
        if wrong:
            raise cfgmod.ConfigError([f"status change on {wrong} did not take effect"])
    _edit(path, _edited(path, change), verify)
    return changed


def set_action_enabled(path: Path, names, enabled: bool) -> list[str]:
    """Enable or disable actions (``enabled = false``). Returns the names that changed."""
    cfg = cfgmod.load(path, must_exist=True)
    names = _unique(names)
    by_name = {a.name: a for a in cfg.actions}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise cfgmod.ConfigError([f"no such action(s): {', '.join(map(repr, missing))}"])
    changed = [n for n in names if by_name[n].enabled != enabled]

    def change(doc):
        for n in names:
            _, item = _find_action(doc, n, "change")
            if enabled:
                item.pop("enabled", None)
            else:
                item["enabled"] = False

    def verify(c):
        wrong = [a.name for a in c.actions if a.name in names and a.enabled != enabled]
        if wrong:
            raise cfgmod.ConfigError([f"change of {wrong} did not take effect"])
    _edit(path, _edited(path, change), verify)
    return changed


def _retarget(kind: str, what: str, current, add, remove, action: str) -> list[str]:
    """The new list of targets (``kind`` is "device" or "tag"); ConfigError if it cannot be."""
    add, remove = _unique(add), _unique(remove)
    both = [n for n in add if n in remove]
    if both:
        raise cfgmod.ConfigError([f"{both} is in both --add-{kind} and --remove-{kind}"])
    missing = [n for n in remove if n not in current]
    if missing:
        raise cfgmod.ConfigError([f"action {action!r} has no {kind}(s) {missing} "
                                  f"(it targets {what}: {', '.join(current)})"])
    new = [n for n in current if n not in remove]
    new += [n for n in add if n not in new]
    if not new:
        raise cfgmod.ConfigError([f"that would leave action {action!r} with no {kind}s; "
                                  f"remove the action instead (`actions remove {action}`)"])
    return new


def set_action_options(path: Path, name: str, *, unnamed: str | None = None,
                       on=None, timeout: float | None = None, run=None, tag_mode=None,
                       detach: bool | None = None, lock: str | None = None,
                       retries: int | None = None, retry_delay: float | None = None,
                       add_devices=(), remove_devices=(), add_tags=(), remove_tags=()):
    """Change an action's ``allow_unnamed``, events, timeout, command, lock or targets.

    ``lock`` is a lock name, or "" to remove the action's lock. ``retries`` is a count
    (0 removes the setting); ``retry_delay`` is in seconds.

    ``unnamed`` is "allow", "named-only" or "default" (back to not stated). An action
    targets devices or tags, never both, and keeps at least one. Returns the new list of
    targets when they were changed, else None.
    """
    cfg = cfgmod.load(path, must_exist=True)
    action = next((a for a in cfg.actions if a.name == name), None)
    if action is None:
        raise cfgmod.ConfigError([f"no such action {name!r}"])
    device_edit, tag_edit = bool(add_devices or remove_devices), bool(add_tags or remove_tags)
    if lock:
        _check_name("lock", lock)
    if run is not None and not (run and all(isinstance(x, str) for x in run)):
        raise cfgmod.ConfigError(["the command must not be empty"])
    if tag_mode is not None and action.devices:
        raise cfgmod.ConfigError([f"action {name!r} targets devices; --all-tags/--any-tags only "
                                  "apply to actions that use tags"])
    if (unnamed is None and on is None and timeout is None and run is None and tag_mode is None
            and detach is None and lock is None and retries is None and retry_delay is None
            and not (device_edit or tag_edit)):
        raise cfgmod.ConfigError(["nothing to change: give --allow-unnamed, --named-only, "
                                  "--default-unnamed, --on, --timeout, --add-device, "
                                  "--remove-device, --add-tag, --remove-tag, --all-tags, "
                                  "--any-tags, --detach, --no-detach, --lock, --no-lock, "
                                  "--retries, --retry-delay or a new command after --"])
    key = None
    targets = None
    if device_edit and tag_edit:
        raise cfgmod.ConfigError(["an action targets devices or tags, not both"])
    if device_edit or tag_edit:
        key = "device" if device_edit else "tag"
        other = "tag" if device_edit else "device"
        if (action.tags if device_edit else action.devices):
            raise cfgmod.ConfigError([
                f"action {name!r} targets {other}s, so it cannot take {key}s. Use "
                f"--add-{other}/--remove-{other}, or remove the action and add it again"])
        current = list(action.devices if device_edit else action.tags)
        for n in (add_devices if device_edit else add_tags):
            _check_name(key, n)
        if device_edit:
            unknown = [n for n in _unique(add_devices) if n not in cfg.devices]
            if unknown:
                raise cfgmod.ConfigError([f"no such device(s): {', '.join(map(repr, unknown))}"])
        targets = _retarget(key, f"{key}s", current,
                            add_devices if device_edit else add_tags,
                            remove_devices if device_edit else remove_tags, name)
        if targets == current:
            targets = None                                  # already as asked: no edit
            key = None
            if (unnamed is None and on is None and timeout is None and run is None
                    and tag_mode is None and detach is None and lock is None
                    and retries is None and retry_delay is None):
                return None

    def change(doc):
        _, item = _find_action(doc, name, "change")
        if unnamed == "allow":
            item["allow_unnamed"] = True
        elif unnamed == "named-only":
            item["allow_unnamed"] = False
        elif unnamed == "default":
            item.pop("allow_unnamed", None)
        if on is not None:
            array = _tomlkit().array()
            for kind in on:
                array.append(kind)
            item["on"] = array
        if timeout is not None:
            item["timeout"] = int(timeout) if float(timeout).is_integer() else float(timeout)
        if retries is not None:
            if retries:
                item["retries"] = retries
            else:
                item.pop("retries", None)
        if retry_delay is not None:
            item["retry_delay"] = int(retry_delay) if float(retry_delay).is_integer() \
                else float(retry_delay)
        if lock:
            item["lock"] = lock
        elif lock == "":
            item.pop("lock", None)
        if detach is True:
            item["detach"] = True
        elif detach is False:
            item.pop("detach", None)
        if tag_mode == "all":
            item["tag_mode"] = "all"
        elif tag_mode == "any":
            item.pop("tag_mode", None)
        if run is not None:
            array = _tomlkit().array()
            for word in run:
                array.append(word)
            item["run"] = array                             # a comment after it is kept
        if key is not None:
            current_value = item.get(key)
            if hasattr(current_value, "append") and hasattr(current_value, "remove"):
                for n in [n for n in current_value if n not in targets]:
                    current_value.remove(n)                 # in place: keeps how it is laid out
                for n in targets:
                    if n not in current_value:
                        current_value.append(n)
            elif len(targets) == 1:
                item[key] = targets[0]                      # a single name stays a plain string
            else:
                array = _tomlkit().array()
                for n in targets:
                    array.append(n)
                item[key] = array

    def verify(c):
        a = next(x for x in c.actions if x.name == name)
        want_unnamed = {"allow": True, "named-only": False, "default": None}.get(unnamed, a.allow_unnamed)
        if (a.allow_unnamed != want_unnamed
                or (on is not None and a.on != tuple(on))
                or (timeout is not None and a.timeout != float(timeout))
                or (run is not None and a.run != tuple(run))
                or (tag_mode is not None and a.tag_mode != tag_mode)
                or (detach is not None and a.detach != detach)
                or (lock is not None and a.lock != (lock or None))
                or (retries is not None and a.retries != retries)
                or (retry_delay is not None and a.retry_delay != float(retry_delay))
                or (key is not None and list(a.targets) != targets)):
            raise cfgmod.ConfigError([f"the change to {name!r} did not take effect"])
    _edit(path, _edited(path, change), verify)
    return targets


def set_device_fields(path: Path, name: str, *, match=None, description=None,
                      set_vars=None, unset_vars=()) -> None:
    """Replace a device's match keys, change its description and/or its variables.

    ``match`` replaces the whole match (a device match is exact values); an empty
    ``description`` removes the line. ``set_vars`` (name -> value) adds or changes
    variables and ``unset_vars`` removes some; the others stay.
    """
    cfg = cfgmod.load(path, must_exist=True)
    device = cfg.devices.get(name)
    if device is None:
        raise cfgmod.ConfigError([f"no such device {name!r}"])
    set_vars, unset_vars = dict(set_vars or {}), list(dict.fromkeys(unset_vars))
    if match is None and description is None and not set_vars and not unset_vars:
        raise cfgmod.ConfigError(["nothing to change: give --match, --description, --var or "
                                  "--unset-var"])
    if match is not None and not match:
        raise cfgmod.ConfigError(["a device needs at least one --match KEY=VALUE"])
    for k in set_vars:
        _check_variable(k)
    both = [k for k in unset_vars if k in set_vars]
    if both:
        raise cfgmod.ConfigError([f"{both} is in both --var and --unset-var"])
    missing = [k for k in unset_vars if k not in device.vars]
    if missing:
        raise cfgmod.ConfigError([f"device {name!r} has no variable(s) {missing}"
                                  + (f" (it has: {', '.join(device.vars)})" if device.vars
                                     else "")])
    new_vars = {k: v for k, v in device.vars.items() if k not in unset_vars}
    new_vars.update(set_vars)
    if unset_vars:                                       # a variable an action uses stays needed
        still_used = {n[4:] for a in cfg.actions for arg in a.run
                      for n in cfgmod.placeholders(arg) if n.startswith("var_")}
        gone = [k for k in unset_vars if k in still_used
                and not any(k in d.vars for n, d in cfg.devices.items() if n != name)
                and k not in new_vars]
        if gone:
            raise cfgmod.ConfigError([f"variable(s) {gone} are used by an action and no other "
                                      "device defines them; edit the action first"])

    def change(doc):
        from tomlkit.items import InlineTable
        table = doc["devices"][name]
        if match is not None:
            current = table.get("match")
            if isinstance(current, InlineTable) or current is None:
                inner = ", ".join(f"{_key(k)} = {_s(v)}" for k, v in match.items())
                fresh = _tomlkit().parse(f"match = {{ {inner} }}\n")["match"]
                if current is not None:                 # keep a comment after the old value
                    fresh.trivia.comment_ws = current.trivia.comment_ws
                    fresh.trivia.comment = current.trivia.comment
                table["match"] = fresh
            else:                                       # a [devices.x.match] sub-table
                for key in list(current.keys()):
                    del current[key]
                for key, value in match.items():
                    current[key] = value
        if description is not None:
            if description == "":
                table.pop("description", None)
            else:
                table["description"] = description
        if set_vars or unset_vars:
            if not new_vars:
                table.pop("vars", None)
                return
            current = table.get("vars")
            if current is None:
                inner = ", ".join(f"{_key(k)} = {_s(v)}" for k, v in new_vars.items())
                table["vars"] = _tomlkit().parse(f"vars = {{ {inner} }}\n")["vars"]
            else:                                       # inline table or sub-table: edit in place
                for key in unset_vars:
                    del current[key]
                for key, value in set_vars.items():
                    current[key] = value

    def verify(c):
        d = c.devices[name]
        if (match is not None and d.match != match) or \
                (description is not None and d.description != description) or \
                ((set_vars or unset_vars) and dict(d.vars) != new_vars):
            raise cfgmod.ConfigError([f"the change to {name!r} did not take effect"])
    _edit(path, _edited(path, change, f"device {name!r}"), verify)


def set_tag_fields(path: Path, name: str, *, match=None, clear_rule: bool = False,
                   description=None) -> None:
    """Replace a declared tag's rule (``match``: key -> str | list[str]), remove its rule,
    and/or change its description (an empty one removes the line)."""
    cfg = cfgmod.load(path, must_exist=True)
    if name not in cfg.tags:
        raise cfgmod.ConfigError([
            f"no declared tag {name!r} ([tags.{name}]). A tag that only devices carry needs no "
            "declaration; declare one with `tags add`"])
    if match is not None and clear_rule:
        raise cfgmod.ConfigError(["give --match or --no-rule, not both"])
    if match is None and not clear_rule and description is None:
        raise cfgmod.ConfigError(["nothing to change: give --match, --no-rule or --description"])
    if match is not None and not match:
        raise cfgmod.ConfigError(["a rule needs at least one --match KEY=VALUE "
                                  "(--no-rule removes it)"])

    def value(v):
        return _s(v) if isinstance(v, str) else _list(v)

    def change(doc):
        from tomlkit.items import InlineTable
        table = doc["tags"][name]
        if clear_rule:
            table.pop("match", None)
        elif match is not None:
            current = table.get("match")
            if isinstance(current, InlineTable) or current is None:
                inner = ", ".join(f"{_key(k)} = {value(v)}" for k, v in match.items())
                table["match"] = _tomlkit().parse(f"match = {{ {inner} }}\n")["match"]
            else:                                       # a [tags.x.match] sub-table
                for key in list(current.keys()):
                    del current[key]
                for key, v in match.items():
                    current[key] = v if isinstance(v, str) else list(v)
        if description is not None:
            if description == "":
                table.pop("description", None)
            else:
                table["description"] = description

    def verify(c):
        t = c.tags[name]
        want = None if match is None else {k: (v,) if isinstance(v, str) else tuple(v)
                                           for k, v in match.items()}
        if (clear_rule and t.match) or (want is not None and dict(t.match) != want) or \
                (description is not None and t.description != description):
            raise cfgmod.ConfigError([f"the change to tag {name!r} did not take effect"])
    _edit(path, _edited(path, change, f"tag {name!r}"), verify)


# -- renames -----------------------------------------------------------------------------

@dataclass
class RenameReport:
    updated: list[str] = field(default_factory=list)       # what referred to the old name
    generated: list[tuple[str, str]] = field(default_factory=list)   # made-up action names that changed
    mentions: list[tuple[int, str]] = field(default_factory=list)    # lines that still contain it


def _swap(container, key: str, old: str, new: str) -> None:
    """Replace ``old`` by ``new`` in container[key] (a string or an array), in place."""
    value = container.get(key)
    if value is None:
        return
    if isinstance(value, str):
        if value == old:
            container[key] = new
    else:
        for i, item in enumerate(list(value)):
            if item == old:
                value[i] = new


def _rename_headers(text: str, table: str, old: str, new: str) -> tuple[str, int]:
    """Rename ``[table.old]`` and its sub-table headers (``[table.old.x]``) in place."""
    n = re.escape(old)
    pattern = re.compile(rf"^(\s*\[\s*{table}\s*\.\s*)(?:{n}|\"{n}\"|'{n}')(\s*[.\]])", re.MULTILINE)
    return pattern.subn(lambda m: m.group(1) + _key(new) + m.group(2), text)


def _expected_after_rename(cfg, raw_actions, kind: str, old: str, new: str):
    """The parsed config as it must look once ``old`` is called ``new`` (same order)."""
    def dev(n):
        return new if kind == "devices" and n == old else n

    def tag(t):
        return new if kind == "tags" and t == old else t
    devices = [(dev(n), replace(d, name=dev(n), tags=tuple(tag(t) for t in d.tags),
                                exclude_tags=tuple(tag(t) for t in d.exclude_tags)))
               for n, d in cfg.devices.items()]
    tags = [(tag(n), replace(t, name=tag(n))) for n, t in cfg.tags.items()]
    actions = []
    for i, a in enumerate(cfg.actions, 1):
        devices_, tags_ = tuple(dev(x) for x in a.devices), tuple(tag(x) for x in a.tags)
        name = a.name if "name" in raw_actions[i - 1] else f"{(devices_ or tags_)[0]}-{i}"
        if kind == "actions" and a.name == old:
            name = new
        actions.append(replace(a, name=name, devices=devices_, tags=tags_))
    return devices, tags, actions


def _mentions(text: str, old: str) -> list[tuple[int, str]]:
    word = re.compile(rf"(?<![\w-]){re.escape(old)}(?![\w-])")
    return [(n, line.strip()) for n, line in enumerate(text.splitlines(), 1) if word.search(line)]


def _rename(path: Path, kind: str, old: str, new: str) -> RenameReport:
    singular = {"devices": "device", "tags": "tag", "actions": "action"}[kind]
    _check_name(singular, new)
    cfg = cfgmod.load(path, must_exist=True)
    existing = {"devices": set(cfg.devices), "tags": cfg.all_tag_names(),
                "actions": {a.name for a in cfg.actions}}[kind]
    if old not in existing:
        raise cfgmod.ConfigError([f"no such {singular} {old!r}"])
    if new == old:
        raise cfgmod.ConfigError([f"{new!r} is already its name"])
    if new in existing:
        raise cfgmod.ConfigError([f"{singular} {new!r} already exists"])
    raw_actions = cfgmod.tomllib.loads(_read(path)).get("actions") or []
    expected = _expected_after_rename(cfg, raw_actions, kind, old, new)

    def change(doc):
        if kind == "devices":                            # every action that names the device
            for action in doc.get("actions", []):
                _swap(action, "device", old, new)
        elif kind == "tags":                             # explicit tags on devices, tag actions
            for _, device in doc.get("devices", {}).items():
                _swap(device, "tags", old, new)
                _swap(device, "exclude_tags", old, new)
            for action in doc.get("actions", []):
                _swap(action, "tag", old, new)
        else:
            _find_action(doc, old, "rename")[1]["name"] = new

    text = _edited(path, change, f"{singular} {old!r}")
    if kind in ("devices", "tags"):
        has_block = old in (cfg.devices if kind == "devices" else cfg.tags)
        text, headers = _rename_headers(text, kind, old, new)
        if has_block and headers == 0:
            raise cfgmod.ConfigError([f"{singular} {old!r} is not written as a [{kind}.{old}] "
                                      "table (dotted keys?); rename it by hand"])

    def verify(c):
        got = (list(c.devices.items()), list(c.tags.items()), c.actions)
        if got != expected:
            raise cfgmod.ConfigError([f"renaming {old!r} would have changed something else in "
                                      "the file; nothing was written"])
    _edit(path, text, verify)

    report = RenameReport(mentions=_mentions(text, old))
    if kind == "devices":
        report.updated = [a.name for a in cfg.actions if old in a.devices]
    elif kind == "tags":
        report.updated = [f"device {d.name}" for d in cfg.devices.values() if old in d.tags]
        report.updated += [f"action {a.name}" for a in cfg.actions if old in a.tags]
    report.generated = [(a.name, e.name) for a, e, raw in zip(cfg.actions, expected[2], raw_actions)
                        if "name" not in raw and a.name != e.name]
    return report


def rename_device(path: Path, old: str, new: str) -> RenameReport:
    return _rename(path, "devices", old, new)


def rename_tag(path: Path, old: str, new: str) -> RenameReport:
    return _rename(path, "tags", old, new)


def rename_action(path: Path, old: str, new: str) -> RenameReport:
    return _rename(path, "actions", old, new)
