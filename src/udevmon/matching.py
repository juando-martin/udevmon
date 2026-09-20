"""Device matching and match-strength assessment."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .events import Endpoint

STRONG, MEDIUM, WEAK = "strong", "medium", "weak"

_IDENTITY_KEYS = ("serial", "fs_uuid", "part_uuid", "drive_serial")
_PLACEHOLDER_SERIALS = {
    "0123456789abcdef", "1234567890", "123456789", "12345678", "1234567",
    "123456", "0123456789", "000000000000", "abcdef0123456789",
    "0000000000000001", "1", "0", "none", "unknown", "n/a",
}


def is_suspicious_serial(serial: str) -> bool:
    s = serial.strip().lower()
    if not s or s in _PLACEHOLDER_SERIALS:
        return True
    return len(set(s)) == 1


def _scalar(value) -> str | None:
    """The single value of ``value``, or None for a real list (a list can match many things)."""
    if isinstance(value, str):
        return value
    if isinstance(value, (tuple, list)) and len(value) == 1:
        return str(value[0])
    return None


def _wanted(value) -> list[str]:
    return [value] if isinstance(value, str) else [str(v) for v in value]


def matches(match: Mapping[str, Any], props: Mapping[str, str]) -> bool:
    """True when every pair in ``match`` matches the property (case-insensitive).

    A value is a string (equals) or a list/tuple of strings (equals any of them).
    """
    if not match:
        return False
    for key, want in match.items():
        got = props.get(key)
        if got is None or got.casefold() not in {w.casefold() for w in _wanted(want)}:
            return False
    return True


def strength(match: Mapping[str, Any]) -> str:
    """Strength of a device match or tag rule; list values never count as identity/location."""
    scalars = {k: v for k, v in ((k, _scalar(v)) for k, v in match.items()) if v is not None}
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


def build_match(endpoint: Endpoint, keys: list[str]) -> dict[str, str]:
    return {k: endpoint.props[k] for k in keys if endpoint.props.get(k)}


@dataclass
class Resolution:
    """What an endpoint is, in terms of the configuration (spec 5.5)."""
    named: list = field(default_factory=list)   # config Devices matching, in file order
    tags: list[str] = field(default_factory=list)  # explicit tags, then matching rule tags

    @property
    def is_named(self) -> bool:
        return bool(self.named)


def resolve(cfg, props: Mapping[str, str]) -> Resolution:
    """Find the named devices and tags that apply to an endpoint with ``props``."""
    res = Resolution()
    for device in cfg.devices.values():
        if matches(device.match, props):
            res.named.append(device)
            for tag in device.tags:
                if tag not in res.tags:
                    res.tags.append(tag)
    for tag in cfg.tags.values():
        if tag.match and tag.name not in res.tags and matches(tag.match, props):
            res.tags.append(tag.name)
    return res
