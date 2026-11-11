# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Tag-level default variables, which a device's own override."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli
from udevmon import config as C
from udevmon import configedit as E
from udevmon.engine import Engine
from udevmon.events import MOUNTED, Endpoint, Event

DATA = {
    "tags": {"backup": {"vars": {"dest": "/srv/backup", "keep": "7"}},
             "photos": {"vars": {"dest": "/srv/photos"}}},
    "devices": {
        "a": {"match": {"fs_uuid": "A"}, "tags": ["backup"]},
        "b": {"match": {"fs_uuid": "B"}, "tags": ["backup"], "vars": {"keep": "30"}},
        "c": {"match": {"fs_uuid": "C"}, "tags": ["photos", "backup"]},
    },
    "actions": [
        {"name": "by-tag", "tag": "backup", "on": ["mounted"],
         "run": ["echo", "{var_dest}", "{var_keep}"]},
        {"name": "by-device", "device": "c", "on": ["mounted"], "run": ["echo", "{var_dest}"]},
    ],
}


class RuleTests(unittest.TestCase):
    def setUp(self):
        self.cfg = C.parse(DATA)

    def test_a_tag_action_gets_its_tags_defaults_and_the_device_overrides(self):
        self.assertEqual(C.variables_for(self.cfg, "a", "backup"),
                         {"dest": "/srv/backup", "keep": "7"})
        self.assertEqual(C.variables_for(self.cfg, "b", "backup"),
                         {"dest": "/srv/backup", "keep": "30"})
        self.assertEqual(C.variables_for(self.cfg, "c", "backup")["dest"], "/srv/backup")

    def test_a_device_action_takes_the_first_listed_tag(self):
        self.assertEqual(C.variables_for(self.cfg, "c", None),
                         {"dest": "/srv/photos", "keep": "7"})

    def test_an_unnamed_thing_gets_the_tags_defaults(self):
        self.assertEqual(C.variables_for(self.cfg, None, "photos"), {"dest": "/srv/photos"})

    def test_the_engine_uses_them(self):
        engine = Engine(self.cfg, runner=None, paused=lambda: False)
        event = Event(MOUNTED, Endpoint("udisks", "k", {"fs_uuid": "C", "mountpoint": "/m"}))
        jobs = {j.action.name: j.vars for j in engine.plan(event).jobs}
        self.assertEqual(jobs["by-device"]["dest"], "/srv/photos")
        self.assertEqual(jobs["by-tag"]["dest"], "/srv/backup")

    def test_check_notes_two_tags_that_disagree(self):
        notes = [n for n in self.cfg.notes if "both set variable" in n]
        self.assertEqual(len(notes), 1)
        self.assertIn("device 'c': tags 'photos' and 'backup' both set variable 'dest'", notes[0])

    def test_a_variable_only_a_tag_defines_is_fine(self):
        data = {"tags": {"t": {"vars": {"x": "1"}}},
                "devices": {"d": {"match": {"fs_uuid": "D"}, "tags": ["t"]}},
                "actions": [{"name": "a", "tag": "t", "on": ["mounted"],
                             "run": ["echo", "{var_x}"]}]}
        cfg = C.parse(data)
        self.assertFalse([w for w in cfg.warnings if "var" in w])

    def test_bad_tag_vars(self):
        with self.assertRaises(C.ConfigError) as cm:
            C.parse({"tags": {"t": {"vars": {"Bad": "1"}}}})
        self.assertIn("variable name(s) ['Bad']", str(cm.exception))


class EditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.p.write_text('[tags.backup]\nmatch = { fs_type = "ext4" }\n\n'
                          '[devices.d]\nmatch = { fs_uuid = "D" }\ntags = ["backup"]\n\n'
                          '[[actions]]\nname = "a"\ntag = "backup"\non = ["mounted"]\n'
                          'run = ["echo"]\n')
        self.p.chmod(0o600)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.p), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_set_and_unset(self):
        E.set_tag_fields(self.p, "backup", set_vars={"dest": "/srv", "keep": "7"})
        self.assertEqual(dict(C.load(self.p).tags["backup"].vars), {"dest": "/srv", "keep": "7"})
        E.set_tag_fields(self.p, "backup", unset_vars=["keep"])
        self.assertEqual(dict(C.load(self.p).tags["backup"].vars), {"dest": "/srv"})
        self.assertIn('match = { fs_type = "ext4" }', self.p.read_text())    # rule untouched

    def test_cli_and_show(self):
        rc, _, err = self.run_cli("tags", "set", "backup", "--var", "dest=/srv")
        self.assertEqual(rc, 0, err)
        rc, out, _ = self.run_cli("tags", "show", "backup")
        self.assertIn("var dest = '/srv'", out)

    def test_a_variable_an_action_uses_stays(self):
        E.set_tag_fields(self.p, "backup", set_vars={"dest": "/srv"})
        text = self.p.read_text().replace('run = ["echo"]', 'run = ["echo", "{var_dest}"]')
        self.p.write_text(text)
        with self.assertRaises(C.ConfigError) as cm:
            E.set_tag_fields(self.p, "backup", unset_vars=["dest"])
        self.assertIn("no other device or tag defines them", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
