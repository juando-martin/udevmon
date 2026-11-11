# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`udevmon apply`: a sequence of editing commands as one change."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli
from udevmon import config as C

BASE = '[devices.old]\nmatch = { fs_uuid = "OLD" }\n'

STEPS = [["devices", "add", "key", "--match", "fs_uuid=U1", "--file", "50-tool"],
         ["devices", "tag", "key", "keepass"],
         ["actions", "add", "--device", "key", "mounted", "--name", "up", "--file", "50-tool",
          "--", "echo", "{mountpoint}"]]


class ApplyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.main = Path(tmp.name) / "config.toml"
        self.main.write_text(BASE)
        self.main.chmod(0o600)
        self.dropin = Path(tmp.name) / "config.d" / "50-tool.toml"

    def apply(self, steps, *extra, reload=None):
        text = steps if isinstance(steps, str) else json.dumps(steps)
        out, err = io.StringIO(), io.StringIO()
        reloaded = mock.Mock(return_value=reload)
        with mock.patch("sys.stdin", io.StringIO(text)), \
                mock.patch("udevmon.daemon.reload_running", reloaded), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.main), "apply", *extra])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue(), reloaded

    def test_all_steps_are_written_and_the_daemon_reloads_once(self):
        rc, out, err, reloaded = self.apply(STEPS, reload={"via": "socket"})
        self.assertEqual(rc, 0, err)
        cfg = C.load(self.main)
        self.assertEqual(cfg.devices["key"].tags, ("keepass",))
        self.assertEqual([a.name for a in cfg.actions], ["up"])
        self.assertEqual(cfg.source("action", "up"), self.dropin)
        self.assertIn("Applied 3 step(s): changed config.d/50-tool.toml.", out)
        self.assertIn("Reloaded the running daemon.", out)
        self.assertNotIn(tempfile.gettempdir() + "/udevmon-apply", out)   # real paths shown
        reloaded.assert_called_once()

    def test_a_failing_step_writes_nothing(self):
        steps = [*STEPS, ["devices", "tag", "nope", "x"]]
        rc, out, err, reloaded = self.apply(steps)
        self.assertEqual(rc, 1)
        self.assertIn("step 4 (devices tag) failed; nothing was written", err)
        self.assertEqual(self.main.read_text(), BASE)
        self.assertFalse(self.dropin.exists())
        reloaded.assert_not_called()

    def test_dry_run_shows_one_diff_and_writes_nothing(self):
        rc, out, err, reloaded = self.apply(STEPS, "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"+++ {self.dropin}", out)
        self.assertIn('+name = "up"', out)
        self.assertIn("Dry run: nothing was written.", out)
        self.assertFalse(self.dropin.exists())
        reloaded.assert_not_called()

    def test_from_a_file_and_no_reload(self):
        steps = Path(self.main.parent) / "steps.json"
        steps.write_text(json.dumps(STEPS[:1]))
        rc, out, err, reloaded = self.apply("", str(steps), "--no-reload")
        self.assertEqual(rc, 0, err)
        self.assertIn("key", C.load(self.main).devices)
        reloaded.assert_not_called()

    def test_bad_input(self):
        for text, says in (("not json", "not JSON"),
                           ("[]", "JSON array of commands"),
                           ('[["devices", 3]]', "JSON array of commands"),
                           ('[["devices", "list", "--config", "/x"]]', "--config is not allowed"),
                           ('[["devices", "remove", "old", "--dry-run"]]', "--dry-run is not"),
                           ('[["check"]]', "not an editing command"),
                           ('[["edit"]]', "not an editing command"),
                           ('[["devices", "frobnicate"]]', "not a valid command")):
            with self.subTest(text):
                rc, _, err, _ = self.apply(text)
                self.assertEqual(rc, 1)
                self.assertIn(says, err)
        self.assertEqual(self.main.read_text(), BASE)

    def test_a_file_changed_meanwhile_is_not_overwritten(self):
        real_run = cli._run_captured

        def meddle(fn, args):
            self.main.write_text(BASE + "# someone else\n")
            return real_run(fn, args)
        with mock.patch.object(cli, "_run_captured", side_effect=meddle):
            rc, _, err, _ = self.apply(STEPS[:1])
        self.assertEqual(rc, 1)
        self.assertIn("changed while the steps ran", err)
        self.assertFalse(self.dropin.exists())

    def test_nothing_to_change(self):
        rc, out, _, reloaded = self.apply([["devices", "disable", "old"],
                                           ["devices", "enable", "old"]])
        self.assertEqual(rc, 0)
        self.assertIn("No file changed.", out)
        reloaded.assert_not_called()


if __name__ == "__main__":
    unittest.main()
