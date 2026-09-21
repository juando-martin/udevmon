"""Per-device variables: `vars = { dest = "..." }` on a device, `{var_dest}` in a command."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import actions as A
from udevmon import cli, completion, explain
from udevmon import config as C
from udevmon import configedit as E
from udevmon.engine import Engine
from udevmon.events import MOUNTED, Endpoint, Event

HAND = """\
# my config
[devices.photos]
match = { fs_uuid = "U-PHOTOS" }
tags = ["backup"]
vars = { dest = "/srv/photos", keep = "30" }   # where it goes

[devices.music]
match = { fs_uuid = "U-MUSIC" }
tags = ["backup"]

[devices.music.vars]
dest = "/srv/music"

[devices.bare]
match = { fs_uuid = "U-BARE" }
tags = ["backup"]

[tags.everything]
match = { fs_type = "ext4" }

[[actions]]
name = "copy"
tag = "backup"
on = ["mounted"]
run = ["copy.sh", "{mountpoint}", "{var_dest}"]

[[actions]]
name = "note"
device = "photos"
on = ["mounted"]
run = ["echo", "{var_keep}", "{var_dest}"]
"""


def load(text=HAND):
    return C.parse(C.tomllib.loads(text))


def plan(cfg, **props):
    return Engine(cfg, runner=mock.Mock()).plan(Event(MOUNTED, Endpoint("udisks", "k", props)))


class ConfigTests(unittest.TestCase):
    def data(self, **device):
        return {"devices": {"d": {"match": {"fs_uuid": "U"}, **device}}}

    def test_variables_are_read(self):
        c = load()
        self.assertEqual(dict(c.devices["photos"].vars), {"dest": "/srv/photos", "keep": "30"})
        self.assertEqual(dict(c.devices["music"].vars), {"dest": "/srv/music"})   # a sub-table
        self.assertEqual(dict(c.devices["bare"].vars), {})

    def test_numbers_become_text(self):
        c = C.parse(self.data(vars={"keep": 30}))
        self.assertEqual(dict(c.devices["d"].vars), {"keep": "30"})

    def test_bad_variables_are_refused(self):
        for value, text in (("text", "must be a table"), ({"Bad": "x"}, "must be lowercase"),
                            ({"1x": "x"}, "must be lowercase"), ({"a-b": "x"}, "must be lowercase"),
                            ({"a": ["x"]}, "must be strings"), ({"a": True}, "must be strings")):
            with self.subTest(value):
                with self.assertRaises(C.ConfigError) as cm:
                    C.parse(self.data(vars=value))
                self.assertIn(text, str(cm.exception))

    def action(self, run, **extra):
        return {"devices": {"d": {"match": {"fs_uuid": "U"}, "vars": {"dest": "/x"}},
                            "e": {"match": {"fs_uuid": "V"}}},
                "tags": {"t": {"match": {"fs_type": "ext4"}}},
                "actions": [{"name": "a", "on": ["mounted"], "run": run, **extra}]}

    def test_a_variable_no_device_defines_is_an_error(self):
        with self.assertRaises(C.ConfigError) as cm:
            C.parse(self.action(["echo", "{var_typo}"], device="d"))
        self.assertIn("no device has a variable 'typo'", str(cm.exception))
        self.assertIn("devices set NAME --var typo=VALUE", str(cm.exception))

    def test_a_defined_variable_is_fine_for_every_event(self):
        for kind in ("connected", "mounted", "removed", "startup"):
            with self.subTest(kind):
                C.parse(self.action(["echo", "{var_dest}"], device="d", on=[kind]))

    def test_devices_that_lack_it_are_warned_about(self):
        c = C.parse(self.action(["echo", "{var_dest}"], device=["d", "e"]))
        self.assertTrue(any("device 'e' does not define" in w for w in c.warnings))
        self.assertFalse(any("device 'd' does not define" in w for w in c.warnings))
        c = load()
        self.assertTrue(any("'copy'" in w and "device 'bare'" in w for w in c.warnings))

    def test_unnamed_things_and_trust(self):
        c = C.parse(self.action(["echo", "{var_dest}", "{fs_label}"], tag="t",
                                allow_unnamed=True))
        warnings = " | ".join(c.warnings)
        self.assertIn("unnamed things do not have", warnings)
        self.assertIn("['fs_label']", warnings)              # the hardware value is still flagged
        self.assertNotIn("var_dest", warnings.split("hardware-supplied")[-1])

    def test_a_variable_alone_is_not_a_hardware_value(self):
        c = C.parse(self.action(["echo", "{var_dest}"], tag="t", allow_unnamed=True))
        self.assertFalse(any("hardware-supplied" in w for w in c.warnings))


class EngineTests(unittest.TestCase):
    def test_jobs_carry_the_devices_variables(self):
        c = load()
        jobs = plan(c, fs_uuid="U-PHOTOS").jobs
        self.assertEqual([(j.action.name, dict(j.vars)) for j in jobs],
                         [("copy", {"dest": "/srv/photos", "keep": "30"}),
                          ("note", {"dest": "/srv/photos", "keep": "30"})])
        self.assertEqual(dict(plan(c, fs_uuid="U-MUSIC").jobs[0].vars), {"dest": "/srv/music"})

    def test_unnamed_things_have_none(self):
        data = C.tomllib.loads(HAND.replace('run = ["copy.sh", "{mountpoint}", "{var_dest}"]',
                                            'run = ["copy.sh"]\nallow_unnamed = true')
                                   .replace('tag = "backup"\non = ["mounted"]',
                                            'tag = "everything"\non = ["mounted"]', 1))
        jobs = plan(C.parse(data), fs_uuid="stranger", fs_type="ext4").jobs
        self.assertEqual([(j.action.name, j.device, dict(j.vars)) for j in jobs],
                         [("copy", None, {})])

    def test_explain_fills_the_values_in(self):
        c = load()
        text = "\n".join(explain.explain_endpoint(
            c, Endpoint("udisks", "k", {"fs_uuid": "U-MUSIC", "mountpoint": "/m"}), [MOUNTED]))
        self.assertIn("copy.sh /m /srv/music", text)

    def test_explain_says_when_a_device_lacks_one(self):
        c = load()
        text = "\n".join(explain.explain_endpoint(
            c, Endpoint("udisks", "k", {"fs_uuid": "U-BARE", "mountpoint": "/m"}), [MOUNTED]))
        self.assertIn("{var_dest} is not available", text)


class RunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def action(self, run):
        return C.Action("t", ("d",), (), ("mounted",), tuple(run), 10)

    def test_placeholders_and_environment(self):
        out = self.dir / "out"
        rc = A.run_action(
            self.action(["sh", "-c", 'echo "$1|$UDEVMON_VAR_DEST|$UDEVMON_VAR_KEEP" > "$2"', "sh",
                         "{var_dest}", str(out)]),
            Event(MOUNTED, Endpoint("udisks", "k", {"mountpoint": "/m"})), "d",
            variables={"dest": "/srv/x y", "keep": "7"})
        self.assertEqual(rc, 0)
        self.assertEqual(out.read_text().strip(), "/srv/x y|/srv/x y|7")

    def test_a_missing_variable_skips_the_action(self):
        marker = self.dir / "ran"
        rc = A.run_action(self.action(["touch", str(marker), "{var_dest}"]),
                          Event(MOUNTED, Endpoint("udisks", "k", {})), "d", variables={})
        self.assertIsNone(rc)
        self.assertFalse(marker.exists())

    def test_the_value_is_one_argument_never_a_shell_line(self):
        out, marker = self.dir / "out", self.dir / "pwned"
        A.run_action(self.action(["sh", "-c", 'printf %s "$1" > "$2"', "sh", "{var_dest}",
                                  str(out)]),
                     Event(MOUNTED, Endpoint("udisks", "k", {})), "d",
                     variables={"dest": f"x; touch {marker}"})
        self.assertEqual(out.read_text(), f"x; touch {marker}")
        self.assertFalse(marker.exists())


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.p.write_text(HAND)
        self.p.chmod(0o600)

    def vars(self, name):
        return dict(C.load(self.p).devices[name].vars)


class EditTests(Base):
    def test_set_and_change_in_an_inline_table_keeping_the_comment(self):
        E.set_device_fields(self.p, "photos", set_vars={"dest": "/new", "extra": "1"})
        self.assertEqual(self.vars("photos"), {"dest": "/new", "keep": "30", "extra": "1"})
        self.assertIn("# where it goes", self.p.read_text())
        self.assertEqual(self.p.with_name("c.toml.bak").read_text(), HAND)

    def test_a_sub_table_is_edited_in_place(self):
        E.set_device_fields(self.p, "music", set_vars={"keep": "3"})
        self.assertEqual(self.vars("music"), {"dest": "/srv/music", "keep": "3"})
        self.assertIn("[devices.music.vars]", self.p.read_text())

    def test_the_first_variable_creates_the_table(self):
        E.set_device_fields(self.p, "bare", set_vars={"dest": "/srv/bare"})
        self.assertEqual(self.vars("bare"), {"dest": "/srv/bare"})

    def test_unset_and_the_last_one_removes_the_key(self):
        E.set_device_fields(self.p, "photos", set_vars={"extra": "1"})
        E.set_device_fields(self.p, "photos", unset_vars=["extra"])
        self.assertEqual(self.vars("photos"), {"dest": "/srv/photos", "keep": "30"})
        E.set_device_fields(self.p, "bare", set_vars={"other": "1"})
        E.set_device_fields(self.p, "bare", unset_vars=["other"])
        self.assertEqual(self.vars("bare"), {})
        self.assertNotIn("vars", self.p.read_text().split("[tags.everything]")[0]
                         .split("[devices.bare]")[1])

    def test_a_value_may_be_empty_and_is_quoted_safely(self):
        E.set_device_fields(self.p, "bare", set_vars={"empty": "", "odd": 'a "b" \\ c\n'})
        self.assertEqual(self.vars("bare"), {"empty": "", "odd": 'a "b" \\ c\n'})

    def test_refusals_write_nothing(self):
        before = self.p.read_bytes()
        cases = (("ghost", {"set_vars": {"a": "1"}}, "no such device"),
                 ("photos", {}, "nothing to change"),
                 ("photos", {"set_vars": {"Bad": "1"}}, "invalid variable name"),
                 ("photos", {"unset_vars": ["nope"]}, "no variable(s) ['nope']"),
                 ("photos", {"set_vars": {"a": "1"}, "unset_vars": ["a"]}, "both"))
        for name, kwargs, text in cases:
            with self.subTest(name, kwargs=kwargs):
                with self.assertRaises(C.ConfigError) as cm:
                    E.set_device_fields(self.p, name, **kwargs)
                self.assertIn(text, str(cm.exception))
                self.assertEqual(self.p.read_bytes(), before)

    def test_removing_the_only_definition_of_a_variable_an_action_uses_is_refused(self):
        before = self.p.read_bytes()
        with self.assertRaises(C.ConfigError) as cm:                     # only photos has `keep`
            E.set_device_fields(self.p, "photos", unset_vars=["keep"])
        self.assertIn("edit the action first", str(cm.exception))
        self.assertEqual(self.p.read_bytes(), before)
        E.set_device_fields(self.p, "photos", unset_vars=["dest"])       # music defines it too
        self.assertEqual(self.vars("photos"), {"keep": "30"})

    def test_an_edit_that_does_nothing_is_rolled_back(self):
        before = self.p.read_bytes()
        with mock.patch.object(E, "_edited", return_value=HAND):
            with self.assertRaises(C.ConfigError):
                E.set_device_fields(self.p, "bare", set_vars={"a": "1"})
        self.assertEqual(self.p.read_bytes(), before)

    def test_other_edits_still_work_together_with_variables(self):
        E.set_device_fields(self.p, "photos", description="Pictures", set_vars={"a": "1"},
                            match={"fs_uuid": "U-PHOTOS2"})
        d = C.load(self.p).devices["photos"]
        self.assertEqual((d.description, d.match["fs_uuid"], d.vars["a"]),
                         ("Pictures", "U-PHOTOS2", "1"))

    def test_add_device_with_variables(self):
        E.add_device(self.p, "fresh", {"fs_uuid": "F"}, variables={"dest": "/srv/f"})
        self.assertEqual(self.vars("fresh"), {"dest": "/srv/f"})
        with self.assertRaises(C.ConfigError):
            E.add_device(self.p, "bad", {"fs_uuid": "B"}, variables={"Nope": "x"})

    def test_renaming_a_device_or_tag_keeps_the_variables(self):
        E.rename_device(self.p, "photos", "pictures")
        self.assertEqual(self.vars("pictures"), {"dest": "/srv/photos", "keep": "30"})
        E.rename_tag(self.p, "backup", "archive")
        self.assertEqual(self.vars("music"), {"dest": "/srv/music"})


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

    def test_devices_add_and_set_and_show(self):
        rc, out, _ = self.run_cli("devices", "add", "fresh", "--match", "fs_uuid=F",
                                  "--var", "dest=/srv/f", "--var", "keep=5")
        self.assertEqual(rc, 0)
        self.assertEqual(self.vars("fresh"), {"dest": "/srv/f", "keep": "5"})
        rc, out, _ = self.run_cli("devices", "set", "fresh", "--var", "dest=/other",
                                  "--unset-var", "keep")
        self.assertEqual(rc, 0)
        self.assertIn("Reloaded the running daemon.", out)
        self.assertEqual(self.vars("fresh"), {"dest": "/other"})
        rc, out, _ = self.run_cli("devices", "show", "fresh")
        self.assertIn("variable dest = /other", out)

    def test_errors(self):
        rc, _, err = self.run_cli("devices", "set", "photos", "--var", "novalue")
        self.assertEqual(rc, 1)
        self.assertIn("expected NAME=VALUE", err)
        rc, _, err = self.run_cli("devices", "set", "photos", "--unset-var", "nope")
        self.assertEqual(rc, 1)
        rc, _, err = self.run_cli("devices", "set", "photos")
        self.assertEqual(rc, 1)
        self.assertIn("--var", err)

    def test_check_warns_about_the_bare_device(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = cli.main(["--config", str(self.p), "check"])
        self.assertEqual(rc, 0)
        self.assertIn("device 'bare' does not define", out.getvalue())


class CompletionTests(Base):
    def words(self, line):
        parts = line.split(" ")
        parts[1:1] = ["--config", str(self.p)]
        return completion.complete(cli.build_parser(), parts, len(parts) - 1)[1]

    def test_names_come_from_the_config(self):
        self.assertEqual(self.words("udevmon devices set photos --unset-var "), ["dest", "keep"])
        self.assertEqual(self.words("udevmon devices set music --unset-var "), ["dest"])
        self.assertEqual(self.words("udevmon devices set --unset-var "), ["dest", "keep"])
        self.assertEqual(self.words("udevmon devices set photos --var k"), ["keep="])
        self.assertEqual(self.words("udevmon devices add x --var d"), ["dest="])


if __name__ == "__main__":
    unittest.main()
