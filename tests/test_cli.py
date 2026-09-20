import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path

from udevmon import cli
from udevmon import config as C


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "config.toml"

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.cfg), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_full_flow(self):
        self.assertEqual(self.run_cli("devices", "add", "disk", "--match", "fs_uuid=U1")[0], 0)
        rc, out, _ = self.run_cli("actions", "add", "disk", "mounted,unmounted",
                                  "--name", "log", "--timeout", "5", "--",
                                  "sh", "-c", "echo $1", "sh", "{mountpoint}")
        self.assertEqual(rc, 0, out)
        cfg = C.load(self.cfg)
        self.assertEqual(cfg.actions[0].run, ("sh", "-c", "echo $1", "sh", "{mountpoint}"))
        self.assertEqual((cfg.actions[0].name, cfg.actions[0].timeout), ("log", 5))
        self.assertEqual(self.run_cli("check")[0], 0)
        self.assertIn("disk", self.run_cli("devices", "list")[1])
        self.assertNotEqual(self.run_cli("devices", "remove", "disk")[0], 0)   # in use
        self.assertEqual(self.run_cli("actions", "remove", "log")[0], 0)
        self.assertEqual(self.run_cli("devices", "remove", "disk")[0], 0)
        self.assertNotIn("\n\n\n", self.cfg.read_text())

    def test_actions_add_requires_command_and_valid_events(self):
        self.run_cli("devices", "add", "disk", "--match", "fs_uuid=U1")
        self.assertNotEqual(self.run_cli("actions", "add", "disk", "mounted")[0], 0)
        self.assertNotEqual(self.run_cli("actions", "add", "disk", "bogus", "--", "true")[0], 0)

    def test_check_reports_bad_config(self):
        self.cfg.write_text('[devices.a]\nmatch = {}\n')
        self.cfg.chmod(0o600)
        rc, out, _ = self.run_cli("check")
        self.assertEqual(rc, 1)
        self.assertIn("non-empty", out)


if __name__ == "__main__":
    unittest.main()
