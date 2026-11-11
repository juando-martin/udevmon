# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import unittest

from udevmon import config as C
from udevmon.daemon import Daemon
from udevmon.engine import Engine
from udevmon.events import SHUTDOWN, STARTUP, Endpoint
from udevmon.sources import udev as U
from udevmon.sources import udisks as D
from tests.test_engine_actions import FakeRunner
from tests.test_sources import P, managed


class PresentKindsTests(unittest.TestCase):
    def test_config_accepts_startup_shutdown_with_mountpoint(self):
        cfg = C.parse({"devices": {"d": {"match": {"fs_uuid": "U1"}}},
                       "actions": [{"device": "d", "on": ["startup", "shutdown"],
                                    "run": ["echo", "{mountpoint}", "{serial}"]}]})
        self.assertEqual(cfg.actions[0].on, ("startup", "shutdown"))

    def test_engine_dispatches_present_kinds(self):
        cfg = C.parse({"devices": {"d": {"match": {"fs_uuid": "U1"}}},
                       "actions": [{"name": "s", "device": "d", "on": ["startup"], "run": ["true"]},
                                   {"name": "e", "device": "d", "on": ["shutdown"], "run": ["true"]}]})
        runner = FakeRunner()
        engine = Engine(cfg, runner)
        dev = Endpoint("udisks", "k", {"fs_uuid": "U1"})
        from udevmon.events import Event
        engine.handle(Event(STARTUP, dev))
        engine.handle(Event(SHUTDOWN, dev))
        self.assertEqual(runner.calls, [(STARTUP, [("s", "d")]), (SHUTDOWN, [("e", "d")])])

    def test_udev_present_tracks_cache(self):
        src = U.UdevSource()
        p = U.normalize({"ID_SERIAL_SHORT": "S1"}, syspath="/sys/a", subsystem="usb",
                        devtype="usb_device", devnode=None, sys_name="1-1")
        src.handle("add", p)
        self.assertEqual([d.key for d in src.present()], ["/sys/a"])
        src.handle("remove", p)
        self.assertEqual(src.present(), [])

    def test_udisks_present_includes_system_by_default(self):
        m = managed(mounted=True)
        m[P][D.I_BLOCK]["HintSystem"] = True
        src = D.UdisksSource()
        src.tracker.load(m)
        self.assertEqual(len(src.present()), 1)
        self.assertEqual(src.present()[0].props["mountpoint"], "/run/media/u/BACKUP")
        self.assertEqual(len(src.tracker.filesystems(include_system=False)), 0)

    def test_daemon_fire_shutdown_emits_for_all_present(self):
        class Src:
            def __init__(self, devs): self.devs = devs
            def present(self): return self.devs
        seen = []

        class Eng:
            def handle(self, e): seen.append((e.kind, e.endpoint.key))
        d = Daemon.__new__(Daemon)
        d._seq = 0
        d.engine, d.sources = Eng(), [Src([Endpoint("udev", "a", {})]), Src([Endpoint("udisks", "b", {})])]
        d.fire_shutdown()
        self.assertEqual(seen, [(SHUTDOWN, "a"), (SHUTDOWN, "b")])


if __name__ == "__main__":
    unittest.main()
