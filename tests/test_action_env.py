# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""An action's own `env` and working folder `cwd`."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import actions as A
from udevmon import cli, explain
from udevmon import config as C
from udevmon import configedit as E
from udevmon.events import MOUNTED, Endpoint, Event

EVENT = Event(MOUNTED, Endpoint("udisks", "k", {"fs_uuid": "U", "mountpoint": "/m"}))


def parse(**extra):
    return C.parse({"devices": {"d": {"match": {"fs_uuid": "U"}}},
                    "actions": [{"name": "a", "device": "d", "on": ["mounted"],
                                 "run": ["true"], **extra}]})


class ConfigTests(unittest.TestCase):
    def test_values(self):
        a = parse(env={"LANG": "C", "TARGET": "{mountpoint}/x"}, cwd="{mountpoint}").actions[0]
        self.assertEqual((dict(a.env), a.cwd), ({"LANG": "C", "TARGET": "{mountpoint}/x"},
                                                "{mountpoint}"))
        self.assertEqual(a.templates, ("true", "C", "{mountpoint}/x", "{mountpoint}"))
        self.assertEqual((dict(parse().actions[0].env), parse().actions[0].cwd), ({}, None))

    def test_bad_values(self):
        for extra, says in (({"env": {"1X": "a"}}, "is not a variable name"),
                            ({"env": {"UDEVMON_X": "a"}}, "is udevmon's own"),
                            ({"env": {"X": 3}}, "'env' must be a table"),
                            ({"env": "X=1"}, "'env' must be a table"),
                            ({"cwd": ""}, "'cwd' must be a folder"),
                            ({"cwd": 3}, "'cwd' must be a folder")):
            with self.subTest(extra):
                with self.assertRaises(C.ConfigError) as cm:
                    parse(**extra)
                self.assertIn(says, str(cm.exception))

    def test_their_placeholders_are_checked_like_the_commands(self):
        with self.assertRaises(C.ConfigError) as cm:
            C.parse({"devices": {"d": {"match": {"serial": "S"}}},
                     "actions": [{"name": "a", "device": "d", "on": ["connected"],
                                  "run": ["true"], "cwd": "{mountpoint}"}]})
        self.assertIn("mountpoint", str(cm.exception))
        with self.assertRaises(C.ConfigError) as cm:
            parse(env={"X": "{var_nope}"})
        self.assertIn("var_nope", str(cm.exception))

    def test_a_relative_program_counts_from_cwd(self):
        with tempfile.TemporaryDirectory() as d:
            tool = Path(d) / "bin" / "tool"
            tool.parent.mkdir()
            tool.write_text("#!/bin/sh\n")
            tool.chmod(0o755)
            a = C.Action("a", ("d",), (), ("mounted",), ("bin/tool",), 10, cwd=d)
            self.assertEqual(C.program_lookup(C.Settings(), a), ("path", str(tool)))
            a = C.Action("a", ("d",), (), ("mounted",), ("bin/tool",), 10, cwd="{mountpoint}")
            self.assertEqual(C.program_lookup(C.Settings(), a), ("placeholder", None))


class RunTests(unittest.TestCase):
    def run_it(self, **kw):
        outcome = {}
        action = C.Action("a", ("d",), (), ("mounted",),
                          kw.pop("run", ("sh", "-c", 'echo "$PWD|$TARGET|$LANG"')), 10, **kw)
        with self.assertLogs("udevmon.actions", "INFO"):
            status = A.run_action(action, EVENT, "d", outcome=outcome)
        return status, outcome

    def test_env_and_cwd_reach_the_command(self):
        with tempfile.TemporaryDirectory() as d:
            real = os.path.realpath(d)
            status, outcome = self.run_it(env={"TARGET": "{mountpoint}/x", "LANG": "C"},
                                          cwd=real)
        self.assertEqual(status, 0)
        self.assertEqual(outcome["output"], (f"{real}|/m/x|C",))

    def test_without_them_the_home_folder(self):
        status, outcome = self.run_it()
        self.assertTrue(outcome["output"][0].startswith(os.path.expanduser("~") + "|"))

    def test_a_missing_folder_is_a_failure_to_start(self):
        status, outcome = self.run_it(cwd="/no/such/folder")
        self.assertIsNone(status)
        self.assertIn("could not start", outcome["failure"])

    def test_the_script_dir_of_a_relative_program_counts_from_cwd(self):
        env = A.build_env(EVENT, "d", program="bin/x", cwd="/srv/base")
        self.assertEqual(env["UDEVMON_SCRIPT_DIR"], "/srv/base/bin")


class ExplainTests(unittest.TestCase):
    def test_explain_shows_them(self):
        cfg = parse(env={"TARGET": "{mountpoint}/x"}, cwd="/srv")
        data = explain.explain_data(cfg, EVENT.endpoint, ["mounted"])
        (run,) = data["events"]["mounted"]["runs"]
        self.assertEqual((run["env"], run["cwd"]), ({"TARGET": "/m/x"}, "/srv"))
        text = "\n".join(explain.explain_endpoint(cfg, EVENT.endpoint, ["mounted"]))
        self.assertIn("(in /srv, with TARGET=/m/x)", text)


class EditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.p.write_text('[devices.d]\nmatch = { fs_uuid = "U" }\n\n[[actions]]\nname = "a"\n'
                          'device = "d"\non = ["mounted"]\nrun = ["true"]\n')
        self.p.chmod(0o600)

    def action(self, name="a"):
        return next(a for a in C.load(self.p).actions if a.name == name)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.p), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_set_and_remove(self):
        E.set_action_options(self.p, "a", set_env={"LANG": "C", "X": "1"}, cwd="/srv")
        a = self.action()
        self.assertEqual((dict(a.env), a.cwd), ({"LANG": "C", "X": "1"}, "/srv"))
        E.set_action_options(self.p, "a", unset_env=["X"], cwd="")
        a = self.action()
        self.assertEqual((dict(a.env), a.cwd), ({"LANG": "C"}, None))
        with self.assertRaises(C.ConfigError):
            E.set_action_options(self.p, "a", unset_env=["NOPE"])

    def test_cli(self):
        rc, _, err = self.run_cli("actions", "add", "--device", "d", "--name", "b",
                                  "--env", "LANG=C", "--cwd", "{mountpoint}", "mounted",
                                  "--", "ls")
        self.assertEqual(rc, 0, err)
        b = self.action("b")
        self.assertEqual((dict(b.env), b.cwd), ({"LANG": "C"}, "{mountpoint}"))
        self.assertRegex(self.run_cli("actions", "list")[1],
                         r"b .*\[cwd: \{mountpoint\}\] \[env: LANG\]")
        self.assertEqual(self.run_cli("actions", "set", "b", "--no-cwd",
                                      "--unset-env", "LANG")[0], 0)
        b = self.action("b")
        self.assertEqual((dict(b.env), b.cwd), ({}, None))
        rc, _, err = self.run_cli("actions", "set", "a", "--env", "UDEVMON_X=1")
        self.assertEqual(rc, 1)
        self.assertIn("udevmon's own", err)


if __name__ == "__main__":
    unittest.main()
