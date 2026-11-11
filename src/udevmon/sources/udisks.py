# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""udisks2 source: filesystem mount/unmount via D-Bus signals on the system bus.

It also links each mounted filesystem to the USB device it is on (through sysfs,
:func:`usb_parent`), so that the daemon can tell a surprise ``disconnected``
(:meth:`UdisksTracker.surprise_for`).
"""

from __future__ import annotations

import itertools
import logging
import os
import time
from typing import Callable

from ..events import MOUNTED, REMOVED, STARTUP, UNMOUNTED, Endpoint, Event

log = logging.getLogger("udevmon.udisks")

BUS_NAME = "org.freedesktop.UDisks2"            #: udisks2's name on the system bus
ROOT = "/org/freedesktop/UDisks2"               #: its object manager
I_BLOCK = "org.freedesktop.UDisks2.Block"       #: the interface of a block device
I_FS = "org.freedesktop.UDisks2.Filesystem"     #: the interface of a filesystem
I_PART = "org.freedesktop.UDisks2.Partition"    #: the interface of a partition
I_DRIVE = "org.freedesktop.UDisks2.Drive"       #: the interface of a drive


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


#: Seconds after a filesystem vanished while mounted during which its USB device's
#: ``disconnected`` still counts as a surprise (udev and udisks report in either order).
SURPRISE_WINDOW = 5.0


def usb_parent(devnode: str) -> str | None:
    """The sysfs path of the USB device a block device (``/dev/sdb1``) sits on, walking up
    its sysfs ancestors; None when it is not on USB or cannot be looked up."""
    try:
        import pyudev
        dev = pyudev.Devices.from_device_file(pyudev.Context(), devnode)
        for parent in dev.ancestors:
            if parent.subsystem == "usb" and parent.properties.get("DEVTYPE") == "usb_device":
                return parent.sys_path
    except Exception:                                # gone already, or not a device file
        return None
    return None


class UdisksTracker:
    """Mirrors the udisks object tree and turns changes into mount events.

    With ``find_usb`` (devnode -> the USB device's sysfs path, like :func:`usb_parent`) it
    also remembers which USB device each mounted filesystem is on, for
    :meth:`surprise_for`; ``clock`` (monotonic seconds) times :data:`SURPRISE_WINDOW`.
    """

    def __init__(self, find_usb: Callable[[str], str | None] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        self.objects: dict[str, dict[str, dict]] = {}
        self._mounted: dict[str, Endpoint] = {}
        self._find_usb = find_usb
        self._clock = clock
        self._usb_of: dict[str, str] = {}         # mounted object path -> its USB device
        self._vanished: dict[str, float] = {}     # USB device -> when a mount on it vanished
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

    def _note_usb(self, path: str, endpoint: Endpoint) -> None:
        """Remember the USB device the newly mounted filesystem at ``path`` is on (read
        now: after a pull its sysfs entries are gone)."""
        devnode = endpoint.props.get("devnode")
        if self._find_usb is not None and devnode:
            usb = self._find_usb(devnode)
            if usb:
                self._usb_of[path] = usb

    def surprise_for(self, usb_syspath: str) -> bool:
        """Whether a ``disconnected`` of the USB device at ``usb_syspath`` is a surprise: a
        filesystem on it is still mounted, or vanished while mounted within
        :data:`SURPRISE_WINDOW` seconds."""
        if any(usb == usb_syspath for path, usb in self._usb_of.items() if path in self._mounted):
            return True
        when = self._vanished.get(usb_syspath)
        return when is not None and self._clock() - when <= SURPRISE_WINDOW

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
                self._note_usb(path, self._mounted[path])

    def _reconcile(self, path: str) -> list[Event]:
        """The events from comparing the object's mount points with what was mounted:
        ``mounted`` (a new instance) or a clean ``unmounted``; none otherwise."""
        mps = self._mount_points(path)
        was = self._mounted.get(path)
        if mps and was is None:
            self._new_instance(path)                     # a new mount, a new instance
            endpoint = self._endpoint(path, mps[0])
            self._mounted[path] = endpoint
            self._note_usb(path, endpoint)
            return [Event(MOUNTED, endpoint)]
        if not mps and was is not None:                 # a clean unmount
            del self._mounted[path]
            self._usb_of.pop(path, None)
            self._instances.pop(path, None)
            return [Event(UNMOUNTED, _with_surprise(was, False))]
        return []

    def interfaces_added(self, path: str, ifaces: dict) -> list[Event]:
        """The events for an ``InterfacesAdded`` signal."""
        self.objects.setdefault(path, {}).update({i: dict(v) for i, v in ifaces.items()})
        return self._reconcile(path)

    def properties_changed(self, path: str, iface: str, changed: dict,
                           invalidated=()) -> list[Event]:
        """A ``PropertiesChanged`` signal: update the object's cached properties; the
        events that follow (a mount or an unmount)."""
        if path not in self.objects:
            return []
        props = self.objects[path].setdefault(iface, {})
        props.update(changed)
        for name in invalidated:
            props.pop(name, None)
        return self._reconcile(path)

    def interfaces_removed(self, path: str, iface_names) -> list[Event]:
        """The events for an ``InterfacesRemoved`` signal."""
        ifaces = self.objects.get(path)
        if ifaces is None:
            return []
        events = []
        if I_FS in iface_names and path in self._mounted:
            # the device vanished (or its filesystem interface was dropped) while mounted:
            # `unmounted` still fires, as before, then `removed`; both say surprise = "1"
            gone = _with_surprise(self._mounted.pop(path), True)
            self._instances.pop(path, None)
            usb = self._usb_of.pop(path, None)
            if usb:
                now = self._clock()
                self._vanished = {k: t for k, t in self._vanished.items()
                                  if now - t <= SURPRISE_WINDOW}
                self._vanished[usb] = now
            events.append(Event(UNMOUNTED, gone))
            events.append(Event(REMOVED, gone))
        for name in iface_names:
            ifaces.pop(name, None)
        if not ifaces:
            del self.objects[path]
            self._instances.pop(path, None)
        return events

    def filesystems(self, include_system: bool = False) -> list[Endpoint]:
        """The filesystems in the tree; system ones only with ``include_system``."""
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
    """Filesystems from udisks2 on the system bus: ``mounted``, ``unmounted`` and ``removed``.

    It follows udisks2 starting and stopping. With ``announce_present`` the filesystems
    already there get ``startup`` once, when udisks2 is first seen; ``include_system``
    says whether :meth:`present` lists system filesystems. Its tracker links mounts to
    their USB devices (:func:`usb_parent`).
    """
    name = "udisks"         #: the ``source`` of its endpoints

    def __init__(self, announce_present=False, include_system=True):
        self.announce_present = announce_present
        self.include_system = include_system
        self._announced = False
        self.tracker = UdisksTracker(find_usb=usb_parent)
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

    def present(self, include_system: bool | None = None) -> list[Endpoint]:
        """Filesystems currently known to be present; system ones as the source was made,
        unless ``include_system`` says otherwise."""
        return self.tracker.filesystems(self.include_system if include_system is None
                                        else include_system)

    def snapshot(self, include_system: bool = False) -> list[Endpoint]:
        """The filesystems present now, asked from udisks2; empty (with a warning) when it cannot
        be reached.
        """
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
        """Watch udisks2 from the GLib main loop and call ``emit`` with each event."""
        from gi.repository import Gio
        self._emit = emit
        self._watch = Gio.bus_watch_name(
            Gio.BusType.SYSTEM, BUS_NAME, Gio.BusNameWatcherFlags.AUTO_START,
            self._appeared, self._vanished)

    def stop(self) -> None:
        """Stop watching."""
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
        """udisks2 left the bus: stop listening and forget its state until it is back."""
        log.info("udisks2 not available; filesystem events paused until it appears")
        self._unsubscribe()
        self.tracker = UdisksTracker(find_usb=usb_parent)

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
