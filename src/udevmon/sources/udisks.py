"""udisks2 source: filesystem mount/unmount via D-Bus signals on the system bus."""

from __future__ import annotations

import itertools
import logging
import os
import time
from typing import Callable

from ..events import MOUNTED, REMOVED, STARTUP, UNMOUNTED, Endpoint, Event

log = logging.getLogger("udevmon.udisks")

BUS_NAME = "org.freedesktop.UDisks2"
ROOT = "/org/freedesktop/UDisks2"
I_BLOCK = "org.freedesktop.UDisks2.Block"
I_FS = "org.freedesktop.UDisks2.Filesystem"
I_PART = "org.freedesktop.UDisks2.Partition"
I_DRIVE = "org.freedesktop.UDisks2.Drive"


def _bytes(v) -> bytes:
    if isinstance(v, (bytes, bytearray)):
        return bytes(v)
    return bytes(v)          # list of ints


def _str(v) -> str:
    if isinstance(v, str):
        return v
    return _bytes(v).split(b"\0", 1)[0].decode(errors="replace")


def _mountpoints(v) -> list[str]:
    return [_str(m) for m in (v or [])]


def _with_surprise(endpoint: Endpoint, surprise: bool) -> Endpoint:
    """A copy of ``endpoint`` whose ``surprise`` says how it stopped being mounted."""
    return Endpoint(endpoint.source, endpoint.key,
                    {**endpoint.props, "surprise": "1" if surprise else "0"})


class UdisksTracker:
    """Mirrors the udisks object tree and turns changes into mount events."""

    def __init__(self):
        self.objects: dict[str, dict[str, dict]] = {}
        self._mounted: dict[str, Endpoint] = {}
        # One `instance` per mount: the same in mounted, startup, unmounted, removed and
        # shutdown of that mount, and new for the next one. Unique while the daemon runs.
        self._instances: dict[str, str] = {}
        self._start = int(time.time())
        self._counter = itertools.count(1)

    def _new_instance(self, path: str) -> str:
        self._instances[path] = f"fs-{self._start}-{next(self._counter)}"
        return self._instances[path]

    def _instance(self, path: str) -> str:
        return self._instances.get(path) or self._new_instance(path)

    # -- device construction -------------------------------------------------
    def _props_for(self, path: str) -> dict[str, str]:
        ifaces = self.objects.get(path, {})
        block, part = ifaces.get(I_BLOCK, {}), ifaces.get(I_PART, {})
        props = {}
        for key, val in (("fs_uuid", block.get("IdUUID")), ("fs_label", block.get("IdLabel")),
                         ("fs_type", block.get("IdType")), ("part_uuid", part.get("UUID")),
                         ("devnode", _str(block["Device"]) if "Device" in block else None)):
            if val:
                props[key] = val
        drive = self.objects.get(block.get("Drive", "/"), {}).get(I_DRIVE, {})
        for key, name in (("drive_vendor", "Vendor"), ("drive_model", "Model"),
                          ("drive_serial", "Serial")):
            if drive.get(name):
                props[key] = drive[name]
        return props

    def _endpoint(self, path: str, mountpoint: str | None = None) -> Endpoint:
        props = self._props_for(path)
        if mountpoint:
            props["mountpoint"] = mountpoint
        props["instance"] = self._instance(path)
        return Endpoint("udisks", path, props)

    def _mount_points(self, path: str) -> list[str]:
        return _mountpoints(self.objects.get(path, {}).get(I_FS, {}).get("MountPoints"))

    # -- state changes ---------------------------------------------------------
    def load(self, managed: dict) -> None:
        """Replace state from GetManagedObjects; fires nothing (priming)."""
        self.objects = {p: {i: dict(v) for i, v in ifs.items()} for p, ifs in managed.items()}
        self._mounted = {}
        self._instances = {}
        for path in self.objects:
            mps = self._mount_points(path)
            if mps:
                self._new_instance(path)
                self._mounted[path] = self._endpoint(path, mps[0])

    def _reconcile(self, path: str) -> list[Event]:
        mps = self._mount_points(path)
        was = self._mounted.get(path)
        if mps and was is None:
            self._new_instance(path)                     # a new mount, a new instance
            endpoint = self._endpoint(path, mps[0])
            self._mounted[path] = endpoint
            return [Event(MOUNTED, endpoint)]
        if not mps and was is not None:                 # a clean unmount
            del self._mounted[path]
            self._instances.pop(path, None)
            return [Event(UNMOUNTED, _with_surprise(was, False))]
        return []

    def interfaces_added(self, path: str, ifaces: dict) -> list[Event]:
        self.objects.setdefault(path, {}).update({i: dict(v) for i, v in ifaces.items()})
        return self._reconcile(path)

    def properties_changed(self, path: str, iface: str, changed: dict,
                           invalidated=()) -> list[Event]:
        if path not in self.objects:
            return []
        props = self.objects[path].setdefault(iface, {})
        props.update(changed)
        for name in invalidated:
            props.pop(name, None)
        return self._reconcile(path)

    def interfaces_removed(self, path: str, iface_names) -> list[Event]:
        ifaces = self.objects.get(path)
        if ifaces is None:
            return []
        events = []
        if I_FS in iface_names and path in self._mounted:
            # the device vanished (or its filesystem interface was dropped) while mounted:
            # `unmounted` still fires, as before, then `removed`; both say surprise = "1"
            gone = _with_surprise(self._mounted.pop(path), True)
            self._instances.pop(path, None)
            events.append(Event(UNMOUNTED, gone))
            events.append(Event(REMOVED, gone))
        for name in iface_names:
            ifaces.pop(name, None)
        if not ifaces:
            del self.objects[path]
            self._instances.pop(path, None)
        return events

    def filesystems(self, include_system: bool = False) -> list[Endpoint]:
        out = []
        for path, ifaces in self.objects.items():
            if I_FS not in ifaces:
                continue
            block = ifaces.get(I_BLOCK, {})
            if block.get("HintSystem") and not include_system:
                continue
            mps = self._mount_points(path)
            out.append(self._endpoint(path, mps[0] if mps else None))
        return out


    def mounted_at(self, mountpoint: str) -> Endpoint | None:
        """The filesystem mounted at ``mountpoint`` (any of its mount points; symlinks
        resolved), with that mount point as its ``mountpoint``; None if there is none."""
        want = os.path.realpath(mountpoint)
        for path, ifaces in self.objects.items():
            if I_FS not in ifaces:
                continue
            for mp in self._mount_points(path):
                if os.path.realpath(mp) == want:
                    return self._endpoint(path, mp)
        return None


class UdisksSource:
    name = "udisks"

    def __init__(self, announce_present=False, include_system=True):
        self.announce_present = announce_present
        self.include_system = include_system
        self._announced = False
        self.tracker = UdisksTracker()
        self._emit: Callable[[Event], None] | None = None
        self._conn = None
        self._subs: list[int] = []
        self._watch = None

    @staticmethod
    def _managed(conn) -> dict:
        from gi.repository import Gio, GLib
        res = conn.call_sync(BUS_NAME, ROOT, "org.freedesktop.DBus.ObjectManager",
                             "GetManagedObjects", None, GLib.VariantType("(a{oa{sa{sv}}})"),
                             Gio.DBusCallFlags.NONE, 5000, None)
        return res.unpack()[0]

    def present(self) -> list[Endpoint]:
        """Filesystems currently known to be present."""
        return self.tracker.filesystems(self.include_system)

    def snapshot(self, include_system: bool = False) -> list[Endpoint]:
        from gi.repository import Gio
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            tracker = UdisksTracker()
            tracker.load(self._managed(conn))
        except Exception as e:
            log.warning("cannot query udisks2: %s", e)
            return []
        return tracker.filesystems(include_system)

    def mounted_at(self, mountpoint: str) -> Endpoint | None:
        """The filesystem mounted at ``mountpoint`` now (asks udisks2), or None."""
        from gi.repository import Gio
        try:
            conn = Gio.bus_get_sync(Gio.BusType.SYSTEM, None)
            tracker = UdisksTracker()
            tracker.load(self._managed(conn))
        except Exception as e:
            log.warning("cannot query udisks2: %s", e)
            return None
        return tracker.mounted_at(mountpoint)

    def start(self, emit: Callable[[Event], None]) -> None:
        from gi.repository import Gio
        self._emit = emit
        self._watch = Gio.bus_watch_name(
            Gio.BusType.SYSTEM, BUS_NAME, Gio.BusNameWatcherFlags.AUTO_START,
            self._appeared, self._vanished)

    def stop(self) -> None:
        from gi.repository import Gio
        if self._watch is not None:
            Gio.bus_unwatch_name(self._watch)
            self._watch = None
        self._unsubscribe()

    def _unsubscribe(self):
        if self._conn is not None:
            for sid in self._subs:
                self._conn.signal_unsubscribe(sid)
        self._subs, self._conn = [], None

    def _appeared(self, conn, name, owner) -> None:
        from gi.repository import Gio
        self._unsubscribe()
        self._conn = conn
        flags = Gio.DBusSignalFlags.NONE
        om, props = "org.freedesktop.DBus.ObjectManager", "org.freedesktop.DBus.Properties"
        self._subs = [
            conn.signal_subscribe(BUS_NAME, om, "InterfacesAdded", ROOT, None, flags, self._signal),
            conn.signal_subscribe(BUS_NAME, om, "InterfacesRemoved", ROOT, None, flags, self._signal),
            conn.signal_subscribe(BUS_NAME, props, "PropertiesChanged", None, None, flags, self._signal),
        ]
        try:
            self.tracker.load(self._managed(conn))
        except Exception:
            log.exception("could not read udisks2 state")
            return
        log.info("udisks2 source attached (%d objects)", len(self.tracker.objects))
        if self.announce_present and not self._announced:
            self._announced = True
            for dev in self.present():
                self._emit(Event(STARTUP, dev))

    def _vanished(self, conn, name) -> None:
        log.info("udisks2 not available; filesystem events paused until it appears")
        self._unsubscribe()
        self.tracker = UdisksTracker()

    def _signal(self, conn, sender, path, iface, signal, params, _data=None) -> None:
        try:
            args = params.unpack()
            if signal == "InterfacesAdded":
                events = self.tracker.interfaces_added(args[0], args[1])
            elif signal == "InterfacesRemoved":
                events = self.tracker.interfaces_removed(args[0], args[1])
            elif signal == "PropertiesChanged":
                if not path.startswith(ROOT + "/"):
                    return
                events = self.tracker.properties_changed(path, args[0], args[1], args[2])
            else:
                return
            for event in events:
                self._emit(event)
        except Exception:
            log.exception("error handling udisks signal %s", signal)
