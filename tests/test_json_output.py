# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""The interface for scripts: --json output, `version`, `devices identify`, --dry-run."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli
from udevmon.events import Endpoint

MAIN = '''[devices.key]
match = { fs_uuid = "AB12-CD34", drive_serial = "1111222233334444" }
tags = ["keepass"]

[tags.storage]
match = { fs_type = "vfat" }

[[actions]]
name = "log"
device = "key"
on = ["mounted"]
run = ["/bin/true", "{mountpoint}"]
'''

DROPIN = '''[devices.stick]
match = { fs_uuid = "EF56-7890", fs_type = "vfat" }
status = "disabled"

[[actions]]
name = "sync"
tag = "keepass"
on = ["mounted"]
run = ["/bin/true"]
lock = "keydisk"
timeout = 120
notify_on_failure = true
'''


def mounted(uuid="AB12-CD34", serial="1111222233334444", mountpoint="/media/u/KEY"):
    props = {"fs_uuid": uuid, "fs_type": "vfat", "mountpoint": mountpoint, "instance": "i"}
    if serial is not None:
        props["drive_serial"] = serial
    return Endpoint("udisks", "/org/x", props)


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.cfg = self.dir / "config.toml"
        self.cfg.write_text(MAIN)
        (self.dir / "config.d").mkdir(mode=0o700)
        self.dropin = self.dir / "config.d" / "50-tool.toml"
        self.dropin.write_text(DROPIN)
        for f in (self.cfg, self.dropin):
            os.chmod(f, 0o600)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.cfg), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def json_of(self, *args):
        rc, out, err = self.run_cli(*args)
        self.assertEqual(rc, 0, err)
        return json.loads(out)


class ListingTests(Base):
    def test_devices_say_which_file_defines_them(self):
        devices = {d["name"]: d for d in self.json_of("devices", "list", "--json")}
        self.assertEqual((devices["key"]["file"], devices["key"]["dropin"]),
                         (str(self.cfg), None))
        self.assertEqual((devices["stick"]["file"], devices["stick"]["dropin"]),
                         (str(self.dropin), "50-tool"))
        self.assertEqual(devices["stick"]["status"], "disabled")
        self.assertEqual(devices["key"]["match"],
                         {"fs_uuid": "AB12-CD34", "drive_serial": "1111222233334444"})
        self.assertEqual(devices["key"]["actions"], ["log", "sync"])

    def test_rule_tags_come_from_the_match_keys(self):
        devices = {d["name"]: d for d in self.json_of("devices", "list", "--json")}
        self.assertEqual(devices["stick"]["rule_tags"], ["storage"])
        self.assertEqual(devices["key"]["rule_tags"], [])          # its fs_type is unknown
        self.assertEqual(devices["key"]["tags"], ["keepass"])

    def test_devices_show(self):
        self.assertEqual(self.json_of("devices", "show", "key", "--json")["name"], "key")
        self.assertEqual(self.run_cli("devices", "show", "nope", "--json")[0], 1)

    def test_actions_give_every_field(self):
        actions = {a["name"]: a for a in self.json_of("actions", "list", "--json")}
        sync = actions["sync"]
        self.assertEqual((sync["tags"], sync["lock"], sync["timeout"], sync["notify_on_failure"],
                          sync["dropin"], sync["order"], sync["enabled"]),
                         (["keepass"], "keydisk", 120, True, "50-tool", 50, True))
        self.assertEqual(actions["log"]["run"], ["/bin/true", "{mountpoint}"])

    def test_tags(self):
        tags = {t["name"]: t for t in self.json_of("tags", "list", "--json")}
        self.assertEqual(tags["storage"]["rule"], {"fs_type": ["vfat"]})
        self.assertTrue(tags["storage"]["declared"])
        self.assertEqual((tags["keepass"]["declared"], tags["keepass"]["members"],
                          tags["keepass"]["actions"], tags["keepass"]["file"]),
                         (False, ["key"], ["sync"], None))
        self.assertEqual(self.json_of("tags", "show", "storage", "--json")["name"], "storage")


class CheckTests(Base):
    def test_ok(self):
        data = self.json_of("check", "--json")
        self.assertTrue(data["ok"])
        self.assertEqual((data["devices"], data["tags"], data["actions"]), (2, 1, 2))
        self.assertEqual([f["dropin"] for f in data["files"]], [None, "50-tool"])
        self.assertEqual(data["files"][1]["devices"], 1)

    def test_errors_are_in_the_json_and_give_status_1(self):
        self.cfg.write_text('[devices.a]\nmatch = {}\n')
        rc, out, _ = self.run_cli("check", "--json")
        data = json.loads(out)
        self.assertEqual((rc, data["ok"]), (1, False))
        self.assertTrue(any("non-empty" in e for e in data["errors"]))


class ExplainTests(Base):
    def test_simulated(self):
        data = self.json_of("explain", "mounted", "--device", "key", "--prop",
                            "mountpoint=/media/u/KEY", "--json")
        self.assertTrue(data["simulated"])
        thing = data["things"][0]
        self.assertEqual(thing["devices"], ["key"])
        runs = {r["action"]: r for r in thing["events"]["mounted"]["runs"]}
        self.assertEqual(runs["log"]["command"], ["/bin/true", "/media/u/KEY"])
        self.assertEqual(runs["log"]["via"], {"device": "key"})
        self.assertEqual((runs["sync"]["via"], runs["sync"]["dropin"]),
                         ({"tag": "keepass"}, "50-tool"))

    def test_a_missing_placeholder(self):
        data = self.json_of("explain", "mounted", "--device", "key", "--json")
        run = data["things"][0]["events"]["mounted"]["runs"][0]
        self.assertEqual((run["command"], run["missing"]), (None, "mountpoint"))

    def test_the_text_still_says_the_same(self):
        rc, out, _ = self.run_cli("explain", "mounted", "--device", "key")
        self.assertIn("runs 'sync' (tag keepass, lock keydisk, from config.d/50-tool.toml)",
                      out)
        self.assertIn("cannot run here: {mountpoint} is not available", out)


class VersionTests(Base):
    def test_plain_and_json(self):
        rc, out, _ = self.run_cli("version")
        self.assertEqual(out.splitlines()[0], cli.__version__)
        data = self.json_of("version", "--json")
        self.assertEqual(data["version"], cli.__version__)
        self.assertIn("identify", data["features"])
        self.assertEqual(len(set(cli.FEATURES)), len(cli.FEATURES))

    def test_behaviour_changes_have_names_too(self):
        for name in ("reload-result", "max-workers-commands", "shutdown-retries",
                     "retry-backoff", "trigger-output", "dropin-script-dir",
                     "action-program", "apply", "status",
                     "action-env-cwd", "disconnect-surprise", "check-json-findings"):
            self.assertIn(name, cli.FEATURES)

    def test_the_version_option_stays_a_bare_number(self):
        rc, out, _ = self.run_cli("--version")
        self.assertEqual((rc, out), (0, cli.__version__ + "\n"))


class IdentifyTests(Base):
    def identify(self, endpoint, *extra):
        with mock.patch("udevmon.sources.udisks.UdisksSource.mounted_at",
                        return_value=endpoint):
            return self.run_cli("devices", "identify", "/media/u/KEY", *extra)

    def test_a_named_drive(self):
        rc, out, _ = self.identify(mounted(), "--json")
        data = json.loads(out)
        self.assertEqual(data["match"], {"fs_uuid": "AB12-CD34",
                                         "drive_serial": "1111222233334444"})
        self.assertEqual((data["fs_uuid"], data["drive_serial"]),
                         ("AB12-CD34", "1111222233334444"))
        self.assertEqual(data["devices"], [{"name": "key", "status": "active",
                                            "file": str(self.cfg), "dropin": None}])
        self.assertIn("storage", data["tags"])
        rc, out, _ = self.identify(mounted())
        self.assertIn("device: key", out)

    def test_a_drive_without_a_serial(self):
        rc, out, _ = self.identify(mounted(uuid="NEW", serial=None), "--json")
        data = json.loads(out)
        self.assertEqual((rc, data["match"], data["devices"]), (0, None, []))
        self.assertIn("no serial number", data["problem"])

    def test_nothing_mounted_there(self):
        rc, out, err = self.identify(None, "--json")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("not the mount point", err)


class DryRunTests(Base):
    def setUp(self):
        super().setUp()
        self.before = {f: f.read_text() for f in (self.cfg, self.dropin)}
        reload = mock.patch("udevmon.daemon.reload_running", return_value=True)
        self.reload = reload.start()
        self.addCleanup(reload.stop)

    def assertUnchanged(self):
        for f, text in self.before.items():
            self.assertEqual(f.read_text(), text)
        self.assertEqual(sorted(p.name for p in self.dir.rglob("*")),
                         ["50-tool.toml", "config.d", "config.toml"])
        self.reload.assert_not_called()

    def test_an_edit_shows_a_diff_of_the_right_file(self):
        rc, out, _ = self.run_cli("devices", "enable", "stick", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn(f"--- {self.dropin}\n+++ {self.dropin}\n", out)
        self.assertIn('-status = "disabled"', out)
        self.assertIn("Enabled: stick.", out)
        self.assertTrue(out.endswith("Dry run: nothing was written.\n"))
        self.assertUnchanged()

    def test_a_new_dropin(self):
        target = self.dir / "config.d" / "60-new.toml"
        rc, out, _ = self.run_cli("devices", "add", "x", "--match", "fs_uuid=X",
                                  "--file", "60-new", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn(f"--- /dev/null\n+++ {target}\n", out)
        self.assertIn("+[devices.x]", out)
        self.assertUnchanged()

    def test_a_refused_edit_keeps_its_status_and_message(self):
        rc, out, err = self.run_cli("devices", "remove", "key", "--dry-run")
        self.assertEqual(rc, 1)
        self.assertIn("error:", err)
        self.assertNotIn("dry-run-", err)               # the copy's folder is not shown
        self.assertUnchanged()

    def test_no_change(self):
        rc, out, _ = self.run_cli("devices", "tag", "key", "keepass", "--dry-run")
        self.assertEqual(rc, 0)
        self.assertIn("No file would change.", out)
        self.assertUnchanged()

    def test_without_dry_run_the_edit_happens(self):
        self.assertEqual(self.run_cli("devices", "enable", "stick")[0], 0)
        self.assertNotIn("disabled", self.dropin.read_text())
        self.reload.assert_called()


if __name__ == "__main__":
    unittest.main()
