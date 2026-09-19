"""udev source: device add/remove via the (unprivileged) udev netlink group."""

from __future__ import annotations

import logging
from typing import Callable, Mapping

from ..events import CONNECTED, DISCONNECTED, STARTUP, Device, Event

log = logging.getLogger("udevmon.udev")


def parse_filters(filters) -> list[tuple[str, str | None]]:
    out = []
    for f in filters:
        sub, _, devtype = f.partition("/")
        out.append((sub, devtype or None))
    return out


def normalize(raw: Mapping[str, str], *, syspath: str, subsystem: str,
              devtype: str | None, devnode: str | None, sys_name: str) -> dict[str, str]:
    """Turn raw udev properties into the normalized property names."""
    props: dict[str, str] = {"syspath": syspath, "subsystem": subsystem or "",
                             "port_path": sys_name}
    if devtype:
        props["devtype"] = devtype
    if devnode:
        props["devnode"] = devnode

    vid = raw.get("ID_VENDOR_ID")
    pid = raw.get("ID_MODEL_ID")
    if (not vid or not pid) and raw.get("PRODUCT"):
        try:
            v, p, _ = raw["PRODUCT"].split("/")
            vid, pid = vid or f"{int(v, 16):04x}", pid or f"{int(p, 16):04x}"
        except ValueError:
            pass
    pairs = {
        "vendor_id": vid, "product_id": pid,
        "serial": raw.get("ID_SERIAL_SHORT"),
        "vendor": raw.get("ID_VENDOR_FROM_DATABASE") or raw.get("ID_VENDOR"),
        "model": raw.get("ID_MODEL_FROM_DATABASE") or raw.get("ID_MODEL"),
    }
    props.update({k: v.lower() if k.endswith("_id") else v for k, v in pairs.items() if v})
    for k, v in raw.items():
        props[f"udev_{k.lower()}"] = v
    return props


def _device_props(dev) -> dict[str, str]:
    return normalize(dict(dev.properties), syspath=dev.sys_path, subsystem=dev.subsystem,
                     devtype=dev.device_type, devnode=dev.device_node, sys_name=dev.sys_name)


class UdevSource:
    name = "udev"

    def __init__(self, filters=("usb/usb_device",), announce_present=False):
        self.filters = parse_filters(filters)
        self.announce_present = announce_present
        self._cache: dict[str, Device] = {}
        self._emit: Callable[[Event], None] | None = None
        self._monitor = None
        self._watch_id = None

    # -- pure state handling (unit-testable) --------------------------------
    def handle(self, action: str, props: dict[str, str]) -> Event | None:
        key = props["syspath"]
        if action == "add":
            device = Device(self.name, key, props)
            self._cache[key] = device
            return Event(CONNECTED, device)
        if action == "remove":
            cached = self._cache.pop(key, None)
            merged = {**(cached.props if cached else {}), **props}
            return Event(DISCONNECTED, Device(self.name, key, merged))
        return None

    def present(self) -> list[Device]:
        """Devices currently known to be present."""
        return list(self._cache.values())

    # -- system glue ---------------------------------------------------------
    def _context(self):
        import pyudev
        return pyudev, pyudev.Context()

    def snapshot(self) -> list[Device]:
        pyudev, ctx = self._context()
        devices = []
        for sub, devtype in self.filters:
            kwargs = {"subsystem": sub}
            if devtype:
                kwargs["DEVTYPE"] = devtype
            for dev in ctx.list_devices(**kwargs):
                devices.append(Device(self.name, dev.sys_path, _device_props(dev)))
        return devices

    def start(self, emit: Callable[[Event], None]) -> None:
        from gi.repository import GLib
        pyudev, ctx = self._context()
        self._emit = emit
        for d in self.snapshot():            # prime cache silently
            self._cache[d.key] = d
        mon = pyudev.Monitor.from_netlink(ctx)
        for sub, devtype in self.filters:
            mon.filter_by(sub, devtype)
        mon.start()
        self._monitor = mon
        self._watch_id = GLib.io_add_watch(mon.fileno(), GLib.PRIORITY_DEFAULT,
                                           GLib.IO_IN, self._readable)
        log.info("udev source watching %s", ", ".join(
            f"{s}/{d}" if d else s for s, d in self.filters))
        if self.announce_present:
            for d in self.present():
                emit(Event(STARTUP, d))

    def _readable(self, fd, condition) -> bool:
        while True:
            dev = self._monitor.poll(timeout=0)
            if dev is None:
                break
            try:
                event = self.handle(dev.action, _device_props(dev))
                if event:
                    self._emit(event)
            except Exception:
                log.exception("error handling udev event")
        return True

    def stop(self) -> None:
        from gi.repository import GLib
        if self._watch_id is not None:
            GLib.source_remove(self._watch_id)
            self._watch_id = None
