# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Surprise removal: the `removed` event and the `surprise` property."""

import unittest

from tests.test_engine_actions import FakeRunner
from tests.test_sources import BLK, FS, P, managed
from udevmon import config as C
from udevmon import explain
from udevmon.engine import Engine
from udevmon.events import (MOUNTED, REMOVED, SHUTDOWN, STARTUP, UNMOUNTED, VALID_KINDS,
                            VOLATILE_KEYS, TRUSTED_PLACEHOLDERS, Endpoint, Event)
from udevmon.sources import udisks as D


def mounted_tracker():
    t = D.UdisksTracker()
    t.load(managed())
    t.properties_changed(P, FS, {"MountPoints": [list(b"/run/media/u/BACKUP\0")]})
    return t


class TrackerTests(unittest.TestCase):
    def test_a_clean_unmount_says_surprise_0_and_fires_no_removed(self):
        t = mounted_tracker()
        events = t.properties_changed(P, FS, {"MountPoints": []})
        self.assertEqual([e.kind for e in events], [UNMOUNTED])
        self.assertEqual(events[0].endpoint.props["surprise"], "0")
        self.assertEqual(events[0].endpoint.props["mountpoint"], "/run/media/u/BACKUP")

    def test_a_yanked_filesystem_fires_unmounted_then_removed(self):
        t = mounted_tracker()
        events = t.interfaces_removed(P, [FS, BLK, D.I_PART])
        self.assertEqual([e.kind for e in events], [UNMOUNTED, REMOVED])
        for e in events:
            self.assertEqual(e.endpoint.props["surprise"], "1")
            self.assertEqual(e.endpoint.props["mountpoint"], "/run/media/u/BACKUP")
            self.assertEqual(e.endpoint.props["fs_uuid"], "U-1")

    def test_a_filesystem_that_was_not_mounted_fires_nothing_when_it_disappears(self):
        t = D.UdisksTracker()
        t.load(managed())
        self.assertEqual(t.interfaces_removed(P, [FS, BLK, D.I_PART]), [])

    def test_removed_after_the_daemon_started_with_it_already_mounted(self):
        t = D.UdisksTracker()
        t.load(managed(mounted=True))                    # present at startup: primed, nothing fires
        events = t.interfaces_removed(P, [FS, BLK])
        self.assertEqual([e.kind for e in events], [UNMOUNTED, REMOVED])

    def test_it_does_not_matter_how_often_or_in_what_order(self):
        t = mounted_tracker()
        t.interfaces_removed(P, [FS])                    # only the filesystem interface first
        self.assertEqual(t.interfaces_removed(P, [BLK, D.I_PART]), [])     # nothing twice
        again = mounted_tracker()
        again.properties_changed(P, FS, {"MountPoints": []})               # unmounted cleanly...
        self.assertEqual(again.interfaces_removed(P, [FS, BLK]), [])       # ...then unplugged: no `removed`


class ConfigTests(unittest.TestCase):
    def parse(self, on, run='["true"]', extra=""):
        return C.parse(C.tomllib.loads(
            f'[devices.d]\nmatch = {{ fs_uuid = "U-1" }}\ntags = ["t"]\n\n[[actions]]\nname = "a"\n'
            f'device = "d"\non = {on}\nrun = {run}\n{extra}'))

    def test_removed_is_a_kind(self):
        self.assertIn(REMOVED, VALID_KINDS)
        self.assertEqual(self.parse('["removed"]').actions[0].on, ("removed",))

    def test_surprise_is_a_placeholder_for_unmounted_and_removed_only(self):
        for kind in ("unmounted", "removed"):
            self.parse(f'["{kind}"]', '["echo", "{surprise}", "{mountpoint}"]')
        for kind in ("mounted", "connected", "startup", "shutdown"):
            with self.subTest(kind):
                with self.assertRaises(C.ConfigError) as cm:
                    self.parse(f'["{kind}"]', '["echo", "{surprise}"]')
                self.assertIn("surprise", str(cm.exception))

    def test_a_tag_rule_cannot_use_it_and_it_is_trusted(self):
        self.assertIn("surprise", VOLATILE_KEYS)
        self.assertIn("surprise", TRUSTED_PLACEHOLDERS)
        with self.assertRaises(C.ConfigError):
            C.parse(C.tomllib.loads('[tags.x]\nmatch = { surprise = "1" }'))

    def test_using_it_from_an_unnamed_allowed_action_is_not_flagged_as_hardware_data(self):
        cfg = C.parse(C.tomllib.loads(
            '[tags.s]\nmatch = { fs_type = "ext4" }\n\n[[actions]]\nname = "a"\ntag = "s"\n'
            'on = ["removed"]\nallow_unnamed = true\nrun = ["echo", "{surprise}"]\n'))
        self.assertFalse([w for w in cfg.warnings if "hardware-supplied" in w])


class EngineTests(unittest.TestCase):
    CFG = ('[devices.d]\nmatch = { fs_uuid = "U-1" }\n\n'
           '[[actions]]\nname = "any-unmount"\ndevice = "d"\non = ["unmounted"]\nrun = ["true"]\n\n'
           '[[actions]]\nname = "yank-warning"\ndevice = "d"\non = ["removed"]\nrun = ["true"]\n')

    def ran(self, events):
        runner = FakeRunner()
        engine = Engine(C.parse(C.tomllib.loads(self.CFG)), runner, clock=lambda: 0.0)
        for e in events:
            engine._last.clear()
            engine.handle(e)
        return [(kind, [name for name, _ in jobs]) for kind, jobs in runner.calls]

    def test_a_yank_runs_both_kinds_of_action_and_a_clean_unmount_only_the_first(self):
        t = mounted_tracker()
        yank = t.interfaces_removed(P, [FS, BLK, D.I_PART])
        self.assertEqual(self.ran(yank), [(UNMOUNTED, ["any-unmount"]), (REMOVED, ["yank-warning"])])
        t = mounted_tracker()
        clean = t.properties_changed(P, FS, {"MountPoints": []})
        self.assertEqual(self.ran(clean), [(UNMOUNTED, ["any-unmount"])])

    def test_the_values_reach_the_command(self):
        from udevmon import actions as A
        t = mounted_tracker()
        yank = t.interfaces_removed(P, [FS, BLK, D.I_PART])[1]
        action = C.Action("w", ("d",), (), (REMOVED,), ("echo", "{surprise}", "{mountpoint}"))
        argv = C.expand(action.run, {**yank.props, "device": "d"})
        self.assertEqual(argv, ["echo", "1", "/run/media/u/BACKUP"])
        self.assertEqual(A.build_env(yank, "d")["UDEVMON_SURPRISE"], "1")


class ExplainTests(unittest.TestCase):
    def test_removed_is_listed_for_filesystems_only(self):
        fs = Endpoint("udisks", "/o", {"fs_uuid": "U-1", "fs_type": "ext4"})
        usb = Endpoint("udev", "/s", {"serial": "S9x7a"})
        self.assertIn(REMOVED, explain.kinds_for(fs))
        self.assertNotIn(REMOVED, explain.kinds_for(usb))
        cfg = C.parse(C.tomllib.loads(EngineTests.CFG))
        text = "\n".join(explain.explain_endpoint(cfg, fs))
        self.assertIn("removed       runs 'yank-warning'", text)
        self.assertTrue(explain.simulated(cfg, "d", {}, REMOVED).is_filesystem)


if __name__ == "__main__":
    unittest.main()
