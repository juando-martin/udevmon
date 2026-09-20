"""devices set, and renaming devices, tags and actions without disturbing the file."""

import contextlib
import difflib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli
from udevmon import config as C
from udevmon import configedit as E


def write(text):
    p = Path(tempfile.mkdtemp()) / "c.toml"
    p.write_text(text)
    p.chmod(0o600)
    return p


HAND = '''# top
[devices.first]
match = { serial = "S1" }

# about stick
[devices.stick]   # header comment
match = { serial = "S9x7a" }   # by serial
tags = ["a", "old-tag"]   # inline comment
description = "my stick"

[devices.card]                    # sub-table style
[devices.card.match]
vendor_id = "2ce3"

[devices.last]
match = { serial = "S3" }

[tags.old-tag]   # tag header comment
match = { fs_type = "ext4" }

[[actions]]   # first action
name = "one"
device = "stick"   # single, with comment
on = ["connected"]
run = ["true"]

[[actions]]
device = [
  "first",
  "stick",   # in a list
  "last",
]
on = ["connected"]
run = ["echo", "stick"]

[[actions]]
name = "three"
tag = "old-tag"
on = ["mounted"]
run = ["true"]

[[actions]]
device = "stick"
on = ["connected"]
run = ["true"]
'''


def changed_lines(before: str, after: str) -> list[str]:
    diff = difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm="", n=0)
    return [l for l in diff if l[:1] in "+-" and l[:3] not in ("+++", "---")]


class RenameDeviceTests(unittest.TestCase):
    def rename(self, old="stick", new="thing", text=HAND):
        p = write(text)
        report = E.rename_device(p, old, new)
        return p, report

    def test_only_the_names_change(self):
        p, _ = self.rename()
        self.assertEqual(changed_lines(HAND, p.read_text()), [
            "-[devices.stick]   # header comment", "+[devices.thing]   # header comment",
            '-device = "stick"   # single, with comment', '+device = "thing"   # single, with comment',
            '-  "stick",   # in a list', '+  "thing",   # in a list',
            '-device = "stick"', '+device = "thing"'])

    def test_the_result_is_the_same_config_with_the_new_name(self):
        p, _ = self.rename()
        before, after = C.load(write(HAND)), C.load(p)
        self.assertEqual(list(after.devices), ["first", "thing", "card", "last"])   # same position
        self.assertEqual(after.devices["thing"].match, before.devices["stick"].match)
        self.assertEqual(after.devices["thing"].tags, before.devices["stick"].tags)
        self.assertEqual([a.devices for a in after.actions],
                         [("thing",), ("first", "thing", "last"), (), ("thing",)])

    def test_the_report(self):
        p, report = self.rename()
        self.assertEqual(report.updated, ["one", "first-2", "stick-4"])
        self.assertEqual(report.generated, [("stick-4", "thing-4")])       # made-up name follows the target
        lines = [text for _, text in report.mentions]
        self.assertIn("# about stick", lines)
        self.assertIn('description = "my stick"', lines)
        self.assertIn('run = ["echo", "stick"]', lines)
        self.assertEqual(len(report.mentions), 3)

    def test_a_device_with_sub_tables(self):
        p, _ = self.rename("card", "reader")
        text = p.read_text()
        self.assertIn("[devices.reader]                    # sub-table style", text)
        self.assertIn("[devices.reader.match]", text)
        self.assertNotIn("devices.card", text)
        self.assertEqual(C.load(p).devices["reader"].match["vendor_id"], "2ce3")

    def test_every_way_of_writing_a_header(self):
        for header in ('[devices.stick]', '  [devices.stick]', '[ devices . stick ]',
                       '[devices."stick"]', "[devices.'stick']", '[devices.stick]   # c'):
            with self.subTest(header):
                text = HAND.replace("[devices.stick]   # header comment", header)
                p, _ = self.rename(text=text)
                self.assertIn("thing", p.read_text().split("[devices.card]")[0])
                self.assertEqual(list(C.load(p).devices), ["first", "thing", "card", "last"])

    def test_names_with_dots_are_quoted(self):
        p, _ = self.rename(new="usb.1")
        self.assertIn('[devices."usb.1"]', p.read_text())
        self.assertEqual(list(C.load(p).devices), ["first", "usb.1", "card", "last"])
        E.rename_device(p, "usb.1", "back.again")               # from a quoted, dotted name
        self.assertIn('[devices."back.again"]', p.read_text())
        E.rename_device(p, "back.again", "plain")
        self.assertIn("[devices.plain]", p.read_text())
        self.assertEqual(list(C.load(p).devices), ["first", "plain", "card", "last"])

    def test_refusals_leave_the_file_alone(self):
        p = write(HAND)
        before = p.read_bytes()
        for old, new in (("ghost", "x"), ("stick", "first"), ("stick", "stick"), ("stick", "bad name"),
                         ("stick", ""), ("stick", "a/b")):
            with self.subTest((old, new)):
                with self.assertRaises(C.ConfigError):
                    E.rename_device(p, old, new)
                self.assertEqual(p.read_bytes(), before)

    def test_a_dotted_key_layout_is_refused_with_a_reason(self):
        text = '[devices]\nx.match = { serial = "S1" }\ny.match = { serial = "S2" }\n'
        p = write(text)
        with self.assertRaises(C.ConfigError) as cm:
            E.rename_device(p, "x", "z")
        self.assertIn("not written as a [devices.x] table", str(cm.exception))
        self.assertEqual(p.read_text(), text)

    def test_a_header_lookalike_inside_a_string_is_caught_by_the_check(self):
        text = ('[devices.stick]\nmatch = { serial = "S1" }\n'
                'description = """about it\n[devices.stick]\nend\n"""\n')
        p = write(text)
        before = p.read_bytes()
        with self.assertRaises(C.ConfigError) as cm:
            E.rename_device(p, "stick", "thing")
        self.assertIn("would have changed something else", str(cm.exception))
        self.assertEqual(p.read_bytes(), before)               # rolled back, nothing written

    def test_a_backup_is_kept(self):
        p, _ = self.rename()
        self.assertEqual(p.with_name("c.toml.bak").read_text(), HAND)

    def test_other_devices_with_similar_names_are_untouched(self):
        text = ('[devices.stick]\nmatch = { serial = "S1" }\n\n[devices.stick2]\nmatch = { serial = "S2" }\n\n'
                '[devices.my-stick]\nmatch = { serial = "S3" }\n\n[[actions]]\nname = "a"\n'
                'device = ["stick", "stick2", "my-stick"]\non = ["connected"]\nrun = ["true"]\n')
        p, _ = self.rename(text=text)
        c = C.load(p)
        self.assertEqual(list(c.devices), ["thing", "stick2", "my-stick"])
        self.assertEqual(c.actions[0].devices, ("thing", "stick2", "my-stick"))


class RenameTagTests(unittest.TestCase):
    TAGS = '''[devices.a]
match = { serial = "S1" }
tags = ["old", "keep"]

[devices.b]
match = { serial = "S2" }
tags = "old"

[devices.c]
match = { serial = "S3" }

[tags.old]   # a declared tag
match = { fs_type = "ext4" }

[[actions]]
name = "x"
tag = "old"
on = ["mounted"]
allow_unnamed = false
run = ["true"]

[[actions]]
name = "y"
tag = ["keep", "old"]
on = ["mounted"]
allow_unnamed = false
run = ["true"]
'''

    def test_a_declared_tag(self):
        p = write(self.TAGS)
        report = E.rename_tag(p, "old", "new")
        c = C.load(p)
        self.assertEqual(list(c.tags), ["new"])
        self.assertEqual((c.devices["a"].tags, c.devices["b"].tags), (("new", "keep"), ("new",)))
        self.assertEqual([a.tags for a in c.actions], [("new",), ("keep", "new")])
        self.assertIn("[tags.new]   # a declared tag", p.read_text())
        self.assertEqual(report.updated, ["device a", "device b", "action x", "action y"])

    def test_a_tag_that_was_never_declared(self):
        p = write(self.TAGS)
        E.rename_tag(p, "keep", "kept")
        c = C.load(p)
        self.assertEqual(c.devices["a"].tags, ("old", "kept"))
        self.assertEqual(c.actions[1].tags, ("kept", "old"))
        self.assertEqual(list(c.tags), ["old"])

    def test_refusals(self):
        p = write(self.TAGS)
        before = p.read_bytes()
        for old, new in (("ghost", "x"), ("old", "keep"), ("old", "old")):
            with self.assertRaises(C.ConfigError):
                E.rename_tag(p, old, new)
        self.assertEqual(p.read_bytes(), before)

    def test_only_the_tag_lines_change(self):
        p = write(self.TAGS)
        E.rename_tag(p, "old", "new")
        self.assertEqual(len(changed_lines(self.TAGS, p.read_text())), 10)     # 5 lines, before and after


class RenameActionTests(unittest.TestCase):
    def test_an_explicit_name(self):
        p = write(HAND)
        report = E.rename_action(p, "one", "first-action")
        self.assertEqual([a.name for a in C.load(p).actions][0], "first-action")
        self.assertEqual(changed_lines(HAND, p.read_text()),
                         ['-name = "one"', '+name = "first-action"'])
        self.assertEqual((report.updated, report.generated), ([], []))

    def test_a_made_up_name_cannot_be_renamed_and_says_why(self):
        p = write(HAND)
        before = p.read_bytes()
        with self.assertRaises(C.ConfigError) as cm:
            E.rename_action(p, "first-2", "x")
        self.assertIn("has no explicit name", str(cm.exception))
        self.assertIn('Add a line `name = "..."`', str(cm.exception))
        self.assertEqual(p.read_bytes(), before)

    def test_refusals(self):
        p = write(HAND)
        for old, new in (("ghost", "x"), ("one", "three"), ("one", "one")):
            with self.assertRaises(C.ConfigError):
                E.rename_action(p, old, new)


class SetDeviceTests(unittest.TestCase):
    def test_replace_the_match_and_keep_the_comment(self):
        p = write(HAND)
        E.set_device_fields(p, "stick", match={"vendor_id": "1050", "serial": "Q1"})
        self.assertEqual(C.load(p).devices["stick"].match, {"vendor_id": "1050", "serial": "Q1"})
        self.assertIn('match = { vendor_id = "1050", serial = "Q1" }   # by serial', p.read_text())
        self.assertEqual(len(changed_lines(HAND, p.read_text())), 2)

    def test_a_sub_table_match(self):
        p = write(HAND)
        E.set_device_fields(p, "card", match={"serial": "Z9"})
        self.assertEqual(C.load(p).devices["card"].match, {"serial": "Z9"})
        self.assertIn("[devices.card.match]", p.read_text())

    def test_description(self):
        p = write(HAND)
        E.set_device_fields(p, "stick", description='He said "hi"')
        self.assertEqual(C.load(p).devices["stick"].description, 'He said "hi"')
        E.set_device_fields(p, "stick", description="")
        self.assertEqual(C.load(p).devices["stick"].description, "")
        self.assertNotIn("my stick", p.read_text())
        E.set_device_fields(p, "first", description="added")             # a device that had none
        self.assertEqual(C.load(p).devices["first"].description, "added")

    def test_both_at_once_and_special_characters(self):
        p = write(HAND)
        E.set_device_fields(p, "last", match={"vendor": 'A "quoted" one', "serial": "S\\1"}, description="d")
        d = C.load(p).devices["last"]
        self.assertEqual((d.match, d.description), ({"vendor": 'A "quoted" one', "serial": "S\\1"}, "d"))

    def test_errors(self):
        p = write(HAND)
        before = p.read_bytes()
        for kwargs, name in (({}, "stick"), ({"match": {}}, "stick"), ({"match": {"a": "b"}}, "ghost")):
            with self.assertRaises(C.ConfigError):
                E.set_device_fields(p, name, **kwargs)
        self.assertEqual(p.read_bytes(), before)


class DottedNamesTests(unittest.TestCase):
    """A name with a dot used to be written as a nested header and rejected."""

    def test_adding_and_editing_dotted_names(self):
        p = write("")
        E.add_device(p, "usb.1", {"serial": "S1x9"}, "d", ["net.tag"])
        E.add_tag(p, "my.tag", {"fs_type": "ext4"})
        c = C.load(p)
        self.assertEqual((list(c.devices), list(c.tags)), (["usb.1"], ["my.tag"]))
        self.assertIn('[devices."usb.1"]', p.read_text())
        E.set_device_tags(p, "usb.1", add=["another.one"], remove=["net.tag"])
        self.assertEqual(C.load(p).devices["usb.1"].tags, ("another.one",))
        E.set_device_status(p, ["usb.1"], "ignored")
        self.assertEqual(C.load(p).devices["usb.1"].status, "ignored")
        E.set_device_fields(p, "usb.1", description="x")
        E.remove_tag(p, "my.tag")
        E.remove_device(p, "usb.1")
        self.assertEqual(C.load(p).devices, {})


class MessageTests(unittest.TestCase):
    def test_a_structure_problem_names_the_entry(self):
        p = write(HAND)
        with self.assertRaises(C.ConfigError) as cm:
            E._edited(p, lambda doc: doc["nope"], "device 'x'")
        text = str(cm.exception)
        self.assertIn("could not find device 'x'", text)
        self.assertIn("edit the file by hand", text)
        self.assertIn("does not exist", text)                    # what tomlkit said


class CliTests(unittest.TestCase):
    def setUp(self):
        self.p = write(HAND)

    def run_cli(self, *args, reload_result=False):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=reload_result) as reload, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.p), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue(), reload

    def test_devices_rename_report(self):
        rc, out, _, reload = self.run_cli("devices", "rename", "stick", "thing", reload_result=True)
        self.assertEqual(rc, 0)
        self.assertIn("Renamed device 'stick' to 'thing'.", out)
        self.assertIn("Updated: one, first-2, stick-4.", out)
        self.assertIn("stick-4 -> thing-4", out)
        self.assertIn("line 5: # about stick", out)
        self.assertIn("Reloaded the running daemon.", out)
        reload.assert_called_once_with(self.p)

    def test_the_other_renames(self):
        rc, out, _, _ = self.run_cli("tags", "rename", "old-tag", "new-tag")
        self.assertEqual(rc, 0)
        self.assertIn("Renamed tag 'old-tag' to 'new-tag'.", out)
        self.assertIn("Updated: device stick, action three.", out)
        self.assertIn("new-tag", self.run_cli("tags", "list")[1])
        rc, out, _, _ = self.run_cli("actions", "rename", "one", "uno")
        self.assertIn("Renamed action 'one' to 'uno'.", out)
        self.assertIn("uno", self.run_cli("actions", "list")[1])

    def test_errors_and_no_reload_on_failure(self):
        before = self.p.read_bytes()
        rc, _, err, reload = self.run_cli("devices", "rename", "ghost", "x")
        self.assertEqual((rc, "no such device 'ghost'" in err), (1, True))
        rc, _, err, _ = self.run_cli("devices", "rename", "stick", "first")
        self.assertIn("already exists", err)
        reload.assert_not_called()
        self.assertEqual(self.p.read_bytes(), before)

    def test_devices_set(self):
        rc, out, _, _ = self.run_cli("devices", "set", "stick", "--match", "vendor_id=1050",
                                     "--match", "serial=Q1", "--description", "new words")
        self.assertEqual(rc, 0)
        self.assertIn("Updated device 'stick' (strong).", out)
        d = C.load(self.p).devices["stick"]
        self.assertEqual((d.match, d.description), ({"vendor_id": "1050", "serial": "Q1"}, "new words"))
        rc, out, _, _ = self.run_cli("devices", "set", "stick", "--match", "vendor_id=1050")
        self.assertIn("(weak)", out)
        rc, _, err, _ = self.run_cli("devices", "set", "stick")
        self.assertEqual((rc, "nothing to change" in err), (1, True))
        rc, _, err, _ = self.run_cli("devices", "set", "stick", "--match", "a=1", "--match", "a=2")
        self.assertEqual((rc, "given twice" in err), (1, True))
        rc, _, err, _ = self.run_cli("devices", "set", "stick", "--match", "novalue")
        self.assertEqual(rc, 1)

    def test_completion_of_the_new_commands(self):
        from udevmon import completion
        parser = cli.build_parser()

        def words(*w):
            ws = ["udevmon", "--config", str(self.p), *w]
            return completion.complete(parser, ws, len(ws) - 1)
        self.assertEqual(words("devices", "rename", "s")[1], ["stick"])
        self.assertEqual(words("devices", "rename", "stick", ""), ("words", []))      # a new name
        self.assertEqual(words("tags", "rename", "o")[1], ["old-tag"])
        self.assertEqual(words("tags", "rename", "")[1], ["a", "old-tag"])     # every tag that exists
        self.assertEqual(words("actions", "rename", "o")[1], ["one"])
        self.assertEqual(words("devices", "set", "l")[1], ["last"])
        self.assertEqual(words("devices", "set", "stick", "--match", "ser"), ("nospace", ["serial="]))


if __name__ == "__main__":
    unittest.main()
