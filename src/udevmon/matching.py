# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Device matching and match-strength assessment."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

from .events import Endpoint

#: How surely a match tells one thing from another (:func:`strength`).
STRONG = "strong"
MEDIUM = "medium"  #: see :data:`STRONG`
WEAK = "weak"  #: see :data:`STRONG`

_IDENTITY_KEYS = ("serial", "fs_uuid", "part_uuid", "drive_serial")
_PLACEHOLDER_SERIALS = {
    "0123456789abcdef", "1234567890", "123456789", "12345678", "1234567",
    "123456", "0123456789", "000000000000", "abcdef0123456789",
    "0000000000000001", "1", "0", "none", "unknown", "n/a",
}


def is_suspicious_serial(serial: str) -> bool:
    """Whether a serial number looks made up (empty, a known placeholder, one repeated character),
    so it cannot tell one device from another (spec 5.3.1).
    """
    s = serial.strip().lower()
    if not s or s in _PLACEHOLDER_SERIALS:
        return True
    return len(set(s)) == 1


def has_wildcard(value) -> bool:
    """True for a tag-rule value that uses ``*`` (any run of characters) or ``?`` (one)."""
    return isinstance(value, str) and ("*" in value or "?" in value)


def _glob(pattern: str):
    """A pattern with only ``*`` and ``?`` as wildcards (everything else is literal)."""
    return re.compile("".join(".*" if c == "*" else "." if c == "?" else re.escape(c)
                              for c in pattern.casefold()), re.DOTALL)


def _scalar(value, wild: bool = False) -> str | None:
    """The single value of ``value``, or None for a real list (a list can match many things)
    and, in a tag rule, for a wildcard pattern (it can match many things too)."""
    if isinstance(value, (tuple, list)) and len(value) == 1:
        value = value[0]
    if not isinstance(value, str) or (wild and has_wildcard(value)):
        return None
    return value


def _wanted(value) -> list[str]:
    return [value] if isinstance(value, str) else [str(v) for v in value]


def matches(match: Mapping[str, Any], props: Mapping[str, str], wild: bool = False) -> bool:
    """True when every pair in ``match`` matches the property (case-insensitive).

    A value is a string (equals) or a list/tuple of strings (equals any of them). With
    ``wild`` (tag rules) ``*`` and ``?`` in a value are wildcards.
    """
    if not match:
        return False
    for key, want in match.items():
        got = props.get(key)
        if got is None:
            return False
        got = got.casefold()
        if wild:
            if not any(_glob(w).fullmatch(got) for w in _wanted(want)):
                return False
        elif got not in {w.casefold() for w in _wanted(want)}:
            return False
    return True


def strength(match: Mapping[str, Any], wild: bool = False) -> str:
    """Strength of a device match or tag rule (``wild``: a rule, where patterns are wildcards).

    List values and wildcard patterns never count as identity or location.
    """
    scalars = {k: v for k, v in ((k, _scalar(v, wild)) for k, v in match.items())
               if v is not None}
    for key in _IDENTITY_KEYS:
        value = scalars.get(key)
        if value is None:
            continue
        if key in ("serial", "drive_serial") and is_suspicious_serial(value):
            continue
        return STRONG
    if {"vendor_id", "product_id", "port_path"} <= set(scalars):
        return MEDIUM
    return WEAK


def strength_note(level: str) -> str:
    """A short explanation of a match strength, for messages."""
    return {
        STRONG: "identifies this exact device",
        MEDIUM: "matches any device of this model in this port",
        WEAK: "matches every device/filesystem with these values",
    }[level]


def default_match_keys(endpoint: Endpoint) -> list[str]:
    """Best available identifying keys for ``endpoint`` (see spec 5.2)."""
    p = endpoint.props
    if endpoint.is_filesystem:
        if p.get("fs_uuid"):
            return ["fs_uuid"]
        if p.get("part_uuid"):
            return ["part_uuid"]
        if p.get("drive_serial") and not is_suspicious_serial(p["drive_serial"]):
            return [k for k in ("drive_serial", "fs_label") if p.get(k)]
        return [k for k in ("fs_label",) if p.get(k)]
    ids = [k for k in ("vendor_id", "product_id") if p.get(k)]
    if p.get("serial") and not is_suspicious_serial(p["serial"]):
        return ids + ["serial"]
    return ids + [k for k in ("port_path",) if p.get(k)]


def lacks_identity(endpoint: Endpoint) -> bool:
    """A USB device with no usable serial number: nothing identifies this particular unit."""
    p = endpoint.props
    return not endpoint.is_filesystem and not (p.get("serial") and
                                               not is_suspicious_serial(p["serial"]))


def match_choices(endpoint: Endpoint) -> list[tuple[str, list[str]]]:
    """What a device with no serial number can be matched on, as (meaning, keys).

    Neither choice identifies the unit: one is tied to the USB port, the other matches every
    unit of the model. `scan` asks the user to pick rather than pre-selecting one.
    """
    p = endpoint.props
    model = [k for k in ("vendor_id", "product_id") if p.get(k)]
    choices = []
    if p.get("port_path") and model:
        choices.append(("this model, in this port only", model + ["port_path"]))
    if model:
        choices.append(("this model, in any port", model))
    return choices


def level_label(endpoint: Endpoint, keys: list[str] | None = None) -> str:
    """The strength of the best match for ``endpoint``, saying so when it is only a port."""
    keys = default_match_keys(endpoint) if keys is None else keys
    if not keys:
        return "-"
    level = strength(build_match(endpoint, keys))
    if "port_path" in keys:
        return f"{level}: no serial, matches the port"
    return level


def build_match(endpoint: Endpoint, keys: list[str]) -> dict[str, str]:
    """The match on ``keys`` with ``endpoint``'s values; keys it has no value for are left out."""
    return {k: endpoint.props[k] for k in keys if endpoint.props.get(k)}


@dataclass
class Resolution:
    """What an endpoint is, in terms of the configuration (spec 5.5)."""
    named: list = field(default_factory=list)   #: config Devices matching, in file order
    tags: list[str] = field(default_factory=list)  #: explicit tags, then matching rule tags
    excluded: list[str] = field(default_factory=list)  #: rule tags a matching device opted out of

    @property
    def is_named(self) -> bool:
        """Whether at least one named device matches."""
        return bool(self.named)

    @property
    def ignored(self) -> list:
        """The matching devices that are ignored (any one silences the whole thing)."""
        return [d for d in self.named if d.status == "ignored"]


def device_label(device) -> str:
    """The device's name, followed by [disabled] or [ignored] when it is not active."""
    return device.name if device.status == "active" else f"{device.name} [{device.status}]"


def resolve(cfg, props: Mapping[str, str]) -> Resolution:
    """Find the named devices and tags that apply to an endpoint with ``props``."""
    res = Resolution()
    opted_out: set[str] = set()
    for device in cfg.devices.values():
        if matches(device.match, props):
            res.named.append(device)
            opted_out.update(device.exclude_tags)
            for tag in device.tags:
                if tag not in res.tags:
                    res.tags.append(tag)
    for tag in cfg.tags.values():
        if tag.match and tag.name not in res.tags and matches(tag.match, props, wild=True):
            # an opt-out only stops a rule from selecting the thing; an explicit tag stays
            (res.excluded if tag.name in opted_out else res.tags).append(tag.name)
    return res
