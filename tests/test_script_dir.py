# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`[daemon] script_dir`: folders searched for a command given by bare name, then only the
standard system paths (never the daemon's own PATH)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import config as C
from udevmon import explain
from udevmon.engine import Engine
from udevmon.events import MOUNTED, Endpoint, Event


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.a, self.b, self.system, self.on_path = (self.root / n for n in
                                                     ("a", "b", "system", "onpath"))
        for d in (self.a, self.b, self.system, self.on_path):
            d.mkdir()
        patcher = mock.patch.object(C, "SYSTEM_PATHS", (str(self.system),))
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"PATH": str(self.on_path)})
        env.start()
        self.addCleanup(env.stop)

    def script(self, folder, name, mode=0o755):
        p = folder / name
        p.write_text("#!/bin/sh\ntrue\n")
        p.chmod(mode)
        return str(p)

    def config(self, run, dirs=None, **daemon):
        data = {"devices": {"d": {"match": {"fs_uuid": "U"}}},
                "actions": [{"name": "a", "device": "d", "on": ["mounted"], "run": run}]}
        data["daemon"] = {**({"script_dir": dirs} if dirs is not None else {}), **daemon}
        return C.parse(data)

    def plan(self, cfg):
        engine = Engine(cfg, runner=mock.Mock())
        return engine.plan(Event(MOUNTED, Endpoint("udisks", "k", {"fs_uuid": "U"})))


class ConfigTests(Base):
    def test_a_list_a_single_name_or_nothing(self):
        self.assertEqual(self.config(["x"], [str(self.a), str(self.b)]).settings.script_dir,
                         (str(self.a), str(self.b)))
        self.assertEqual(self.config(["x"], str(self.a)).settings.script_dir, (str(self.a),))
        self.assertEqual(self.config(["x"]).settings.script_dir, ())
        self.assertEqual(self.config(["x"], ["p", "p"]).settings.script_dir, ("p",))

    def test_bad_values_are_refused(self):
        for bad in (5, [1], [""], {"a": "b"}):
            with self.subTest(bad):
                with self.assertRaises(C.ConfigError) as cm:
                    self.config(["x"], bad)
                self.assertIn("script_dir", str(cm.exception))


class LookupTests(Base):
    def test_the_folders_are_searched_in_order_then_the_system_paths(self):
        cfg = self.config(["tool"], [str(self.a), str(self.b)])
        system = self.script(self.system, "tool")             # also there: must come last
        second = self.script(self.b, "tool")
        self.assertEqual(C.resolve_program(cfg.settings, "tool"), second)
        first = self.script(self.a, "tool")
        self.assertEqual(C.resolve_program(cfg.settings, "tool"), first)
        os.remove(first), os.remove(second)
        self.assertEqual(C.resolve_program(cfg.settings, "tool"), system)

    def test_the_daemons_path_is_ignored(self):
        self.script(self.on_path, "tool")                        # only on PATH
        cfg = self.config(["tool"], [str(self.a)])
        self.assertIsNone(C.resolve_program(cfg.settings, "tool"))

    def test_only_executable_files_count(self):
        self.script(self.a, "tool", mode=0o644)
        (self.a / "adir").mkdir()
        cfg = self.config(["tool"], [str(self.a)])
        self.assertIsNone(C.resolve_program(cfg.settings, "tool"))
        self.assertIsNone(C.resolve_program(cfg.settings, "adir"))

    def test_tilde_is_expanded(self):
        with mock.patch.dict(os.environ, {"HOME": str(self.root)}):
            self.script(self.a, "tool")
            cfg = self.config(["tool"], ["~/a"])
            self.assertEqual(C.resolve_program(cfg.settings, "tool"), str(self.a / "tool"))

    def test_which_commands_use_it(self):
        s = self.config(["x"], [str(self.a)]).settings
        self.assertTrue(C.uses_script_dir(s, "tool"))
        self.assertFalse(C.uses_script_dir(s, "/usr/bin/tool"))
        self.assertFalse(C.uses_script_dir(s, "~/bin/tool"))
        self.assertFalse(C.uses_script_dir(s, "{mountpoint}"))
        self.assertFalse(C.uses_script_dir(self.config(["x"]).settings, "tool"))


class EngineTests(Base):
    def test_the_job_runs_the_full_path_and_explain_shows_it(self):
        path = self.script(self.a, "tool")
        cfg = self.config(["tool", "{mountpoint}"], [str(self.a)])
        jobs = self.plan(cfg).jobs
        self.assertEqual(jobs[0].action.run, (path, "{mountpoint}"))
        text = "\n".join(explain.explain_endpoint(
            cfg, Endpoint("udisks", "k", {"fs_uuid": "U", "mountpoint": "/m"}), [MOUNTED]))
        self.assertIn(f"{path} /m", text)

    def test_a_script_added_later_is_found_without_a_reload(self):
        cfg = self.config(["tool"], [str(self.a)])
        self.assertEqual(self.plan(cfg).jobs, [])
        path = self.script(self.a, "tool")
        self.assertEqual(self.plan(cfg).jobs[0].action.run, (path,))

    def test_not_found_skips_with_a_reason_and_warns_for_a_real_event(self):
        cfg = self.config(["tool"], [str(self.a)])
        plan = self.plan(cfg)
        self.assertEqual(plan.jobs, [])
        self.assertIn("not found in script_dir or the standard system paths",
                      plan.skipped[0].reason)
        engine = Engine(cfg, runner=mock.Mock())
        with self.assertLogs("udevmon.engine", "WARNING") as logs:
            engine.handle(Event(MOUNTED, Endpoint("udisks", "k", {"fs_uuid": "U"})))
        self.assertIn("'tool' not found", logs.output[0])

    def test_without_script_dir_nothing_changes(self):
        cfg = self.config(["tool"])
        self.assertEqual(self.plan(cfg).jobs[0].action.run, ("tool",))

    def test_paths_and_placeholders_are_left_alone(self):
        for prog in ("/usr/bin/true", "~/bin/x", "{mountpoint}"):
            with self.subTest(prog):
                cfg = self.config([prog], [str(self.a)])
                self.assertEqual(self.plan(cfg).jobs[0].action.run, (prog,))

    def test_tag_actions_resolve_too(self):
        path = self.script(self.a, "tool")
        cfg = C.parse({"devices": {"d": {"match": {"fs_uuid": "U"}, "tags": ["t"]}},
                       "daemon": {"script_dir": [str(self.a)]},
                       "actions": [{"name": "a", "tag": "t", "on": ["mounted"],
                                    "run": ["tool"]}]})
        self.assertEqual(self.plan(cfg).jobs[0].action.run, (path,))


class CheckTests(Base):
    def test_a_missing_folder_and_a_missing_program_are_reported(self):
        cfg = self.config(["tool"], [str(self.root / "nope")])
        problems = C.command_problems(cfg)
        self.assertTrue(any("script_dir" in p and "nope" in p for p in problems))
        self.assertTrue(any("'tool' was not found in script_dir or the standard system "
                            "paths" in p for p in problems))

    def test_a_missing_folder_is_only_a_note_when_another_exists(self):
        self.script(self.a, "tool")
        cfg = self.config(["tool"], [str(self.root / "nope"), str(self.a)])
        self.assertEqual(C.command_problems(cfg), [])
        notes = C.command_notes(cfg)
        self.assertEqual(len(notes), 1)
        self.assertIn("nope", notes[0])

    def test_all_folders_missing_is_still_a_warning(self):
        cfg = self.config(["/bin/true"], [str(self.root / "nope"), str(self.root / "nada")])
        self.assertEqual(len(C.command_problems(cfg)), 2)
        self.assertEqual(C.command_notes(cfg), [])

    def test_a_file_in_place_of_a_folder_is_still_a_warning(self):
        (self.root / "file").write_text("")
        cfg = self.config(["/bin/true"], [str(self.root / "file"), str(self.a)])
        self.assertEqual(len(C.command_problems(cfg)), 1)
        self.assertEqual(C.command_notes(cfg), [])

    def test_a_program_in_the_folder_is_fine_even_if_not_on_the_path(self):
        self.script(self.a, "tool")
        self.assertEqual(C.command_problems(self.config(["tool"], [str(self.a)])), [])

    def test_without_script_dir_the_path_is_used_as_before(self):
        self.assertTrue(C.command_problems(self.config(["tool"])))
        self.script(self.on_path, "tool")
        self.assertEqual(C.command_problems(self.config(["tool"])), [])


if __name__ == "__main__":
    unittest.main()
