import contextlib
import io
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli
from udevmon import config as C
from udevmon import daemon


def write_lock(text):
    daemon.lock_path().write_text(text)


class LockFileTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(lambda: daemon.lock_path().unlink(missing_ok=True))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "c.toml"
        self.cfg.write_text("")

    def running(self, alive=True):
        return mock.patch("udevmon.daemon._is_udevmon", return_value=alive)

    def test_parsing_new_and_old_lock_files(self):
        with self.running():
            write_lock(f"123\n{self.cfg}\n")
            self.assertEqual(daemon.running_daemon(), (123, str(self.cfg)))
            self.assertEqual(daemon.running_pid(), 123)
            write_lock("123")                                # written by an older daemon
            self.assertEqual(daemon.running_daemon(), (123, None))
            write_lock("garbage")
            self.assertIsNone(daemon.running_daemon())
        with self.running(alive=False):
            write_lock(f"123\n{self.cfg}\n")
            self.assertIsNone(daemon.running_daemon())        # stale: nobody home
        daemon.lock_path().unlink()
        self.assertIsNone(daemon.running_daemon())

    def test_reload_only_the_daemon_that_uses_this_config(self):
        other = Path(self.tmp.name) / "other.toml"
        with self.running(), mock.patch("os.kill") as kill:
            write_lock(f"123\n{self.cfg}\n")
            self.assertTrue(daemon.reload_running(self.cfg))
            kill.assert_called_once_with(123, signal.SIGHUP)
            kill.reset_mock()
            self.assertFalse(daemon.reload_running(other))   # a different config: leave it alone
            kill.assert_not_called()
            self.assertTrue(daemon.reload_running())            # no config given: plain reload
            kill.reset_mock()
            write_lock("123")                                # config unknown: assume it matches
            self.assertTrue(daemon.reload_running(other))

    def test_same_file_through_different_spellings(self):
        link = Path(self.tmp.name) / "link.toml"
        link.symlink_to(self.cfg)
        with self.running(), mock.patch("os.kill") as kill:
            write_lock(f"7\n{self.cfg}\n")
            self.assertTrue(daemon.reload_running(link))
            kill.assert_called_once()

    def test_a_failing_signal_is_not_an_error(self):
        with self.running(), mock.patch("os.kill", side_effect=PermissionError):
            write_lock(f"123\n{self.cfg}\n")
            self.assertFalse(daemon.reload_running(self.cfg))

    def test_nothing_running(self):
        self.assertFalse(daemon.reload_running(self.cfg))

    def test_the_daemon_records_its_pid_and_config(self):
        d = daemon.Daemon(self.cfg)
        fd = d._acquire_lock()
        self.addCleanup(os.close, fd)
        pid, _, rest = daemon.lock_path().read_text().partition("\n")
        self.assertEqual((int(pid), rest.strip()), (os.getpid(), os.path.realpath(self.cfg)))


class AutoReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "c.toml"

    def run_cli(self, *args, reload_result=True):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=reload_result) as reload, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.cfg), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), reload

    def test_every_editing_command_reloads(self):
        steps = [
            ("devices", "add", "d", "--match", "serial=S9x7a"),
            ("devices", "tag", "d", "t1"),
            ("devices", "untag", "d", "t1"),
            ("tags", "add", "storage", "--match", "fs_type=ext4"),
            ("actions", "add", "--device", "d", "connected", "--name", "a", "--", "true"),
            ("actions", "remove", "a"),
            ("tags", "remove", "storage"),
            ("devices", "remove", "d"),
        ]
        for step in steps:
            with self.subTest(step=step[:2]):
                rc, out, reload = self.run_cli(*step)
                self.assertEqual(rc, 0, out)
                self.assertIn("Reloaded the running daemon.", out)
                reload.assert_called_once_with(self.cfg)

    def test_the_opt_out_and_no_daemon(self):
        rc, out, reload = self.run_cli("devices", "add", "d", "--match", "serial=S9x7a", "--no-reload")
        self.assertEqual(rc, 0)
        reload.assert_not_called()
        self.assertNotIn("Reloaded", out)
        rc, out, reload = self.run_cli("devices", "add", "e", "--match", "serial=S9x7b",
                                       reload_result=False)          # nobody to tell
        self.assertNotIn("Reloaded", out)

    def test_a_failed_edit_does_not_reload(self):
        self.run_cli("devices", "add", "d", "--match", "serial=S9x7a", "--no-reload")
        rc, out, reload = self.run_cli("devices", "add", "d", "--match", "serial=S9x7a")   # duplicate
        self.assertEqual(rc, 1)
        reload.assert_not_called()
        # a failure that returns 1 instead of raising (bad event name)
        rc, out, reload = self.run_cli("actions", "add", "--device", "d", "bogus", "--", "true")
        self.assertEqual(rc, 1)
        reload.assert_not_called()

    def test_read_only_commands_never_reload(self):
        self.run_cli("devices", "add", "d", "--match", "serial=S9x7a", "--no-reload")
        for cmd in (("devices", "list"), ("tags", "list"), ("actions", "list"), ("check",),
                    ("devices", "show", "d")):
            rc, out, reload = self.run_cli(*cmd)
            reload.assert_not_called()

    def test_scan_reloads_after_saving_a_device(self):
        from udevmon.events import Endpoint
        from udevmon.scan import Scanner
        scanner = Scanner(self.cfg, prompt=True)
        scanner.loop = mock.Mock()
        scanner.pending = {"a": Endpoint("udev", "k", {"vendor_id": "1", "product_id": "2",
                                                       "serial": "S9x7a"})}
        answers = iter(["1", "", "stick", "", "", ""])
        out = io.StringIO()
        with mock.patch("builtins.input", lambda p="": next(answers)), \
                mock.patch("udevmon.daemon.reload_running", return_value=True) as reload, \
                contextlib.redirect_stdout(out):
            scanner._prompt_user()
        reload.assert_called_once_with(self.cfg)
        self.assertIn("Reloaded the running daemon.", out.getvalue())


class CommandProblemTests(unittest.TestCase):
    def cfg(self, *runs):
        actions = "".join(f'[[actions]]\nname = "a{i}"\ndevice = "d"\non = ["connected"]\n'
                          f'run = {run}\n' for i, run in enumerate(runs))
        return C.parse(C.tomllib.loads('[devices.d]\nmatch = { serial = "S9x7a" }\n' + actions))

    def test_finds_missing_and_unusable_programs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        plain = Path(tmp.name) / "plain.txt"
        plain.write_text("x")
        script = Path(tmp.name) / "run.sh"
        script.write_text("#!/bin/sh\n")
        script.chmod(0o755)
        cfg = self.cfg('["definitely-not-a-program"]', f'["{tmp.name}/missing.sh"]',
                       f'["{plain}"]', f'["{script}"]', '["sh", "-c", "true"]', '["{device}"]')
        problems = C.command_problems(cfg)
        self.assertEqual(len(problems), 3)
        self.assertIn("'definitely-not-a-program' was not found in PATH", problems[0])
        self.assertIn("missing.sh' does not exist", problems[1])
        self.assertIn("plain.txt' is not an executable file", problems[2])

    def test_home_and_the_given_path(self):
        cfg = self.cfg('["~/no-such-dir/x.sh"]', '["sh"]')
        problems = C.command_problems(cfg)
        self.assertEqual(len(problems), 1)
        self.assertIn(os.path.expanduser("~/no-such-dir/x.sh"), problems[0])
        self.assertEqual(len(C.command_problems(cfg, path="/nonexistent")), 2)   # sh not found there

    def test_check_prints_them_as_warnings_without_failing(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "c.toml"
        path.write_text('[devices.d]\nmatch = { serial = "S9x7a" }\n[[actions]]\nname = "x"\n'
                        'device = "d"\non = ["connected"]\nrun = ["definitely-not-a-program"]\n')
        path.chmod(0o600)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--config", str(path), "check"])
        self.assertEqual(rc, 0)
        self.assertIn("warning: action 'x': command 'definitely-not-a-program' was not found", out.getvalue())

    def test_a_reload_warns_about_them(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "c.toml"
        path.write_text('[devices.d]\nmatch = { serial = "S9x7a" }\n[[actions]]\nname = "x"\n'
                        'device = "d"\non = ["connected"]\nrun = ["definitely-not-a-program"]\n')
        path.chmod(0o600)
        d = daemon.Daemon(path)
        d.cfg = C.Config()
        d.engine = mock.Mock()
        with self.assertLogs("udevmon.daemon", "WARNING") as logs:
            d.reload()
        self.assertTrue(any("'definitely-not-a-program' was not found" in m for m in logs.output))
        d.engine.set_config.assert_called_once()


if __name__ == "__main__":
    unittest.main()
