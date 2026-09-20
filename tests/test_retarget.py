"""`actions set --add-device|--remove-device|--add-tag|--remove-tag`."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli, completion
from udevmon import config as C
from udevmon import configedit as E

HAND = """\
# my devices
[devices.stick]
match = { serial = "S9x7a" }
tags = ["usbtest"]

[devices.disk]
match = { fs_uuid = "U1" }

[devices.card]
match = { vendor_id = "1", product_id = "2" }

[tags.storage]
match = { fs_type = ["ext4"] }

[[actions]]                       # one device, as a plain string
name = "solo"
device = "stick"
on = ["connected"]
run = ["true"]

[[actions]]
name = "several"
device = [
  "stick",   # the key
  "disk",
]
on = ["mounted"]
run = ["true"]
timeout = 45   # seconds

[[actions]]
name = "bytag"
tag = "usbtest"
on = ["connected"]
allow_unnamed = true
run = ["true"]

[[actions]]
name = "bytags"
tag = ["usbtest", "storage"]
on = ["mounted"]
run = ["true"]
"""


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.p.write_text(HAND)
        self.p.chmod(0o600)

    def action(self, name):
        return next(a for a in C.load(self.p).actions if a.name == name)


class EditTests(Base):
    def test_a_string_grows_into_a_list_and_shrinks_back(self):
        self.assertEqual(E.set_action_options(self.p, "solo", add_devices=["disk"]),
                         ["stick", "disk"])
        self.assertEqual(self.action("solo").devices, ("stick", "disk"))
        self.assertEqual(E.set_action_options(self.p, "solo", remove_devices=["stick"]), ["disk"])
        self.assertEqual(self.action("solo").devices, ("disk",))   # a one-name list is fine

    def test_replacing_the_only_name_keeps_a_plain_string(self):
        E.set_action_options(self.p, "solo", add_devices=["disk"], remove_devices=["stick"])
        self.assertIn('name = "solo"\ndevice = "disk"\n', self.p.read_text())

    def test_an_array_is_edited_in_place_keeping_comments_and_layout(self):
        E.set_action_options(self.p, "several", add_devices=["card"], remove_devices=["disk"])
        self.assertEqual(self.action("several").devices, ("stick", "card"))
        text = self.p.read_text()
        self.assertIn('"stick",   # the key', text)
        self.assertIn("timeout = 45   # seconds", text)
        self.assertIn("# my devices", text)
        self.assertIn("[[actions]]                       # one device, as a plain string", text)
        self.assertEqual(self.p.with_name("c.toml.bak").read_text(), HAND)

    def test_tags(self):
        self.assertEqual(E.set_action_options(self.p, "bytag", add_tags=["storage", "new"]),
                         ["usbtest", "storage", "new"])          # a tag needs no declaration
        self.assertEqual(self.action("bytag").tags, ("usbtest", "storage", "new"))
        E.set_action_options(self.p, "bytags", remove_tags=["usbtest"])
        self.assertEqual(self.action("bytags").tags, ("storage",))
        self.assertIs(self.action("bytag").allow_unnamed, True)   # untouched

    def test_several_at_once_and_along_with_other_options(self):
        E.set_action_options(self.p, "several", add_devices=["card", "card"], on=["unmounted"],
                             timeout=9)
        a = self.action("several")
        self.assertEqual((a.devices, a.on, a.timeout), (("stick", "disk", "card"),
                                                        ("unmounted",), 9.0))

    def test_adding_what_is_there_changes_nothing(self):
        before = self.p.read_bytes()
        self.assertIsNone(E.set_action_options(self.p, "several", add_devices=["stick"]))
        self.assertEqual(self.p.read_bytes(), before)
        self.assertFalse(self.p.with_name("c.toml.bak").exists())

    def test_refusals_write_nothing(self):
        before = self.p.read_bytes()
        cases = {
            "unknown device": ("solo", {"add_devices": ["ghost"]}, "'ghost'"),
            "device not targeted": ("solo", {"remove_devices": ["disk"]}, "no device(s) ['disk']"),
            "tag not targeted": ("bytag", {"remove_tags": ["storage"]}, "no tag(s) ['storage']"),
            "would leave none": ("solo", {"remove_devices": ["stick"]}, "no devices"),
            "would leave no tags": ("bytag", {"remove_tags": ["usbtest"]}, "no tags"),
            "devices on a tag action": ("bytag", {"add_devices": ["disk"]}, "targets tags"),
            "tags on a device action": ("solo", {"add_tags": ["storage"]}, "targets devices"),
            "both kinds": ("solo", {"add_devices": ["disk"], "add_tags": ["x"]}, "not both"),
            "added and removed": ("several", {"add_devices": ["card"], "remove_devices": ["card"]},
                                  "both"),
            "bad name": ("bytag", {"add_tags": ["no spaces"]}, "invalid"),
            "no such action": ("ghost", {"add_devices": ["disk"]}, "no such action"),
        }
        for label, (name, kwargs, text) in cases.items():
            with self.subTest(label):
                with self.assertRaises(C.ConfigError) as cm:
                    E.set_action_options(self.p, name, **kwargs)
                self.assertIn(text, str(cm.exception))
                self.assertEqual(self.p.read_bytes(), before)

    def test_an_edit_that_does_nothing_is_rolled_back(self):
        before = self.p.read_bytes()
        with mock.patch.object(E, "_edited", return_value=HAND):
            with self.assertRaises(C.ConfigError):
                E.set_action_options(self.p, "solo", add_devices=["disk"])
        self.assertEqual(self.p.read_bytes(), before)

    def test_crlf_files_keep_their_line_endings(self):
        self.p.write_bytes(HAND.replace("\n", "\r\n").encode())
        E.set_action_options(self.p, "several", add_devices=["card"])
        raw = self.p.read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))
        self.assertEqual(self.action("several").devices, ("stick", "disk", "card"))


class CliTests(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.p), "actions", "set", *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_add_and_remove_report_the_new_targets_and_reload(self):
        rc, out, _ = self.run_cli("solo", "--add-device", "disk", "--add-device", "card")
        self.assertEqual(rc, 0)
        self.assertIn("Updated action 'solo'.", out)
        self.assertIn("It now targets devices: stick, disk, card.", out)
        self.assertIn("Reloaded the running daemon.", out)
        rc, out, _ = self.run_cli("bytag", "--add-tag", "storage", "--remove-tag", "usbtest")
        self.assertIn("It now targets tags: storage.", out)

    def test_nothing_to_do_is_said(self):
        rc, out, _ = self.run_cli("several", "--add-device", "disk")
        self.assertEqual(rc, 0)
        self.assertIn("already targets that; nothing changed", out)

    def test_errors_exit_1_with_a_message(self):
        rc, _, err = self.run_cli("solo", "--remove-device", "stick")
        self.assertEqual(rc, 1)
        self.assertIn("remove the action instead", err)
        rc, _, err = self.run_cli("solo")
        self.assertEqual(rc, 1)
        self.assertIn("--add-device", err)                  # the hint lists the new options

    def test_check_sees_the_result(self):
        self.run_cli("solo", "--add-device", "disk")
        with contextlib.redirect_stdout(io.StringIO()):
            rc = cli.main(["--config", str(self.p), "check"])
        self.assertEqual(rc, 0)


class CompletionTests(Base):
    def words(self, line):
        parts = line.split(" ")
        parts[1:1] = ["--config", str(self.p)]
        return completion.complete(cli.build_parser(), parts, len(parts) - 1)[1]

    def test_add_offers_every_name_and_remove_only_the_actions_own(self):
        self.assertEqual(self.words("udevmon actions set solo --add-device "),
                         ["stick", "disk", "card"])
        self.assertEqual(self.words("udevmon actions set several --remove-device "),
                         ["stick", "disk"])
        self.assertEqual(self.words("udevmon actions set solo --remove-device "), ["stick"])
        self.assertEqual(self.words("udevmon actions set bytags --remove-tag "),
                         ["usbtest", "storage"])
        self.assertEqual(self.words("udevmon actions set solo --add-tag st"), ["storage"])

    def test_without_the_action_yet_remove_offers_what_any_action_targets(self):
        self.assertEqual(self.words("udevmon actions set --remove-device "), ["stick", "disk"])

    def test_the_options_are_offered(self):
        offered = self.words("udevmon actions set solo --")
        for opt in ("--add-device", "--remove-device", "--add-tag", "--remove-tag"):
            self.assertIn(opt, offered)


if __name__ == "__main__":
    unittest.main()
