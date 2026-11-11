# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""A drop-in's own `script_dir`, and the program `actions list --json` reports."""

import os
import tempfile
import unittest
from pathlib import Path

from udevmon import config as C
from udevmon import jsonout
from udevmon.engine import Engine
from udevmon.events import MOUNTED, Endpoint, Event


def executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.main = self.root / "config.toml"
        self.dropins = self.root / "config.d"
        self.dropins.mkdir(mode=0o700)
        self.mine = executable(self.root / "mine" / "tool")
        self.theirs = executable(self.root / "theirs" / "tool")
        executable(self.root / "theirs" / "only-theirs")

    def write(self, path: Path, text: str) -> None:
        path.write_text(text)
        path.chmod(0o600)

    def action(self, name, run, device="d"):
        return (f'[[actions]]\nname = "{name}"\ndevice = "{device}"\non = ["mounted"]\n'
                f'run = {run}\n')

    def setup_files(self, main_dir=True):
        daemon = f'[daemon]\nscript_dir = ["{self.mine.parent}"]\n' if main_dir else ""
        self.write(self.main, daemon + '[devices.d]\nmatch = { fs_uuid = "U" }\n'
                   + self.action("main-tool", '["tool"]')
                   + self.action("main-only-theirs", '["only-theirs"]'))
        self.write(self.dropins / "50-tool.toml",
                   f'[daemon]\nscript_dir = "{self.theirs.parent}"\n'
                   + self.action("dropin-tool", '["tool"]')
                   + self.action("dropin-only-theirs", '["only-theirs"]'))
        return C.load(self.main)


class DropinScriptDirTests(Base):
    def test_a_dropins_folders_are_for_its_own_actions_and_come_first(self):
        cfg = self.setup_files()
        by = {a.name: a for a in cfg.actions}
        self.assertEqual(cfg.settings.script_dir, (str(self.mine.parent),))
        self.assertEqual(by["dropin-tool"].script_dir, (str(self.theirs.parent),))
        self.assertEqual(by["main-tool"].script_dir, ())
        lookup = {n: C.program_lookup(cfg.settings, a) for n, a in by.items()}
        self.assertEqual(lookup["dropin-tool"], ("script_dir", str(self.theirs)))
        self.assertEqual(lookup["dropin-only-theirs"][1], str(self.theirs.parent / "only-theirs"))
        self.assertEqual(lookup["main-tool"], ("script_dir", str(self.mine)))
        self.assertEqual(lookup["main-only-theirs"], ("script_dir", None))   # not its folder

    def test_without_a_main_script_dir_only_the_dropins_actions_use_one(self):
        cfg = self.setup_files(main_dir=False)
        by = {a.name: a for a in cfg.actions}
        self.assertEqual(C.program_lookup(cfg.settings, by["dropin-tool"]),
                         ("script_dir", str(self.theirs)))
        self.assertEqual(C.program_lookup(cfg.settings, by["main-tool"]), ("PATH", None))

    def test_the_engine_runs_the_dropins_program(self):
        cfg = self.setup_files()
        engine = Engine(cfg, runner=None, paused=lambda: False)
        event = Event(MOUNTED, Endpoint("udisks", "k", {"fs_uuid": "U", "mountpoint": "/m"}))
        plan = engine.plan(event)
        jobs = {j.action.name: j.action.run[0] for j in plan.jobs}
        self.assertEqual(jobs["dropin-tool"], str(self.theirs))
        self.assertEqual(jobs["main-tool"], str(self.mine))
        self.assertIn("main-only-theirs", [s.action.name for s in plan.skipped])

    def test_check_reports_a_missing_dropin_folder_and_program(self):
        self.write(self.main, '[devices.d]\nmatch = { fs_uuid = "U" }\n')
        self.write(self.dropins / "50-x.toml", '[daemon]\nscript_dir = ["/no/such/dir"]\n'
                   + self.action("x", '["nothing-here-xyz"]'))
        problems = C.command_problems(C.load(self.main))
        self.assertTrue(any("'/no/such/dir'" in p and "50-x.toml" in p for p in problems))
        self.assertTrue(any("'nothing-here-xyz' was not found in script_dir" in p
                            for p in problems))

    def test_only_script_dir_and_only_folders(self):
        self.write(self.main, "")
        for text, says in (("[daemon]\ndebounce = 2\n", "may only have script_dir"),
                           ('[daemon]\nscript_dir = ["/a"]\nmax_workers = 2\n',
                            "may only have script_dir"),
                           ("[daemon]\nscript_dir = []\n", "must be a folder or a list"),
                           ("[daemon]\nscript_dir = [3]\n", "must be a folder or a list")):
            with self.subTest(text):
                self.write(self.dropins / "50-x.toml", text)
                with self.assertRaises(C.ConfigError) as cm:
                    C.load(self.main)
                self.assertIn(says, str(cm.exception))


class ProgramFieldTests(Base):
    def entry(self, cfg, name):
        return jsonout.action_entry(cfg, next(a for a in cfg.actions if a.name == name))

    def test_the_json_says_which_program_runs(self):
        cfg = self.setup_files()
        e = self.entry(cfg, "dropin-tool")
        self.assertEqual((e["program"], e["program_lookup"], e["script_dir"]),
                         (str(self.theirs), "script_dir", [str(self.theirs.parent)]))
        e = self.entry(cfg, "main-only-theirs")
        self.assertEqual((e["program"], e["program_lookup"]), (None, "script_dir"))

    def test_a_path_a_placeholder_and_the_daemons_path(self):
        home = Path(os.path.expanduser("~"))
        self.write(self.main, '[devices.d]\nmatch = { fs_uuid = "U" }\n'
                   + self.action("abs", f'["{self.mine}"]')
                   + self.action("gone", '["/no/such/program"]')
                   + self.action("ph", '["{mountpoint}/run.sh"]')
                   + self.action("bare", '["sh"]'))
        cfg = C.load(self.main)
        self.assertEqual(C.program_lookup(cfg.settings, cfg.actions[0]), ("path", str(self.mine)))
        self.assertEqual(C.program_lookup(cfg.settings, cfg.actions[1]), ("path", None))
        self.assertEqual(C.program_lookup(cfg.settings, cfg.actions[2]), ("placeholder", None))
        self.assertEqual(C.program_lookup(cfg.settings, cfg.actions[3]), ("PATH", None))
        rel = C.Action("rel", ("d",), (), ("mounted",), ("bin/x",), 10)
        self.assertEqual(C.program_lookup(cfg.settings, rel)[0], "path")
        self.assertEqual(os.path.dirname(os.path.dirname(
            os.path.join(str(home), "bin/x"))), str(home))


if __name__ == "__main__":
    unittest.main()
