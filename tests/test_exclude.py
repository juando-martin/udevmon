"""A named device can opt out of a rule tag (`exclude_tags`)."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli, completion, explain
from udevmon import config as C
from udevmon import configedit as E
from udevmon.engine import Engine
from udevmon.events import MOUNTED, Endpoint, Event
from udevmon.matching import resolve

HAND = """\
# my config
[devices.special]
match = { fs_uuid = "U-SPECIAL" }
exclude_tags = ["storage"]   # my own disk: no generic drive handling

[devices.normal]
match = { fs_uuid = "U-NORMAL" }

[devices.both]
match = { fs_uuid = "U-BOTH" }
tags = ["backup"]
exclude_tags = ["storage", "photos"]

[tags.storage]
match = { fs_type = ["ext4", "exfat"] }

[tags.photos]
match = { fs_label = "PHOTOS*" }

[tags.backup]

[[actions]]
name = "generic"
tag = "storage"
on = ["mounted"]
allow_unnamed = true
run = ["true"]

[[actions]]
name = "named-only"
tag = "storage"
on = ["mounted"]
allow_unnamed = false
run = ["true"]

[[actions]]
name = "own"
device = "special"
on = ["mounted"]
run = ["true"]
"""


def load(text=HAND):
    return C.parse(C.tomllib.loads(text))


def names(cfg, **props):
    plan = Engine(cfg, runner=mock.Mock()).plan(Event(MOUNTED, Endpoint("udisks", "k", props)))
    return [j.action.name for j in plan.jobs]


class ResolveTests(unittest.TestCase):
    def test_an_opted_out_device_does_not_get_the_rule_tag(self):
        c = load()
        res = resolve(c, {"fs_uuid": "U-SPECIAL", "fs_type": "ext4"})
        self.assertEqual((res.tags, res.excluded), ([], ["storage"]))
        res = resolve(c, {"fs_uuid": "U-NORMAL", "fs_type": "ext4"})
        self.assertEqual((res.tags, res.excluded), (["storage"], []))

    def test_the_device_stays_named(self):
        res = resolve(load(), {"fs_uuid": "U-SPECIAL", "fs_type": "ext4"})
        self.assertTrue(res.is_named)

    def test_only_a_rule_that_actually_matched_is_reported(self):
        res = resolve(load(), {"fs_uuid": "U-SPECIAL", "fs_type": "vfat"})
        self.assertEqual((res.tags, res.excluded), ([], []))

    def test_an_explicit_tag_from_another_device_is_kept(self):
        data = C.tomllib.loads(HAND + '\n[devices.carrier]\nmatch = { fs_type = "ext4" }\n'
                                      'tags = ["storage"]\n')
        res = resolve(C.parse(data), {"fs_uuid": "U-SPECIAL", "fs_type": "ext4"})
        self.assertEqual((res.tags, res.excluded), (["storage"], []))

    def test_several_rules_and_explicit_tags_together(self):
        res = resolve(load(), {"fs_uuid": "U-BOTH", "fs_type": "ext4", "fs_label": "PHOTOS1"})
        self.assertEqual((res.tags, res.excluded), (["backup"], ["storage", "photos"]))


class EngineTests(unittest.TestCase):
    def test_actions_on_the_tag_do_not_run_but_the_devices_own_do(self):
        c = load()
        self.assertEqual(names(c, fs_uuid="U-SPECIAL", fs_type="ext4"), ["own"])
        self.assertEqual(names(c, fs_uuid="U-NORMAL", fs_type="ext4"), ["generic", "named-only"])
        self.assertEqual(names(c, fs_uuid="stranger", fs_type="ext4"), ["generic"])

    def test_explain_says_so(self):
        c = load()
        text = "\n".join(explain.explain_endpoint(
            c, Endpoint("udisks", "k", {"fs_uuid": "U-SPECIAL", "fs_type": "ext4"}), [MOUNTED]))
        self.assertIn("excluded from: storage", text)
        self.assertIn("'own'", text)
        self.assertNotIn("'generic'", text)


class ConfigTests(unittest.TestCase):
    def data(self, **device):
        return {"devices": {"d": {"match": {"fs_uuid": "U"}, **device}}}

    def test_parsed_as_a_name_or_a_list(self):
        self.assertEqual(C.parse(self.data(exclude_tags="a")).devices["d"].exclude_tags, ("a",))
        self.assertEqual(C.parse(self.data(exclude_tags=["a", "b", "a"])).devices["d"]
                         .exclude_tags, ("a", "b"))

    def test_bad_values_are_refused(self):
        for device, text in (({"exclude_tags": []}, "non-empty list"),
                             ({"exclude_tags": ["bad name"]}, "non-empty list"),
                             ({"tags": ["a"], "exclude_tags": ["a"]}, "both 'tags' and")):
            with self.subTest(device):
                with self.assertRaises(C.ConfigError) as cm:
                    C.parse(self.data(**device))
                self.assertIn(text, str(cm.exception))

    def test_excluding_a_tag_with_no_rule_gets_a_note(self):
        c = C.parse(self.data(exclude_tags=["ghost"]))
        self.assertTrue(any("nothing to opt out of" in n for n in c.notes))
        self.assertFalse(any("nothing to opt out of" in n for n in load().notes
                             if "'special'" in n))


class EditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.p.write_text(HAND)
        self.p.chmod(0o600)

    def excluded(self, name):
        return C.load(self.p).devices[name].exclude_tags

    def test_exclude_and_include(self):
        E.set_device_exclusions(self.p, "normal", add=["storage", "photos"])
        self.assertEqual(self.excluded("normal"), ("storage", "photos"))
        E.set_device_exclusions(self.p, "normal", remove=["storage"])
        self.assertEqual(self.excluded("normal"), ("photos",))
        E.set_device_exclusions(self.p, "normal", remove=["photos"])
        self.assertNotIn("exclude_tags", self.p.read_text().split("[devices.both]")[0]
                         .split("[devices.normal]")[1])

    def test_an_array_is_edited_in_place_keeping_comments(self):
        E.set_device_exclusions(self.p, "special", add=["photos"])
        text = self.p.read_text()
        self.assertIn("# my own disk: no generic drive handling", text)
        self.assertIn("# my config", text)
        self.assertEqual(self.excluded("special"), ("storage", "photos"))

    def test_refusals_write_nothing(self):
        before = self.p.read_bytes()
        cases = (("ghost", {"add": ["storage"]}, "no such device"),
                 ("normal", {"remove": ["storage"]}, "no exclusion(s) ['storage']"),
                 ("both", {"add": ["backup"]}, "carries ['backup'] as an explicit tag"),
                 ("normal", {"add": ["bad name"]}, "invalid tag name"))
        for name, kwargs, text in cases:
            with self.subTest(name, kwargs=kwargs):
                with self.assertRaises(C.ConfigError) as cm:
                    E.set_device_exclusions(self.p, name, **kwargs)
                self.assertIn(text, str(cm.exception))
                self.assertEqual(self.p.read_bytes(), before)

    def test_tagging_a_device_that_excludes_the_tag_is_refused(self):
        before = self.p.read_bytes()
        with self.assertRaises(C.ConfigError) as cm:
            E.set_device_tags(self.p, "special", add=["storage"])
        self.assertIn("devices include", str(cm.exception))
        self.assertEqual(self.p.read_bytes(), before)

    def test_the_old_tag_edits_still_work(self):
        E.set_device_tags(self.p, "normal", add=["x", "y"])
        E.set_device_tags(self.p, "normal", remove=["x"])
        self.assertEqual(C.load(self.p).devices["normal"].tags, ("y",))
        with self.assertRaises(C.ConfigError):
            E.set_device_tags(self.p, "normal", remove=["storage"])

    def test_an_edit_that_does_nothing_is_rolled_back(self):
        before = self.p.read_bytes()
        with mock.patch.object(E, "_edited", return_value=HAND):
            with self.assertRaises(C.ConfigError):
                E.set_device_exclusions(self.p, "normal", add=["storage"])
        self.assertEqual(self.p.read_bytes(), before)

    def test_renaming_a_tag_updates_the_exclusions(self):
        E.rename_tag(self.p, "storage", "drives")
        self.assertEqual(self.excluded("special"), ("drives",))
        self.assertEqual(self.excluded("both"), ("drives", "photos"))
        self.assertIn("# my own disk: no generic drive handling", self.p.read_text())
        self.assertEqual(self.excluded("normal"), ())

    def test_add_device_with_exclusions(self):
        E.add_device(self.p, "fresh", {"fs_uuid": "F"}, exclude_tags=["storage", "storage"])
        self.assertEqual(self.excluded("fresh"), ("storage",))


class CliTests(EditTests):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=True), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.p), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_commands(self):
        rc, out, _ = self.run_cli("devices", "exclude", "normal", "storage", "photos")
        self.assertEqual(rc, 0)
        self.assertIn("'normal' now opts out of the rule tag(s): storage, photos.", out)
        self.assertIn("Reloaded the running daemon.", out)
        rc, out, _ = self.run_cli("devices", "show", "normal")
        self.assertIn("opts out of rule tags: storage, photos", out)
        rc, out, _ = self.run_cli("devices", "include", "normal", "storage")
        self.assertIn("no longer opts out of: storage.", out)
        rc, _, err = self.run_cli("devices", "include", "normal", "storage")
        self.assertEqual(rc, 1)
        rc, out, _ = self.run_cli("devices", "add", "third", "--match", "fs_uuid=T",
                                  "--exclude-tag", "storage")
        self.assertEqual(rc, 0)
        self.assertEqual(self.excluded("third"), ("storage",))

    def test_check_and_present_style_status(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main(["--config", str(self.p), "check"]), 0)
        self.assertIn("OK", out.getvalue())


class CompletionTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.p.write_text(HAND)
        self.p.chmod(0o600)

    def words(self, line):
        parts = line.split(" ")
        parts[1:1] = ["--config", str(self.p)]
        return completion.complete(cli.build_parser(), parts, len(parts) - 1)[1]

    def test_exclude_offers_rule_tags_and_include_the_devices_own(self):
        self.assertEqual(self.words("udevmon devices exclude normal "),
                         ["photos", "storage"])                # declared with a rule; not backup
        self.assertEqual(self.words("udevmon devices include both "), ["storage", "photos"])
        self.assertEqual(self.words("udevmon devices include special "), ["storage"])
        self.assertEqual(self.words("udevmon devices add x --exclude-tag "),
                         ["photos", "storage"])


if __name__ == "__main__":
    unittest.main()
