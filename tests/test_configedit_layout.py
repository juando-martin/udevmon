"""The line editor must cope with the layouts people write by hand."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from udevmon import cli
from udevmon import config as C
from udevmon import configedit as E

KEEP = "# keep me\n[devices.other]\nmatch = { serial = \"S9x7z\" }   # and me\n"


def write(text):
    d = tempfile.mkdtemp()
    p = Path(d) / "c.toml"
    p.write_text(text)
    p.chmod(0o600)
    return p


class HeaderSpellingTests(unittest.TestCase):
    """Every way of writing ``[devices.x]`` that TOML allows must be found."""

    HEADERS = {
        "plain": '[devices.x]',
        "trailing comment": '[devices.x]   # a note',
        "comment without space": '[devices.x]#note',
        "indented": '   [devices.x]',
        "spaces inside": '[ devices . x ]',
        "double-quoted key": '[devices."x"]',
        "single-quoted key": "[devices.'x']",
        "everything at once": '  [ devices . "x" ]  # note',
    }

    def config(self, header):
        return f'{KEEP}\n{header}\nmatch = {{ serial = "S9x7a" }}\n'

    def test_remove_device(self):
        for label, header in self.HEADERS.items():
            with self.subTest(label):
                p = write(self.config(header))
                E.remove_device(p, "x")
                self.assertEqual(list(C.load(p).devices), ["other"])
                self.assertEqual(p.read_text().rstrip("\n"), KEEP.rstrip("\n"))   # nothing else touched

    def test_tag_and_untag_a_device(self):
        for label, header in self.HEADERS.items():
            with self.subTest(label):
                p = write(self.config(header))
                E.set_device_tags(p, "x", add=["t1", "t2"])
                self.assertEqual(C.load(p).devices["x"].tags, ("t1", "t2"))
                E.set_device_tags(p, "x", remove=["t1"])
                self.assertEqual(C.load(p).devices["x"].tags, ("t2",))
                self.assertIn(header, p.read_text())                     # the header is left as written
                self.assertTrue(p.read_text().startswith(KEEP))

    def test_remove_tag_rule(self):
        for label, header in self.HEADERS.items():
            with self.subTest(label):
                p = write(f'{KEEP}\n{header.replace("devices", "tags")}\nmatch = {{ fs_type = "ext4" }}\n')
                E.remove_tag(p, "x")
                self.assertEqual(C.load(p).tags, {})
                self.assertEqual(p.read_text().rstrip("\n"), KEEP.rstrip("\n"))


class ActionSpellingTests(unittest.TestCase):
    BLOCKS = {
        "plain": '[[actions]]\nname = "one"',
        "comment on the header": '[[actions]]   # note\nname = "one"',
        "comment on the name": '[[actions]]\nname = "one"   # note',
        "both": '[[actions]]  # a\nname = "one"  # b',
        "indented": '  [[actions]]\n  name = "one"',
        "spaces inside": '[[ actions ]]\nname = "one"',
        "single-quoted name": "[[actions]]\nname = 'one'",
    }

    def config(self, block):
        return (f'{KEEP}\n{block}\ndevice = "other"\non = ["connected"]\nrun = ["true"]\n'
                '\n[[actions]]\nname = "two"\ndevice = "other"\non = ["connected"]\nrun = ["true"]\n')

    def test_remove_action(self):
        for label, block in self.BLOCKS.items():
            with self.subTest(label):
                p = write(self.config(block))
                E.remove_action(p, "one")
                self.assertEqual([a.name for a in C.load(p).actions], ["two"])
                self.assertIn('name = "two"', p.read_text())              # the other action is intact
                self.assertTrue(p.read_text().startswith(KEEP))

    def test_the_header_of_a_neighbour_is_not_mistaken_for_ours(self):
        p = write(self.config('[[actions]]   # note\nname = "one"'))
        E.remove_action(p, "two")
        self.assertEqual([a.name for a in C.load(p).actions], ["one"])
        self.assertIn("# note", p.read_text())


class BlockBoundaryTests(unittest.TestCase):
    """Where a block ends: at the next header, however that header is written."""

    def test_an_indented_next_header_ends_the_block(self):
        p = write('[devices.x]\nmatch = { serial = "S9x7a" }\n\n'
                  '  [devices.y]   # indented, with a comment\n  match = { serial = "S9x7b" }\n')
        E.remove_device(p, "x")
        self.assertEqual(list(C.load(p).devices), ["y"])
        self.assertIn("# indented, with a comment", p.read_text())

    def test_the_same_for_tags_and_actions(self):
        p = write('[tags.a]\nmatch = { fs_type = "ext4" }\n\n   [tags.b]\nmatch = { fs_type = "vfat" }\n')
        E.remove_tag(p, "a")
        self.assertEqual(list(C.load(p).tags), ["b"])
        block = 'device = "d"\non = ["connected"]\nrun = ["true"]\n'
        p = write(f'[devices.d]\nmatch = {{ serial = "S9x7a" }}\n\n[[actions]]\nname = "a"\n{block}\n'
                  f'  [[actions]]\n  name = "b"\n  {block.replace(chr(10), chr(10) + "  ").rstrip()}\n')
        E.remove_action(p, "a")
        self.assertEqual([a.name for a in C.load(p).actions], ["b"])

    def test_blank_lines_and_comments_inside_a_block_belong_to_it(self):
        text = ('[devices.before]\nmatch = { serial = "S9x7b" }\n\n'
                '[devices.x]   # the one to remove\nmatch = { serial = "S9x7a" }\n\n'
                '# a comment inside the block\n\ndescription = "after blank lines"\n\n'
                '[devices.after]\nmatch = { serial = "S9x7c" }\n')
        p = write(text)
        E.remove_device(p, "x")
        self.assertEqual(list(C.load(p).devices), ["before", "after"])
        self.assertNotIn("after blank lines", p.read_text())
        self.assertNotIn("inside the block", p.read_text())
        p = write(text)
        E.set_device_tags(p, "x", add=["t1"])                      # the new line goes inside the block
        self.assertEqual(C.load(p).devices["x"].tags, ("t1",))
        self.assertEqual(C.load(p).devices["x"].description, "after blank lines")
        self.assertEqual(C.load(p).devices["before"].tags, ())

    def test_a_block_at_the_end_of_the_file_and_one_with_no_blank_line_before_it(self):
        p = write('[devices.y]\nmatch = { serial = "S9x7b" }\n[devices.x]\nmatch = { serial = "S9x7a" }')
        E.remove_device(p, "x")
        self.assertEqual(list(C.load(p).devices), ["y"])


class NearMissTests(unittest.TestCase):
    """Things that look like a header but are not ours."""

    def test_a_longer_name_is_not_matched(self):
        p = write('[devices.stick2]   # decoy\nmatch = { serial = "S9x7b" }\n\n'
                  '[devices.stick]\nmatch = { serial = "S9x7a" }\n')
        E.remove_device(p, "stick")
        self.assertEqual(list(C.load(p).devices), ["stick2"])
        self.assertIn("# decoy", p.read_text())

    def test_a_header_inside_a_comment_is_ignored(self):
        p = write('# [devices.x] used to be here\n[devices.y]\nmatch = { serial = "S9x7b" }\n\n'
                  '[devices.x]   # the real one\nmatch = { serial = "S9x7a" }\n')
        E.remove_device(p, "x")
        self.assertEqual(list(C.load(p).devices), ["y"])
        self.assertIn("# [devices.x] used to be here", p.read_text())

    def test_a_missing_block_is_still_reported(self):
        p = write('[devices.x]\nmatch = { serial = "S9x7a" }\n')
        with self.assertRaises(C.ConfigError):
            E.remove_device(p, "ghost")

    def test_a_failed_edit_leaves_the_file_alone(self):
        text = '[devices.x]   # note\nmatch = { serial = "S9x7a" }\n\n[[actions]]\nname = "a"\ndevice = "x"\non = ["connected"]\nrun = ["true"]\n'
        p = write(text)
        with self.assertRaises(C.ConfigError):
            E.remove_device(p, "x")                                       # an action still uses it
        self.assertEqual(p.read_text(), text)


class CliTests(unittest.TestCase):
    def test_the_reported_scenario_through_the_cli(self):
        p = write('[devices.commented]   # a comment after the header\nmatch = { serial = "S9x7b" }\n\n'
                  '[tags.rule]   # comment\nmatch = { fs_type = "ext4" }\n\n'
                  '[[actions]]   # comment\nname = "one"\ndevice = "commented"\non = ["connected"]\nrun = ["true"]\n')
        out = io.StringIO()

        def run(*args):
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                return cli.main(["--config", str(p), *args, "--no-reload"])
        self.assertEqual(run("devices", "tag", "commented", "t1"), 0)
        self.assertEqual(run("actions", "remove", "one"), 0)
        self.assertEqual(run("tags", "remove", "rule"), 0)
        self.assertEqual(run("devices", "remove", "commented"), 0)
        self.assertEqual(p.read_text().strip(), "")


if __name__ == "__main__":
    unittest.main()
