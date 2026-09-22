"""Devices with no serial number: nothing identifies the unit, so `scan` asks what to tie it to,
`present` says what a match would really identify, and `check` notes rules that use port_path."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli
from udevmon import config as C
from udevmon.events import Endpoint
from udevmon.matching import lacks_identity, level_label, match_choices
from udevmon.scan import Scanner

YUBIKEY = Endpoint("udev", "/sys/yk", {"vendor_id": "1050", "product_id": "0407",
                                       "port_path": "3-4", "vendor": "Yubico",
                                       "model": "Yubikey"})
STICK = Endpoint("udev", "/sys/stick", {"vendor_id": "0781", "product_id": "55ae",
                                        "serial": "323434393551343035323530", "port_path": "2-2"})
FAKE_SERIAL = Endpoint("udev", "/sys/fake", {"vendor_id": "1", "product_id": "2",
                                             "serial": "0000000", "port_path": "1-1"})
FS = Endpoint("udisks", "/org/x", {"fs_uuid": "U1", "fs_label": "X"})
FS_NO_UUID = Endpoint("udisks", "/org/y", {"fs_label": "X"})
NO_PORT = Endpoint("udev", "/sys/np", {"vendor_id": "1", "product_id": "2"})


class HelperTests(unittest.TestCase):
    def test_who_lacks_an_identity(self):
        self.assertTrue(lacks_identity(YUBIKEY))
        self.assertTrue(lacks_identity(FAKE_SERIAL))            # a placeholder serial is no serial
        self.assertFalse(lacks_identity(STICK))
        self.assertFalse(lacks_identity(FS))                    # filesystems have their own rules
        self.assertFalse(lacks_identity(FS_NO_UUID))

    def test_the_choices_are_the_port_and_the_model_and_neither_is_preselected(self):
        self.assertEqual(match_choices(YUBIKEY),
                         [("this model, in this port only", ["vendor_id", "product_id",
                                                              "port_path"]),
                          ("this model, in any port", ["vendor_id", "product_id"])])
        self.assertEqual(match_choices(NO_PORT), [("this model, in any port",
                                                   ["vendor_id", "product_id"])])

    def test_labels_say_when_a_match_would_only_identify_the_port(self):
        self.assertEqual(level_label(STICK), "strong")
        self.assertEqual(level_label(FS), "strong")
        self.assertEqual(level_label(YUBIKEY), "medium: no serial, matches the port")
        self.assertEqual(level_label(Endpoint("udev", "k", {})), "-")


class ScanTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"

    def name(self, endpoint, answers, kind="d"):
        scanner = Scanner(self.p, prompt=True)
        scanner.loop = mock.Mock()
        scanner.pending = {"0": endpoint}
        seq, prompts = iter(["1", kind, "yk"] + answers), []

        def fake(prompt=""):
            prompts.append(prompt)
            return next(seq)
        out = io.StringIO()
        with mock.patch("builtins.input", fake), \
                mock.patch("udevmon.daemon.reload_running", return_value=False), \
                contextlib.redirect_stdout(out):
            try:
                scanner._prompt_user()
            except StopIteration:
                pass
        return out.getvalue(), prompts

    def match(self):
        return dict(C.load(self.p).devices["yk"].match)

    def test_the_explanation_and_both_choices_are_shown_without_a_default(self):
        out, prompts = self.name(YUBIKEY, [""])
        self.assertIn("no serial number, so nothing identifies\nthis particular unit.", out)
        self.assertIn("1) this model, in this port only", out)
        self.assertIn("2) this model, in any port", out)
        self.assertTrue(any(p.startswith("Number, or a list of properties") for p in prompts))
        self.assertFalse(any("Enter to accept" in p for p in prompts))     # nothing pre-filled

    def test_enter_alone_chooses_nothing(self):
        out, _ = self.name(YUBIKEY, [""])
        self.assertIn("Nothing chosen; skipped.", out)
        self.assertFalse(self.p.exists())

    def test_choice_1_ties_the_device_to_the_port(self):
        out, _ = self.name(YUBIKEY, ["1", "", "y"])
        self.assertEqual(self.match(), {"vendor_id": "1050", "product_id": "0407",
                                        "port_path": "3-4"})
        self.assertIn("(medium — matches any device of this model in this port)", out)

    def test_choice_2_matches_the_model_in_any_port(self):
        out, _ = self.name(YUBIKEY, ["2", "", "y"])
        self.assertEqual(self.match(), {"vendor_id": "1050", "product_id": "0407"})
        self.assertIn("(weak — matches every device/filesystem with these values)", out)

    def test_a_typed_list_still_works_and_a_wrong_answer_asks_again(self):
        out, _ = self.name(YUBIKEY, ["9", "vendor_id,nonsense", "vendor_id", "", "y"])
        self.assertEqual(self.match(), {"vendor_id": "1050"})
        self.assertIn("Not a property of this thing", out)

    def test_declining_to_save_a_weak_match_saves_nothing(self):
        self.name(YUBIKEY, ["2", "", "n"])
        self.assertFalse(self.p.exists())

    def test_choosing_category_instead_declares_a_tag(self):
        out, _ = self.name(YUBIKEY, ["1", ""], kind="c")
        self.assertIn("category (tag) 'yk'", out)
        self.assertEqual(dict(C.load(self.p).tags["yk"].match),
                         {"vendor_id": ("1050",), "product_id": ("0407",),
                          "port_path": ("3-4",)})
        self.assertIn("Add an action with e.g.:\n  udevmon actions add --tag yk connected "
                      "--allow-unnamed -- notify-send 'hello'", out)

    def test_category_is_the_default_for_a_thing_with_no_identity(self):
        out, prompts = self.name(YUBIKEY, ["1", ""], kind="")
        self.assertIn("category (tag) 'yk'", out)
        self.assertTrue(any(p.startswith("Choice [c]") for p in prompts))

    def test_device_is_the_default_when_there_is_a_serial(self):
        out, prompts = self.name(STICK, ["", "", ""], kind="")
        self.assertTrue(any(p.startswith("Choice [d]") for p in prompts))
        self.assertEqual(C.load(self.p).devices["yk"].match["serial"], STICK.props["serial"])

    def test_devices_with_an_identity_keep_the_old_prompt(self):
        scanner = Scanner(self.p, prompt=True)
        seq, prompts = iter([""]), []
        with mock.patch("builtins.input", lambda p="": (prompts.append(p), next(seq))[1]), \
                contextlib.redirect_stdout(io.StringIO()):
            keys = scanner._choose_keys(STICK)
        self.assertEqual(keys, ["vendor_id", "product_id", "serial"])
        self.assertTrue(prompts[0].startswith("Match on [vendor_id, product_id, serial]"))


class EmitTests(unittest.TestCase):
    def emit(self, endpoint):
        from udevmon.events import CONNECTED, Event
        scanner = Scanner(Path("/nonexistent/c.toml"), prompt=False)
        out = io.StringIO()
        with mock.patch("gi.repository.GLib.timeout_add"), contextlib.redirect_stdout(out):
            scanner._emit(Event(CONNECTED, endpoint))
        return out.getvalue(), scanner

    def test_a_device_without_a_serial_gets_no_suggestion(self):
        out, scanner = self.emit(YUBIKEY)
        self.assertIn("no serial number: nothing identifies this particular unit", out)
        self.assertNotIn("suggested match", out)
        self.assertEqual(len(scanner.pending), 1)               # it can still be named

    def test_a_device_with_a_serial_keeps_the_suggestion(self):
        out, _ = self.emit(STICK)
        self.assertIn("suggested match: vendor_id, product_id, serial  (strong)", out)


class PresentTests(unittest.TestCase):
    def test_present_labels_them(self):
        out = io.StringIO()
        with mock.patch("udevmon.sources.udev.UdevSource.snapshot", return_value=[YUBIKEY, STICK]), \
                mock.patch("udevmon.sources.udisks.UdisksSource.snapshot", return_value=[FS]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", "/nonexistent/c.toml", "present"]), 0)
        text = out.getvalue()
        self.assertIn("[medium: no serial, matches the port]", text)
        self.assertEqual(text.count("[strong]"), 2)


class CheckTests(unittest.TestCase):
    def notes(self, data):
        return C.parse(data).notes

    def test_a_device_that_matches_by_port_gets_a_note(self):
        notes = self.notes({"devices": {"dock": {"match": {"vendor_id": "1", "product_id": "2",
                                                            "port_path": "3-4"}}}})
        self.assertTrue(any("'dock' matches by port_path" in n and "another port or a hub" in n
                            for n in notes))

    def test_a_tag_rule_that_uses_the_port_gets_a_note(self):
        notes = self.notes({"tags": {"here": {"match": {"port_path": "3-4"}}}})
        self.assertTrue(any("tag rule 'here' uses port_path" in n for n in notes))

    def test_other_matches_get_no_such_note(self):
        notes = self.notes({"devices": {"k": {"match": {"serial": "S9x7a"}},
                                        "m": {"match": {"vendor_id": "1", "product_id": "2"}}}})
        self.assertFalse(any("port_path" in n for n in notes))


if __name__ == "__main__":
    unittest.main()
