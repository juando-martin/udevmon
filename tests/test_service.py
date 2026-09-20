import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli, completion, service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.calls = []
        self.unit_existed = []

    def fake_run(self, argv, **kw):
        self.calls.append(argv[2:])
        self.unit_existed.append((self.dir / "systemd" / "user" / "udevmon.service").exists())

    def real_mode(self):
        """XDG_CONFIG_HOME in the temp dir, and no real systemctl."""
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name}))
        stack.enter_context(mock.patch("udevmon.service.subprocess.run", self.fake_run))

    def test_install_then_uninstall_a_file_only(self):
        with mock.patch("udevmon.service.subprocess.run", self.fake_run):
            unit = service.install(self.dir, now=True)
            self.assertTrue(unit.exists())
            self.assertEqual(service.uninstall(self.dir), ("removed", unit))
            self.assertFalse(unit.exists())
            self.assertEqual(service.uninstall(self.dir), ("absent", unit))      # idempotent
        self.assertEqual(self.calls, [])          # --dest never touches systemd

    def test_real_uninstall_stops_it_before_removing_the_file(self):
        self.real_mode()
        service.install(None, now=False)
        self.calls.clear()
        self.unit_existed.clear()
        state, unit = service.uninstall(None)
        self.assertEqual(state, "removed")
        self.assertFalse(unit.exists())
        self.assertEqual(self.calls, [["disable", "--now", "udevmon.service"], ["daemon-reload"]])
        self.assertEqual(self.unit_existed, [True, False])      # disabled while the file existed

    def test_nothing_installed_means_no_systemctl_calls(self):
        self.real_mode()
        self.assertEqual(service.uninstall(None)[0], "absent")
        self.assertEqual(self.calls, [])

    def test_a_foreign_unit_is_never_removed(self):
        self.real_mode()
        unit = service.unit_dir() / "udevmon.service"
        unit.parent.mkdir(parents=True)
        unit.write_text("[Service]\nExecStart=/usr/bin/something-else\n")
        self.assertEqual(service.uninstall(None), ("foreign", unit))
        self.assertTrue(unit.exists())
        self.assertEqual(self.calls, [])

    def test_a_missing_systemctl_is_not_an_error(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name}):
            unit = service.unit_dir() / "udevmon.service"
            unit.parent.mkdir(parents=True)
            unit.write_text("ExecStart=/x/udevmon run\n")
            with mock.patch("udevmon.service.subprocess.run", side_effect=FileNotFoundError):
                self.assertEqual(service.uninstall(None)[0], "removed")
            self.assertFalse(unit.exists())


class CliUninstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(list(args))
        return rc, out.getvalue(), err.getvalue()

    def test_service_uninstall(self):
        self.assertEqual(self.run_cli("service", "install", "--dest", str(self.dir))[0], 0)
        rc, out, _ = self.run_cli("service", "uninstall", "--dest", str(self.dir))
        self.assertEqual(rc, 0)
        self.assertIn("Removed", out)
        rc, out, _ = self.run_cli("service", "uninstall", "--dest", str(self.dir))
        self.assertEqual((rc, "Nothing to remove" in out), (0, True))
        (self.dir / "udevmon.service").write_text("[Service]\nExecStart=/bin/true\n")
        rc, out, err = self.run_cli("service", "uninstall", "--dest", str(self.dir))
        self.assertEqual(rc, 1)
        self.assertIn("does not look like a udevmon unit", err)
        self.assertTrue((self.dir / "udevmon.service").exists())

    def test_completion_uninstall(self):
        with mock.patch.dict(os.environ, {"XDG_DATA_HOME": self.tmp.name}):
            self.assertEqual(self.run_cli("completion", "install")[0], 0)
            target = completion.default_install_path()
            self.assertTrue(target.exists())
            rc, out, _ = self.run_cli("completion", "uninstall")
            self.assertEqual((rc, target.exists(), "Removed" in out), (0, False, True))
            rc, out, _ = self.run_cli("completion", "uninstall")            # again: fine
            self.assertEqual((rc, "Nothing to remove" in out), (0, True))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("# somebody else's completion\n")
            rc, _, err = self.run_cli("completion", "uninstall")
            self.assertEqual(rc, 1)
            self.assertIn("not a udevmon completion script", err)
            self.assertTrue(target.exists())

    def test_completion_uninstall_with_dest(self):
        dest = self.dir / "x" / "udevmon"
        self.assertEqual(self.run_cli("completion", "install", "--dest", str(dest))[0], 0)
        self.assertEqual(self.run_cli("completion", "uninstall", "--dest", str(dest))[0], 0)
        self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
