# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Wildcards (`*` and `?`) in tag rules."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli
from udevmon import config as C
from udevmon.engine import Engine
from udevmon.events import MOUNTED, Endpoint, Event
from udevmon.matching import STRONG, WEAK, matches, resolve, strength


def cfg(data):
    return C.parse(data)


class MatchTests(unittest.TestCase):
    def test_star_and_question_mark(self):
        rule = {"fs_label": "BACKUP*"}
        for label, expected in (("BACKUP", True), ("BACKUP-2024", True), ("backup_x", True),
                                ("MYBACKUP", False), ("", False)):
            self.assertEqual(matches(rule, {"fs_label": label}, wild=True), expected, label)
        self.assertTrue(matches({"fs_label": "DISK?"}, {"fs_label": "DISK1"}, wild=True))
        self.assertFalse(matches({"fs_label": "DISK?"}, {"fs_label": "DISK"}, wild=True))
        self.assertFalse(matches({"fs_label": "DISK?"}, {"fs_label": "DISK12"}, wild=True))
        self.assertTrue(matches({"fs_label": "*"}, {"fs_label": "anything"}, wild=True))

    def test_the_whole_value_must_match_and_only_two_characters_are_special(self):
        self.assertFalse(matches({"model": "Flash"}, {"model": "Flash Drive"}, wild=True))
        self.assertTrue(matches({"model": "Flash*"}, {"model": "Flash Drive"}, wild=True))
        self.assertTrue(matches({"model": "*Drive"}, {"model": "Flash Drive"}, wild=True))
        self.assertTrue(matches({"model": "*sh*ive"}, {"model": "Flash Drive"}, wild=True))
        for special in ("a.c", "a[bc]", "a+", "(a)", "a|b", "^a", "a$"):
            self.assertTrue(matches({"x": special}, {"x": special}, wild=True), special)
        self.assertFalse(matches({"x": "a.c"}, {"x": "abc"}, wild=True))     # a dot is a dot
        self.assertFalse(matches({"x": "a[bc]"}, {"x": "ab"}, wild=True))    # not a class

    def test_lists_mix_patterns_and_plain_values(self):
        rule = {"fs_label": ["BACKUP*", "Photos"]}
        self.assertTrue(matches(rule, {"fs_label": "backup7"}, wild=True))
        self.assertTrue(matches(rule, {"fs_label": "photos"}, wild=True))
        self.assertFalse(matches(rule, {"fs_label": "music"}, wild=True))

    def test_every_key_must_match_and_a_missing_property_never_does(self):
        rule = {"fs_label": "B*", "fs_type": "ext?"}
        self.assertTrue(matches(rule, {"fs_label": "Bk", "fs_type": "ext4"}, wild=True))
        self.assertFalse(matches(rule, {"fs_label": "Bk", "fs_type": "vfat"}, wild=True))
        self.assertFalse(matches(rule, {"fs_type": "ext4"}, wild=True))

    def test_without_wild_the_characters_are_literal(self):
        self.assertFalse(matches({"fs_label": "BACKUP*"}, {"fs_label": "BACKUP1"}))
        self.assertTrue(matches({"fs_label": "BACKUP*"}, {"fs_label": "backup*"}))

    def test_a_pattern_is_never_a_strong_rule(self):
        self.assertEqual(strength({"fs_uuid": "3F2A-91BC"}, wild=True), STRONG)
        self.assertEqual(strength({"fs_uuid": "3F2A*"}, wild=True), WEAK)
        self.assertEqual(strength({"serial": "AB?D"}, wild=True), WEAK)
        vid = {"vendor_id": "1", "product_id": "2", "port_path": "3-*"}
        self.assertEqual(strength(vid, wild=True), WEAK)
        self.assertEqual(strength(vid), "medium")                # in a device it is literal


DATA = {
    "devices": {"mine": {"match": {"fs_uuid": "U1"}}},
    "tags": {"backups": {"match": {"fs_label": "BACKUP*"}}},
    "actions": [
        {"name": "note", "tag": "backups", "on": ["mounted"], "run": ["true"],
         "allow_unnamed": True},
        {"name": "strict", "tag": "backups", "on": ["mounted"], "run": ["true"],
         "allow_unnamed": False},
    ],
}


class EngineTests(unittest.TestCase):
    def endpoint(self, **props):
        return Endpoint("udisks", "k", props)

    def test_a_rule_with_a_pattern_selects_things(self):
        c = cfg(DATA)
        res = resolve(c, {"fs_label": "backup-2024", "fs_uuid": "other"})
        self.assertEqual(res.tags, ["backups"])
        self.assertEqual(resolve(c, {"fs_label": "photos"}).tags, [])

    def test_unnamed_things_get_only_allow_unnamed_actions(self):
        eng = Engine(cfg(DATA), runner=mock.Mock())
        plan = eng.plan(Event(MOUNTED, self.endpoint(fs_label="BACKUP1", fs_uuid="x")))
        self.assertEqual([j.action.name for j in plan.jobs], ["note"])
        self.assertEqual([s.action.name for s in plan.skipped], ["strict"])

    def test_a_named_device_matching_the_pattern_gets_every_action(self):
        eng = Engine(cfg(DATA), runner=mock.Mock())
        plan = eng.plan(Event(MOUNTED, self.endpoint(fs_label="BACKUP1", fs_uuid="U1")))
        self.assertEqual([j.action.name for j in plan.jobs], ["note", "strict"])


class ConfigTests(unittest.TestCase):
    def test_a_wildcard_in_a_device_match_is_warned_about(self):
        c = cfg({"devices": {"odd": {"match": {"fs_label": "A*"}}}})
        self.assertTrue(any("not wildcards in a device match" in w for w in c.warnings))

    def test_the_note_about_a_rule_that_is_not_strong_covers_patterns(self):
        c = cfg(DATA)
        self.assertTrue(any("not strong" in n and "backups" in n for n in c.notes))


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.p.write_text("")
        self.p.chmod(0o600)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.p), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_tags_add_and_set_take_patterns_and_report_them_as_weak(self):
        rc, out, _ = self.run_cli("tags", "add", "backups", "--match", "fs_label=BACKUP*")
        self.assertEqual(rc, 0)
        self.assertIn("weak", out)
        self.assertEqual(C.load(self.p).tags["backups"].match, {"fs_label": ("BACKUP*",)})
        rc, out, _ = self.run_cli("tags", "set", "backups", "--match", "fs_label=Z??")
        self.assertEqual(rc, 0)
        self.assertIn("weak", out)
        rc, out, _ = self.run_cli("tags", "show", "backups")
        self.assertIn("weak", out)


if __name__ == "__main__":
    unittest.main()
