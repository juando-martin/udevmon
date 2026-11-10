# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli, explain
from udevmon import config as C
from udevmon.engine import Engine
from udevmon.events import CONNECTED, MOUNTED, Endpoint, Event

CONFIG = """
[devices.stick]
match = { serial = "S9x7a" }
tags = ["usbtest"]

[devices.backup-disk]
match = { fs_uuid = "U1" }

[tags.storage]
match = { fs_type = ["ext4", "exfat"] }

[[actions]]
name = "log"
tag = "usbtest"
on = ["connected", "disconnected"]
run = ["sh", "-c", "echo $1", "sh", "{device}"]

[[actions]]
name = "open"
tag = "storage"
on = ["mounted"]
allow_unnamed = true
run = ["notify-send", "mounted {mountpoint}"]

[[actions]]
name = "closed"
tag = "storage"
on = ["mounted"]
allow_unnamed = false
run = ["true"]

[[actions]]
name = "backup"
device = "backup-disk"
on = ["mounted"]
run = ["backup.sh", "{mountpoint}"]
"""

USB = Endpoint("udev", "/sys/1", {"serial": "S9x7a", "vendor_id": "1", "product_id": "2",
                                  "vendor": "V", "model": "M"})
UNKNOWN_FS = Endpoint("udisks", "/o/1", {"fs_type": "ext4", "fs_uuid": "zzz", "fs_label": "X",
                                         "mountpoint": "/media/x"})
BACKUP_FS = Endpoint("udisks", "/o/2", {"fs_type": "ext4", "fs_uuid": "U1", "mountpoint": "/media/b"})


def load(text=CONFIG):
    return C.parse(C.tomllib.loads(text))


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.engine = Engine(load(), runner=None)

    def test_plan_matches_what_handle_would_select_without_side_effects(self):
        event = Event(MOUNTED, UNKNOWN_FS)
        first, second = self.engine.plan(event), self.engine.plan(event)     # no debounce
        self.assertEqual([j.action.name for j in first.jobs], ["open"])
        self.assertEqual([j.action.name for j in second.jobs], ["open"])
        self.assertEqual([s.action.name for s in first.skipped], ["closed"])
        self.assertIn("allow_unnamed", first.skipped[0].reason)

    def test_named_things_skip_nothing(self):
        plan = self.engine.plan(Event(MOUNTED, BACKUP_FS))
        self.assertEqual([j.action.name for j in plan.jobs], ["open", "closed", "backup"])
        self.assertEqual(plan.skipped, [])


class ExplainTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load()

    def text(self, endpoint, kinds=None):
        return "\n".join(explain.explain_endpoint(self.cfg, endpoint, kinds))

    def test_a_named_usb_device(self):
        text = self.text(USB)
        self.assertIn("device: stick   tags: usbtest", text)
        self.assertIn("connected     runs 'log' (tag usbtest)", text)
        self.assertIn("sh -c 'echo $1' sh stick", text)            # command shown expanded
        self.assertIn("nothing to run for: startup, shutdown", text)

    def test_an_unnamed_drive_shows_what_runs_and_what_is_skipped(self):
        text = self.text(UNKNOWN_FS)
        self.assertIn("unnamed   tags: storage", text)
        self.assertIn("mounted       runs 'open' (tag storage, unnamed)", text)
        self.assertIn("notify-send 'mounted /media/x'", text)
        self.assertIn("mounted       skips 'closed'", text)
        self.assertNotIn("backup", text)

    def test_a_named_drive_runs_everything_for_it(self):
        text = self.text(BACKUP_FS)
        self.assertIn("device: backup-disk   tags: storage", text)
        for name in ("open", "closed", "backup"):
            self.assertIn(f"runs '{name}'", text)
        self.assertNotIn("skips", text)

    def test_a_placeholder_that_is_not_known_yet_is_explained(self):
        unmounted = Endpoint("udisks", "/o/3", {"fs_type": "ext4", "fs_uuid": "zzz"})   # no mountpoint
        text = self.text(unmounted, [MOUNTED])
        self.assertIn("(cannot run here: {mountpoint} is not available)", text)

    def test_only_the_asked_event(self):
        text = self.text(USB, [CONNECTED])
        self.assertIn("connected", text)
        self.assertNotIn("disconnected", text)

    def test_simulated_things(self):
        cfg = self.cfg
        named = explain.simulated(cfg, "stick", {"vendor_id": "9"})
        self.assertEqual(dict(named.props), {"serial": "S9x7a", "vendor_id": "9"})
        self.assertFalse(named.is_filesystem)
        self.assertTrue(explain.simulated(cfg, None, {"fs_type": "ext4"}).is_filesystem)
        self.assertTrue(explain.simulated(cfg, "stick", {}, MOUNTED).is_filesystem)   # the event decides
        self.assertFalse(explain.simulated(cfg, None, {"fs_type": "ext4"}, CONNECTED).is_filesystem)


class ExplainCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "c.toml"
        self.cfg.write_text(CONFIG)
        self.cfg.chmod(0o600)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.cfg), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_simulating_a_named_device(self):
        rc, out, _ = self.run_cli("explain", "connected", "--device", "stick")
        self.assertEqual(rc, 0)
        self.assertIn("Simulated thing (serial=S9x7a)   device: stick", out)
        self.assertIn("runs 'log'", out)
        self.assertIn("Only the properties above are known", out)

    def test_simulating_an_unnamed_drive_by_properties(self):
        rc, out, _ = self.run_cli("explain", "mounted", "--prop", "fs_type=ext4",
                                  "--prop", "mountpoint=/media/z")
        self.assertEqual(rc, 0)
        self.assertIn("unnamed   tags: storage", out)
        self.assertIn("runs 'open'", out)
        self.assertIn("skips 'closed'", out)
        self.assertIn("notify-send 'mounted /media/z'", out)

    def test_a_named_device_plus_properties(self):
        rc, out, _ = self.run_cli("explain", "mounted", "--device", "backup-disk",
                                  "--prop", "fs_type=ext4", "--prop", "mountpoint=/media/b")
        self.assertEqual(rc, 0)
        self.assertIn("runs 'backup'", out)
        self.assertNotIn("skips", out)

    def test_bad_arguments(self):
        rc, _, err = self.run_cli("explain", "--device", "ghost")
        self.assertEqual((rc, "no such device 'ghost'" in err), (1, True))
        rc, _, err = self.run_cli("explain", "--prop", "oops")
        self.assertEqual((rc, "expected key=value" in err), (1, True))
        rc, _, err = self.run_cli("explain", "--prop", "a=1", "--prop", "a=2")
        self.assertEqual((rc, "given twice" in err), (1, True))
        rc, _, err = self.run_cli("explain", "bogus")
        self.assertNotEqual(rc, 0)

    def present(self):
        return (mock.patch("udevmon.sources.udev.UdevSource.snapshot", return_value=[USB,
                Endpoint("udev", "/sys/hub", {"vendor_id": "1d6b", "product_id": "0002"})]),
                mock.patch("udevmon.sources.udisks.UdisksSource.snapshot",
                           return_value=[UNKNOWN_FS,
                                         Endpoint("udisks", "/o/9", {"fs_type": "vfat", "fs_uuid": "e"})]))

    def test_everything_present_hides_what_nothing_matches(self):
        usb, fs = self.present()
        with usb, fs:
            rc, out, _ = self.run_cli("explain")
        self.assertEqual(rc, 0)
        self.assertIn("device: stick", out)
        self.assertIn("tags: storage", out)
        self.assertIn("Not shown: 2 present thing(s)", out)
        self.assertNotIn("1d6b", out)
        usb, fs = self.present()
        with usb, fs:
            rc, out, _ = self.run_cli("explain", "--all")
        self.assertNotIn("Not shown", out)
        self.assertIn("[1d6b:0002]", out)

    def test_an_event_limits_what_is_shown(self):
        usb, fs = self.present()
        with usb, fs:
            rc, out, _ = self.run_cli("explain", "mounted")
        self.assertIn("Filesystem", out)
        self.assertNotIn("USB", out)                      # a USB device cannot be "mounted"
        self.assertNotIn("connected", out)


if __name__ == "__main__":
    unittest.main()
