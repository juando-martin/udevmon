# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""The `check` notes for overlapping devices, single-use locks and dead exclusions."""

import os
import tempfile
import unittest
from pathlib import Path

from udevmon import config as C


def notes(text: str) -> list[str]:
    return C.parse(C.tomllib.loads(text)).notes


ACTION = '''
[[actions]]
tag = "t"
on = ["mounted"]
run = ["/bin/true"]
'''


class OverlapTests(unittest.TestCase):
    def test_a_match_inside_another(self):
        found = notes('''
[devices.fs]
match = { fs_uuid = "A" }
[devices.key]
match = { fs_uuid = "A", drive_serial = "S" }
tags = ["t"]
''' + ACTION)
        overlap = [n for n in found if "never matches alone" in n]
        self.assertEqual(len(overlap), 1, found)
        self.assertIn("device 'key' never matches alone", overlap[0])
        self.assertIn("{device} = 'fs'", overlap[0])
        self.assertNotIn("drop-in", overlap[0])             # one file: nothing about names

    def test_the_same_match(self):
        found = notes('[devices.a]\nmatch = { serial = "1" }\n'
                      '[devices.b]\nmatch = { serial = "1" }\n')
        self.assertTrue(any("'a' and 'b' have the same match" in n for n in found), found)

    def test_different_values_do_not_overlap(self):
        found = notes('[devices.a]\nmatch = { fs_uuid = "A" }\n'
                      '[devices.b]\nmatch = { fs_uuid = "B", drive_serial = "S" }\n')
        self.assertFalse(any("matches alone" in n or "same match" in n for n in found), found)

    def test_an_ignored_broad_device_silences_the_other(self):
        found = notes('[devices.all]\nmatch = { vendor_id = "090c" }\nstatus = "ignored"\n'
                      '[devices.pen]\nmatch = { vendor_id = "090c", serial = "1" }\n')
        self.assertTrue(any("'all' is ignored, so nothing ever runs for 'pen'" in n
                            for n in found), found)

    def test_ignoring_part_of_a_broad_device_is_not_noted(self):
        found = notes('[devices.all]\nmatch = { vendor_id = "090c" }\n'
                      '[devices.pen]\nmatch = { vendor_id = "090c", serial = "1" }\n'
                      'status = "ignored"\n')
        self.assertFalse(any("matches alone" in n for n in found), found)

    def test_across_files_the_note_names_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            main = Path(tmp) / "config.toml"
            main.write_text('[devices.fs]\nmatch = { fs_uuid = "A" }\n')
            (Path(tmp) / "config.d").mkdir(mode=0o700)
            dropin = Path(tmp) / "config.d" / "50-tool.toml"
            dropin.write_text('[devices.key]\nmatch = { fs_uuid = "A", drive_serial = "S" }\n')
            for f in (main, dropin):
                os.chmod(f, 0o600)
            found = C.load(main).notes
        overlap = [n for n in found if "never matches alone" in n]
        self.assertEqual(len(overlap), 1, found)
        self.assertIn("(the main file, then config.d/50-tool.toml: the drop-in file names "
                      "decide it)", overlap[0])


class LockTests(unittest.TestCase):
    def action(self, name, lock):
        return f'[[actions]]\nname = "{name}"\ntag = "t"\non = ["mounted"]\n' \
               f'run = ["/bin/true"]\nlock = "{lock}"\n'

    def test_a_lock_used_once(self):
        found = notes('[devices.d]\nmatch = { serial = "1" }\ntags = ["t"]\n'
                      + self.action("a", "keydisk") + self.action("b", "keydisk")
                      + self.action("c", "keydsk"))
        locks = [n for n in found if "the only one with lock" in n]
        self.assertEqual(locks, ["action 'c' is the only one with lock 'keydsk', so it only "
                                 "keeps that action's own runs from overlapping (a typo for "
                                 "'keydisk'?)"])

    def test_a_shared_lock_is_not_noted(self):
        found = notes('[devices.d]\nmatch = { serial = "1" }\ntags = ["t"]\n'
                      + self.action("a", "x") + self.action("b", "x"))
        self.assertFalse(any("only one with lock" in n for n in found), found)


class ExcludeTests(unittest.TestCase):
    def test_a_rule_that_can_never_match(self):
        found = notes('[tags.storage]\nmatch = { fs_type = ["ext4", "btrfs"] }\n'
                      '[devices.key]\nmatch = { fs_uuid = "A", fs_type = "vfat" }\n'
                      'exclude_tags = ["storage"]\n')
        self.assertTrue(any("excludes tag 'storage', whose rule can never match it" in n
                            for n in found), found)

    def test_a_rule_that_can_match_is_not_noted(self):
        for rule in ('{ fs_type = ["vf*"] }', '{ fs_label = "KEY" }'):   # a pattern; a key
            found = notes(f'[tags.storage]\nmatch = {rule}\n'               # the device lacks
                          '[devices.key]\nmatch = { fs_uuid = "A", fs_type = "vfat" }\n'
                          'exclude_tags = ["storage"]\n')
            self.assertFalse(any("can never match" in n for n in found), (rule, found))


if __name__ == "__main__":
    unittest.main()
