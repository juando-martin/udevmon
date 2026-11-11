# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`disconnected` says `surprise = "1"` when a filesystem on that USB device was mounted."""

import unittest
from unittest import mock

from udevmon import config as C
from udevmon import daemon
from udevmon.events import DISCONNECTED, Endpoint, Event
from udevmon.sources import udisks as D

from tests.test_sources import FS, P, managed

USB = "/sys/devices/pci0000:00/0000:00:14.0/usb1/1-2"


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


class TrackerTests(unittest.TestCase):
    def tracker(self, usb=USB):
        self.clock = Clock()
        self.asked = []

        def find(devnode):
            self.asked.append(devnode)
            return usb
        return D.UdisksTracker(find_usb=find, clock=self.clock)

    def test_mounted_at_the_disconnect(self):
        t = self.tracker()
        t.load(managed(mounted=True))
        self.assertEqual(self.asked, ["/dev/sdb1"])
        self.assertTrue(t.surprise_for(USB))
        self.assertFalse(t.surprise_for("/sys/devices/other"))

    def test_mounted_later_and_cleanly_unmounted(self):
        t = self.tracker()
        t.load(managed())
        self.assertFalse(t.surprise_for(USB))
        t.properties_changed(P, FS, {"MountPoints": [list(b"/run/media/u/BACKUP\0")]})
        self.assertTrue(t.surprise_for(USB))
        t.properties_changed(P, FS, {"MountPoints": []})             # a clean unmount
        self.assertFalse(t.surprise_for(USB))

    def test_udisks_reported_first(self):
        t = self.tracker()
        t.load(managed(mounted=True))
        t.interfaces_removed(P, [FS])                                 # vanished while mounted
        self.assertTrue(t.surprise_for(USB))
        self.clock.now += D.SURPRISE_WINDOW + 0.1
        self.assertFalse(t.surprise_for(USB))

    def test_not_on_usb(self):
        t = self.tracker(usb=None)
        t.load(managed(mounted=True))
        self.assertFalse(t.surprise_for(USB))

    def test_without_a_finder_nothing_is_linked(self):
        t = D.UdisksTracker()
        t.load(managed(mounted=True))
        self.assertFalse(t.surprise_for(USB))

    def test_a_device_that_is_gone_has_no_parent(self):
        self.assertIsNone(D.usb_parent("/dev/no-such-device-xyz"))


class DaemonTests(unittest.TestCase):
    def setUp(self):
        self.d = daemon.Daemon(None)
        self.d.cfg = C.parse({})
        self.d.engine = mock.Mock()
        self.udisks = D.UdisksSource()
        self.udisks.tracker = mock.Mock()
        self.d.sources = [self.udisks]

    def disconnect(self, surprise):
        self.udisks.tracker.surprise_for.return_value = surprise
        self.d._emit(Event(DISCONNECTED, Endpoint("udev", USB, {"serial": "S"})))
        (event,), _ = self.d.engine.handle.call_args
        self.udisks.tracker.surprise_for.assert_called_with(USB)
        return event

    def test_the_event_says_so(self):
        self.assertEqual(self.disconnect(True).props["surprise"], "1")
        self.assertEqual(self.disconnect(False).props["surprise"], "0")

    def test_the_placeholder_is_available(self):
        C.parse({"devices": {"k": {"match": {"serial": "S"}}},
                 "actions": [{"name": "a", "device": "k", "on": ["disconnected"],
                              "run": ["echo", "{surprise}"]}]})


if __name__ == "__main__":
    unittest.main()
