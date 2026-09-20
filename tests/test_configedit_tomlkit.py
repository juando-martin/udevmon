"""Edits to existing entries go through tomlkit: they must keep however the file was written."""

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli
from udevmon import config as C
from udevmon import configedit as E


def write(text, newline=None):
    p = Path(tempfile.mkdtemp()) / "c.toml"
    with open(p, "w", newline=newline) as f:
        f.write(text)
    p.chmod(0o600)
    return p


HAND = '''# hand-written config
# about the stick
[devices.stick]   # my stick
match = { serial = "S9x7a" }
tags = [
  "usbtest",   # the first
  "backup",
]

[devices.disk]
match = { fs_uuid = "U1" }

[devices.card]                    # sub-table form instead of an inline table
[devices.card.match]
vendor_id = "2ce3"
product_id = "9563"
port_path = "3-3"

[devices.pair]
match = { serial = "S9x7p" }

[devices.notes]
match = { serial = "S9x7z" }
description = """a multi-line description
[not a header] but this line starts with a bracket
"""

[[actions]]   # the backup
name = "backup"
device = [
  "stick",
  "pair",
]
on = ["mounted"]
run = [
  "sh", "-c",
  "rsync -a ~/Documents/",
  "sh", "{mountpoint}",
]
'''


class HandFormattedTests(unittest.TestCase):
    """The files that defeated the line editor."""

    def test_the_file_is_valid_to_begin_with(self):
        p = write(HAND)
        cfg = C.load(p)
        self.assertEqual(list(cfg.devices), ["stick", "disk", "card", "pair", "notes"])
        self.assertEqual(cfg.actions[0].devices, ("stick", "pair"))

    def test_a_sub_table_device_can_be_removed(self):
        p = write(HAND)
        E.remove_device(p, "card")
        self.assertEqual(list(C.load(p).devices), ["stick", "disk", "pair", "notes"])
        self.assertNotIn("devices.card", p.read_text())
        self.assertNotIn("port_path", p.read_text())

    def test_a_device_with_a_multiline_string_can_be_removed(self):
        p = write(HAND)
        E.remove_device(p, "notes")
        self.assertEqual(list(C.load(p).devices), ["stick", "disk", "card", "pair"])
        self.assertNotIn("not a header", p.read_text())
        self.assertIn("[devices.card.match]", p.read_text())            # its neighbour is intact

    def test_only_the_removed_block_changes(self):
        p = write(HAND)
        E.remove_device(p, "disk")
        expected = HAND.replace('[devices.disk]\nmatch = { fs_uuid = "U1" }\n\n', "")
        self.assertEqual(p.read_text(), expected)                       # nothing else moved

    def test_an_action_with_multiline_values_can_be_removed(self):
        p = write(HAND)
        E.remove_action(p, "backup")
        self.assertEqual(C.load(p).actions, [])
        self.assertNotIn("rsync", p.read_text())
        self.assertIn("# about the stick", p.read_text())

    def test_tags_in_a_multiline_array_keep_their_layout_and_comments(self):
        p = write(HAND)
        E.set_device_tags(p, "stick", add=["extra"], remove=["backup"])
        self.assertEqual(C.load(p).devices["stick"].tags, ("usbtest", "extra"))
        text = p.read_text()
        self.assertIn('"usbtest",   # the first', text)                 # the inline comment survived
        self.assertIn("[devices.stick]   # my stick", text)

    def test_removing_the_last_tag_deletes_the_key(self):
        p = write(HAND)
        E.set_device_tags(p, "stick", remove=["usbtest", "backup"])
        self.assertEqual(C.load(p).devices["stick"].tags, ())
        self.assertNotIn("tags", p.read_text().split("[devices.disk]")[0])

    def test_a_bare_string_tag_becomes_a_list(self):
        p = write('[devices.d]\nmatch = { serial = "S9x7a" }\ntags = "solo"\n')
        E.set_device_tags(p, "d", add=["second"])
        self.assertEqual(C.load(p).devices["d"].tags, ("solo", "second"))

    def test_actions_written_as_an_inline_array(self):
        p = write('actions = [\n  { name = "a", device = "d", on = ["connected"], run = ["true"] },\n'
                  '  { name = "b", device = "d", on = ["connected"], run = ["true"] },\n]\n\n'
                  '[devices.d]\nmatch = { serial = "S9x7a" }\n')
        E.remove_action(p, "a")
        self.assertEqual([a.name for a in C.load(p).actions], ["b"])

    def test_the_last_device_and_tag_can_go(self):
        p = write('[devices.only]\nmatch = { serial = "S9x7a" }\n\n[tags.t]\nmatch = { fs_type = "ext4" }\n')
        E.remove_device(p, "only")
        E.remove_tag(p, "t")
        cfg = C.load(p)
        self.assertEqual((cfg.devices, cfg.tags), ({}, {}))

    def test_a_dotted_key_layout(self):
        p = write('[devices]\nx.match = { serial = "S9x7a" }\ny.match = { serial = "S9x7b" }\n')
        E.remove_device(p, "x")
        self.assertEqual(list(C.load(p).devices), ["y"])


class NewlineTests(unittest.TestCase):
    def test_windows_line_endings_survive_an_edit_and_a_rollback(self):
        text = HAND.replace("\n", "\r\n")
        p = write(text, newline="")
        E.remove_device(p, "disk")
        raw = p.read_bytes()
        self.assertIn(b"\r\n", raw)
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))              # no bare newline crept in
        before = p.read_bytes()
        with self.assertRaises(C.ConfigError):
            E.remove_device(p, "stick")                                 # an action names it: refused
        self.assertEqual(p.read_bytes(), before)

    def test_a_file_with_no_final_newline(self):
        p = write('[devices.a]\nmatch = { serial = "S9x7a" }\n\n[devices.b]\nmatch = { serial = "S9x7b" }')
        E.remove_device(p, "a")
        self.assertEqual(p.read_text(), '[devices.b]\nmatch = { serial = "S9x7b" }')


class SafetyNetTests(unittest.TestCase):
    def test_a_refused_edit_leaves_the_file_byte_for_byte(self):
        p = write(HAND)
        before = p.read_bytes()
        for call in (lambda: E.remove_device(p, "stick"),               # used by an action
                     lambda: E.remove_device(p, "ghost"),
                     lambda: E.set_device_tags(p, "stick", remove=["nope"]),
                     lambda: E.remove_tag(p, "nope"),
                     lambda: E.remove_action(p, "nope")):
            with self.assertRaises(C.ConfigError):
                call()
        self.assertEqual(p.read_bytes(), before)

    def test_a_result_that_does_not_do_what_was_asked_is_rolled_back(self):
        p = write(HAND)
        before = p.read_bytes()
        with mock.patch.object(E, "_edited", return_value=HAND):        # an edit that changes nothing
            with self.assertRaises(C.ConfigError):
                E.remove_device(p, "disk")
        self.assertEqual(p.read_bytes(), before)

    def test_every_kind_of_edit_is_checked_and_rolled_back(self):
        """If tomlkit's result did not do what was asked, nothing is written."""
        text = HAND + '\n[tags.rule]\nmatch = { fs_type = "ext4" }\n'
        operations = {
            "remove device": lambda p: E.remove_device(p, "disk"),
            "remove tag": lambda p: E.remove_tag(p, "rule"),
            "remove action": lambda p: E.remove_action(p, "backup"),
            "add tag to device": lambda p: E.set_device_tags(p, "disk", add=["t1"]),
            "remove tag from device": lambda p: E.set_device_tags(p, "stick", remove=["backup"]),
        }
        for label, operation in operations.items():
            with self.subTest(label):
                p = write(text)
                before = p.read_bytes()
                with mock.patch.object(E, "_edited", return_value=text):     # a no-op "edit"
                    with self.assertRaises(C.ConfigError):
                        operation(p)
                self.assertEqual(p.read_bytes(), before)

    def test_an_action_without_an_explicit_name_is_explained(self):
        p = write('[devices.d]\nmatch = { serial = "S9x7a" }\n\n[[actions]]\ndevice = "d"\n'
                  'on = ["connected"]\nrun = ["true"]\n')
        name = C.load(p).actions[0].name                                     # generated: d-1
        before = p.read_bytes()
        with self.assertRaises(C.ConfigError) as cm:
            E.remove_action(p, name)
        self.assertIn("no explicit name", str(cm.exception))
        self.assertEqual(p.read_bytes(), before)

    def test_a_backup_is_kept(self):
        p = write(HAND)
        E.remove_device(p, "disk")
        self.assertEqual(p.with_name("c.toml.bak").read_text(), HAND)

    def test_a_missing_tomlkit_is_explained_and_adding_still_works(self):
        p = write(HAND)
        before = p.read_bytes()
        with mock.patch.dict(sys.modules, {"tomlkit": None}):
            with self.assertRaises(C.ConfigError) as cm:
                E.remove_device(p, "disk")
            self.assertIn("tomlkit", str(cm.exception))
            self.assertEqual(p.read_bytes(), before)
            E.add_device(p, "fresh", {"serial": "S9x7q"})                # appending needs no tomlkit
            E.add_tag(p, "storage", {"fs_type": "ext4"})
        self.assertIn("fresh", C.load(p).devices)

    def test_the_cli_reports_a_missing_tomlkit_cleanly(self):
        p = write(HAND)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(sys.modules, {"tomlkit": None}), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit) as raised:
                cli.main(["--config", str(p), "devices", "remove", "disk", "--no-reload"])
        self.assertEqual(raised.exception.code, 1)
        self.assertIn("pip install tomlkit", err.getvalue())


if __name__ == "__main__":
    unittest.main()
