"""`tag_mode = "all"`: an action on several tags that needs every one of them."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli, explain
from udevmon import config as C
from udevmon import configedit as E
from udevmon.engine import Engine
from udevmon.events import MOUNTED, Endpoint, Event

DATA = {
    "devices": {
        "both": {"match": {"fs_uuid": "U-BOTH"}, "tags": ["backup", "offsite"]},
        "one": {"match": {"fs_uuid": "U-ONE"}, "tags": ["backup"]},
    },
    "tags": {"ext": {"match": {"fs_type": "ext4"}}},
    "actions": [
        {"name": "any", "tag": ["backup", "offsite"], "on": ["mounted"], "run": ["true"]},
        {"name": "all", "tag": ["backup", "offsite"], "tag_mode": "all", "on": ["mounted"],
         "run": ["echo", "{tag}"]},
        {"name": "mixed", "tag": ["backup", "ext"], "tag_mode": "all", "on": ["mounted"],
         "allow_unnamed": True, "run": ["true"]},
    ],
}


def plan(data, **props):
    engine = Engine(C.parse(data), runner=mock.Mock())
    return engine.plan(Event(MOUNTED, Endpoint("udisks", "k", props)))


class EngineTests(unittest.TestCase):
    def names(self, **props):
        return [j.action.name for j in plan(DATA, **props).jobs]

    def test_any_needs_one_tag_and_all_needs_every_one(self):
        self.assertEqual(self.names(fs_uuid="U-BOTH", fs_type="vfat"), ["any", "all"])
        self.assertEqual(self.names(fs_uuid="U-ONE", fs_type="vfat"), ["any"])

    def test_a_rule_tag_counts_like_an_explicit_one(self):
        self.assertEqual(self.names(fs_uuid="U-ONE", fs_type="ext4"), ["any", "mixed"])

    def test_the_gate_for_unnamed_things_still_applies(self):
        # an unnamed ext4 thing has the rule tag only, never `backup`: no action runs
        self.assertEqual(self.names(fs_uuid="stranger", fs_type="ext4"), [])

    def test_the_tag_placeholder_is_the_first_listed_tag(self):
        job = next(j for j in plan(DATA, fs_uuid="U-BOTH").jobs if j.action.name == "all")
        self.assertEqual(job.tag, "backup")

    def test_explain_names_all_the_tags(self):
        c = C.parse(DATA)
        text = "\n".join(explain.explain_endpoint(
            c, Endpoint("udisks", "k", {"fs_uuid": "U-BOTH"}), [MOUNTED]))
        self.assertIn("'all' (tags backup + offsite)", text)
        self.assertIn("'any' (tag backup)", text)


class ConfigTests(unittest.TestCase):
    def action(self, **extra):
        data = {"devices": {"d": {"match": {"fs_uuid": "U"}, "tags": ["a", "b"]}},
                "actions": [{"name": "x", "on": ["mounted"], "run": ["true"], **extra}]}
        return data

    def test_the_default_is_any(self):
        c = C.parse(self.action(tag=["a", "b"]))
        self.assertEqual(c.actions[0].tag_mode, "any")

    def test_bad_values_and_misuse_are_refused(self):
        for extra, text in (({"tag": ["a"], "tag_mode": "some"}, "'tag_mode' must be"),
                            ({"device": "d", "tag_mode": "all"}, "only applies to actions that "
                                                                 "use 'tag'")):
            with self.subTest(extra):
                with self.assertRaises(C.ConfigError) as cm:
                    C.parse(self.action(**extra))
                self.assertIn(text, str(cm.exception))

    def test_all_with_one_tag_gets_a_note(self):
        c = C.parse(self.action(tag="a", tag_mode="all"))
        self.assertTrue(any("same as the default" in n for n in c.notes))


class EditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.p.write_text(
            '[devices.d]\nmatch = { fs_uuid = "U" }\ntags = ["a", "b"]\n\n'
            '[[actions]]\nname = "x"\ntag = ["a", "b"]   # both\non = ["mounted"]\n'
            'run = ["true"]\n\n[[actions]]\nname = "solo"\ndevice = "d"\non = ["mounted"]\n'
            'run = ["true"]\n')
        self.p.chmod(0o600)

    def mode(self):
        return next(a for a in C.load(self.p).actions if a.name == "x").tag_mode

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.p), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_set_all_then_any(self):
        E.set_action_options(self.p, "x", tag_mode="all")
        self.assertEqual(self.mode(), "all")
        self.assertIn("# both", self.p.read_text())
        E.set_action_options(self.p, "x", tag_mode="any")
        self.assertEqual(self.mode(), "any")
        self.assertNotIn("tag_mode", self.p.read_text())        # back to not stated

    def test_a_device_action_cannot_take_it(self):
        before = self.p.read_bytes()
        with self.assertRaises(C.ConfigError):
            E.set_action_options(self.p, "solo", tag_mode="all")
        self.assertEqual(self.p.read_bytes(), before)

    def test_add_writes_the_key(self):
        rc, out, _ = self.run_cli("actions", "add", "--tag", "a", "--tag", "b", "--all-tags",
                                  "--name", "y", "mounted", "--", "true")
        self.assertEqual(rc, 0)
        y = next(a for a in C.load(self.p).actions if a.name == "y")
        self.assertEqual((y.tags, y.tag_mode), (("a", "b"), "all"))

    def test_add_refuses_all_tags_without_tags(self):
        rc, _, err = self.run_cli("actions", "add", "--device", "d", "--all-tags", "mounted",
                                  "--", "true")
        self.assertEqual(rc, 1)
        self.assertIn("--all-tags only applies with --tag", err)

    def test_cli_set_and_list(self):
        rc, out, _ = self.run_cli("actions", "set", "x", "--all-tags")
        self.assertEqual(rc, 0)
        rc, out, _ = self.run_cli("actions", "list")
        self.assertIn("tag:a+b", out)
        self.assertIn("[all tags]", out)
        rc, _, _ = self.run_cli("actions", "set", "x", "--any-tags")
        self.assertEqual(self.mode(), "any")
        rc, _, _ = self.run_cli("actions", "set", "x", "--all-tags", "--any-tags")
        self.assertEqual(rc, 2)                                 # mutually exclusive


if __name__ == "__main__":
    unittest.main()
