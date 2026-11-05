"""Normalized data model shared by every source, the engine and the CLI."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Mapping

CONNECTED = "connected"
DISCONNECTED = "disconnected"
MOUNTED = "mounted"
UNMOUNTED = "unmounted"
REMOVED = "removed"          # a mounted filesystem vanished without being unmounted
STARTUP = "startup"          # device already present when the daemon started
SHUTDOWN = "shutdown"        # device still present when the daemon exits

# kind -> property keys that are always available to placeholders for it
_USB_KEYS = frozenset({
    "vendor_id", "product_id", "serial", "vendor", "model", "port_path",
    "devnode", "syspath", "subsystem", "devtype", "instance",
})
_FS_KEYS = frozenset({
    "fs_uuid", "fs_label", "fs_type", "part_uuid", "devnode",
    "drive_vendor", "drive_model", "drive_serial", "instance",
})
_COMMON = frozenset({"event"})   # "device"/"tag" depend on the action; see config

KNOWN_KEYS: dict[str, frozenset[str]] = {
    CONNECTED: _USB_KEYS | _COMMON,
    DISCONNECTED: _USB_KEYS | _COMMON,
    MOUNTED: _FS_KEYS | {"mountpoint"} | _COMMON,
    UNMOUNTED: _FS_KEYS | {"mountpoint", "surprise"} | _COMMON,
    REMOVED: _FS_KEYS | {"mountpoint", "surprise"} | _COMMON,
    # present devices may be USB or filesystems; a missing key skips the action at run time
    STARTUP: _USB_KEYS | _FS_KEYS | {"mountpoint"} | _COMMON,
    SHUTDOWN: _USB_KEYS | _FS_KEYS | {"mountpoint"} | _COMMON,
}
VALID_KINDS = frozenset(KNOWN_KEYS)
# What `udevmon trigger` can fire for a present thing (the others say it is gone).
TRIGGER_KINDS = (STARTUP, SHUTDOWN, CONNECTED, MOUNTED, UNMOUNTED)

# Properties that change per plug / per event; useless (and misleading) in match rules.
VOLATILE_KEYS = frozenset({
    "mountpoint", "devnode", "syspath", "event", "device", "tag", "surprise",
    "udev_devname", "udev_devpath", "udev_busnum", "udev_devnum", "udev_seqnum",
    "udev_usec_initialized", "instance",
})

# Placeholders whose values do not come from the (possibly hostile) hardware itself.
TRUSTED_PLACEHOLDERS = frozenset({
    "event", "tag", "device", "devnode", "syspath", "port_path", "subsystem", "devtype",
    "surprise", "instance", "state_dir",
})


@dataclass(frozen=True)
class Endpoint:
    source: str                     # "udev" | "udisks"
    key: str                        # stable while present (syspath / object path)
    props: Mapping[str, str]

    @property
    def is_filesystem(self) -> bool:
        return self.source == "udisks"


@dataclass(frozen=True)
class Event:
    kind: str
    endpoint: Endpoint
    time: float = field(default_factory=time.time)

    @property
    def props(self) -> dict[str, str]:
        return {**self.endpoint.props, "event": self.kind}


def describe(endpoint: Endpoint) -> str:
    """One-line human description of a device."""
    p = endpoint.props
    if endpoint.is_filesystem:
        name = p.get("fs_label") or p.get("fs_uuid") or p.get("devnode", "?")
        out = f"Filesystem {name}"
        if p.get("fs_type"):
            out += f" ({p['fs_type']})"
        if p.get("devnode"):
            out += f" on {p['devnode']}"
        if p.get("mountpoint"):
            out += f" at {p['mountpoint']}"
        return out
    name = " ".join(x for x in (p.get("vendor"), p.get("model")) if x) or "USB device"
    ids = ":".join(x for x in (p.get("vendor_id"), p.get("product_id")) if x)
    return f"USB {name}" + (f" [{ids}]" if ids else "")


def is_raw(name: str) -> bool:
    """A ``{udev_NAME}`` placeholder: a raw udev property, known only when an event arrives."""
    return name.startswith("udev_")


def is_variable(name: str) -> bool:
    """A ``{var_NAME}`` placeholder: a value the user wrote on a device."""
    return name.startswith("var_")
