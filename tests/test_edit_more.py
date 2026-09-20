"""`actions set ... -- COMMAND` (replace the command) and `tags set` (rule, description)."""

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
# my config
[devices.stick]
match = { serial = "S9x7a" }
tags = ["usbtest"]

[tags.storage]                    # drives I care about
description = "Filesystems"
match = { fs_type = ["ext4", "exfat"] }   # any of these

[tags.split]
[tags.split.match]
fs_type = "vfat"
vendor_id = "0781"

[tags.plain]

[[actions]]
name = "solo"
device = "stick"
on = ["connected"]
run = ["notify-send", "old"]   # what to run
timeout = 30

[[actions]]
name = "asstring"
device = "stick"
on = ["mounted"]
run = "notify-send 'in a string' {mountpoint}"

[[actions]]
name = "bytag"
tag = "storage"
on = ["mounted"]
allow_unnamed = false
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

    def tag(self, name):
        return C.load(self.p).tags[name]


class ReplaceCommandTests(Base):
    def test_the_command_is_replaced_and_the_rest_kept(self):
        E.set_action_options(self.p, "solo", run=["sh", "-c", 'echo "$1"', "sh", "{device}"])
        a = self.action("solo")
        self.assertEqual(a.run, ("sh", "-c", 'echo "$1"', "sh", "{device}"))
        self.assertEqual((a.on, a.timeout), (("connected",), 30.0))
        text = self.p.read_text()
        self.assertIn("# what to run", text)                     # the comment survived
        self.assertIn("# my config", text)
        self.assertEqual(self.p.with_name("c.toml.bak").read_text(), HAND)

    def test_a_command_written_as_a_string_becomes_a_list(self):
        E.set_action_options(self.p, "asstring", run=["true"])
        self.assertEqual(self.action("asstring").run, ("true",))

    def test_along_with_other_options(self):
        E.set_action_options(self.p, "solo", run=["true"], timeout=5, on=["disconnected"])
        a = self.action("solo")
        self.assertEqual((a.run, a.timeout, a.on), (("true",), 5.0, ("disconnected",)))

    def test_a_placeholder_the_event_lacks_is_refused_and_rolled_back(self):
        before = self.p.read_bytes()
        with self.assertRaises(C.ConfigError) as cm:
            E.set_action_options(self.p, "solo", run=["echo", "{mountpoint}"])   # `connected`
        self.assertIn("mountpoint", str(cm.exception))
        self.assertEqual(self.p.read_bytes(), before)

    def test_an_empty_command_and_nothing_at_all_are_refused(self):
        before = self.p.read_bytes()
        with self.assertRaises(C.ConfigError) as cm:
            E.set_action_options(self.p, "solo", run=[])
        self.assertIn("must not be empty", str(cm.exception))
        with self.assertRaises(C.ConfigError) as cm:
            E.set_action_options(self.p, "solo")
        self.assertIn("after --", str(cm.exception))
        self.assertEqual(self.p.read_bytes(), before)

    def test_an_edit_that_does_nothing_is_rolled_back(self):
        before = self.p.read_bytes()
        with mock.patch.object(E, "_edited", return_value=HAND):
            with self.assertRaises(C.ConfigError):
                E.set_action_options(self.p, "solo", run=["true"])
        self.assertEqual(self.p.read_bytes(), before)


class TagSetTests(Base):
    def test_the_rule_is_replaced_keeping_a_comment_after_it(self):
        E.set_tag_fields(self.p, "storage", match={"fs_type": ["ntfs", "vfat"], "fs_label": "X"})
        self.assertEqual(dict(self.tag("storage").match),
                         {"fs_type": ("ntfs", "vfat"), "fs_label": ("X",)})
        text = self.p.read_text()
        self.assertIn("# any of these", text)
        self.assertIn("# drives I care about", text)
        self.assertIn('description = "Filesystems"', text)
        self.assertEqual(self.p.with_name("c.toml.bak").read_text(), HAND)

    def test_a_rule_written_as_a_sub_table(self):
        E.set_tag_fields(self.p, "split", match={"fs_type": "ext4"})
        self.assertEqual(dict(self.tag("split").match), {"fs_type": ("ext4",)})
        self.assertIn("[tags.split.match]", self.p.read_text())

    def test_a_tag_without_a_rule_can_get_one(self):
        E.set_tag_fields(self.p, "plain", match={"vendor_id": "0781"})
        self.assertEqual(dict(self.tag("plain").match), {"vendor_id": ("0781",)})

    def test_the_rule_can_be_removed(self):
        E.set_tag_fields(self.p, "storage", clear_rule=True)
        self.assertEqual(dict(self.tag("storage").match), {})
        self.assertNotIn("any of these", self.p.read_text())
        E.set_tag_fields(self.p, "split", clear_rule=True)
        self.assertEqual(dict(self.tag("split").match), {})
        self.assertNotIn("vendor_id", self.p.read_text())

    def test_the_description(self):
        E.set_tag_fields(self.p, "storage", description="New words")
        self.assertEqual(self.tag("storage").description, "New words")
        self.assertEqual(dict(self.tag("storage").match), {"fs_type": ("ext4", "exfat")})
        E.set_tag_fields(self.p, "storage", description="")
        self.assertNotIn("description", self.p.read_text().split("[tags.split]")[0])
        E.set_tag_fields(self.p, "plain", description="Now described")
        self.assertEqual(self.tag("plain").description, "Now described")

    def test_refusals_write_nothing(self):
        before = self.p.read_bytes()
        cases = {
            "not declared (only devices carry it)": ("usbtest", {"match": {"fs_type": "x"}},
                                                     "no declared tag"),
            "no such tag": ("ghost", {"description": "x"}, "no declared tag"),
            "nothing to do": ("storage", {}, "nothing to change"),
            "empty rule": ("storage", {"match": {}}, "at least one"),
            "rule and no rule": ("storage", {"match": {"fs_type": "x"}, "clear_rule": True},
                                 "not both"),
            "volatile key": ("storage", {"match": {"mountpoint": "/x"}}, "changes between plugs"),
        }
        for label, (name, kwargs, text) in cases.items():
            with self.subTest(label):
                with self.assertRaises(C.ConfigError) as cm:
                    E.set_tag_fields(self.p, name, **kwargs)
                self.assertIn(text, str(cm.exception))
                self.assertEqual(self.p.read_bytes(), before)

    def test_an_edit_that_does_nothing_is_rolled_back(self):
        before = self.p.read_bytes()
        with mock.patch.object(E, "_edited", return_value=HAND):
            with self.assertRaises(C.ConfigError):
                E.set_tag_fields(self.p, "storage", match={"fs_type": "ntfs"})
        self.assertEqual(self.p.read_bytes(), before)

    def test_crlf_files_keep_their_line_endings(self):
        self.p.write_bytes(HAND.replace("\n", "\r\n").encode())
        E.set_tag_fields(self.p, "storage", match={"fs_type": "ntfs"})
        raw = self.p.read_bytes()
        self.assertEqual(raw.count(b"\n"), raw.count(b"\r\n"))


class CliTests(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.p), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_actions_set_with_a_command_after_the_dashes(self):
        rc, out, _ = self.run_cli("actions", "set", "solo", "--timeout", "9", "--",
                                  "sh", "-c", 'echo "$1"', "sh", "{device}")
        self.assertEqual(rc, 0)
        self.assertIn("Updated action 'solo'.", out)
        self.assertIn("Its command is now: sh -c echo \"$1\" sh {device}", out)
        self.assertIn("Reloaded the running daemon.", out)
        a = self.action("solo")
        self.assertEqual((a.run, a.timeout), (("sh", "-c", 'echo "$1"', "sh", "{device}"), 9.0))

    def test_actions_set_without_anything_to_change_says_how(self):
        rc, _, err = self.run_cli("actions", "set", "solo")
        self.assertEqual(rc, 1)
        self.assertIn("a new command after --", err)

    def test_a_bad_placeholder_is_reported_and_nothing_changes(self):
        before = self.p.read_bytes()
        rc, _, err = self.run_cli("actions", "set", "solo", "--", "echo", "{mountpoint}")
        self.assertEqual(rc, 1)
        self.assertIn("mountpoint", err)
        self.assertEqual(self.p.read_bytes(), before)

    def test_tags_set(self):
        rc, out, _ = self.run_cli("tags", "set", "storage", "--match", "fs_type=ntfs",
                                  "--match", "fs_type=vfat", "--description", "Two kinds")
        self.assertEqual(rc, 0)
        self.assertIn("Updated tag 'storage'", out)
        self.assertIn("Reloaded the running daemon.", out)
        self.assertEqual(dict(self.tag("storage").match), {"fs_type": ("ntfs", "vfat")})
        self.assertEqual(self.tag("storage").description, "Two kinds")
        rc, out, _ = self.run_cli("tags", "set", "storage", "--no-rule")
        self.assertEqual(rc, 0)
        self.assertIn("no rule now", out)
        rc, out, _ = self.run_cli("tags", "show", "storage")
        self.assertEqual(rc, 0)

    def test_tags_set_errors(self):
        rc, _, err = self.run_cli("tags", "set", "usbtest", "--match", "fs_type=x")
        self.assertEqual(rc, 1)
        self.assertIn("no declared tag", err)
        rc, _, err = self.run_cli("tags", "set", "storage")
        self.assertEqual(rc, 1)
        self.assertIn("nothing to change", err)
        rc, _, _ = self.run_cli("tags", "set", "storage", "--match", "fs_type=x", "--no-rule")
        self.assertEqual(rc, 2)                              # argparse: mutually exclusive
        rc, _, err = self.run_cli("tags", "set", "storage", "--match", "nonsense")
        self.assertEqual(rc, 1)


class CompletionTests(Base):
    def words(self, line):
        parts = line.split(" ")
        parts[1:1] = ["--config", str(self.p)]
        return completion.complete(cli.build_parser(), parts, len(parts) - 1)[1]

    def test_tags_set(self):
        self.assertEqual(self.words("udevmon tags set "), ["plain", "split", "storage"])
        keys = self.words("udevmon tags set storage --match ")
        self.assertIn("fs_type=", keys)
        self.assertNotIn("mountpoint=", keys)               # a rule cannot use it
        self.assertEqual(set(self.words("udevmon tags set storage --")),
                         {"--match", "--no-rule", "--description", "--no-reload", "--help"})

    def test_after_the_dashes_the_shell_completes_the_command(self):
        self.assertEqual(completion.complete(cli.build_parser(),
                                             ["udevmon", "actions", "set", "solo", "--", ""], 5),
                         (completion.FILES, []))


if __name__ == "__main__":
    unittest.main()
