"""Asset matching and match-strength assessment."""

from __future__ import annotations

from typing import Mapping

from .events import Device

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


def matches(match: Mapping[str, str], props: Mapping[str, str]) -> bool:
    """True when every pair in ``match`` equals the property (case-insensitive)."""
    if not match:
        return False
    for key, want in match.items():
        got = props.get(key)
        if got is None or got.casefold() != str(want).casefold():
            return False
    return True


def strength(match: Mapping[str, str]) -> str:
    for key in _IDENTITY_KEYS:
        value = match.get(key)
        if value is None:
            continue
        if key in ("serial", "drive_serial") and is_suspicious_serial(value):
            continue
        return STRONG
    if {"vendor_id", "product_id", "port_path"} <= set(match):
        return MEDIUM
    return WEAK


def strength_note(level: str) -> str:
    return {
        STRONG: "identifies this exact device",
        MEDIUM: "matches any device of this model in this port",
        WEAK: "matches every device/filesystem with these values",
    }[level]


def default_match_keys(device: Device) -> list[str]:
    """Best available identifying keys for ``device`` (see spec 5.2)."""
    p = device.props
    if device.is_filesystem:
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


def build_match(device: Device, keys: list[str]) -> dict[str, str]:
    return {k: device.props[k] for k in keys if device.props.get(k)}
