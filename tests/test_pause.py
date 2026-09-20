"""The global pause: udevmon pause / resume and the daemon honouring it."""

import contextlib
import io
import logging
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_engine_actions import FakeRunner
from udevmon import cli, daemon, pause
from udevmon import config as C
from udevmon.engine import Engine
from udevmon.events import CONNECTED, MOUNTED, SHUTDOWN, STARTUP, Endpoint, Event
from udevmon.runtime import pause_path


class PauseTestCase(unittest.TestCase):
    def setUp(self):
        pause.resume()
        self.addCleanup(pause.resume)


class DurationTests(unittest.TestCase):
    def test_good(self):
        for text, seconds in (("30m", 1800), ("2h", 7200), ("90s", 90), ("1d", 86400),
                              ("1h30m", 5400), ("45M", 2700), (" 2h ", 7200), ("1d2h3m4s", 93784)):
            with self.subTest(text):
                self.assertEqual(pause.parse_duration(text), float(seconds))

    def test_bad(self):
        for text in ("", "5", "m", "x", "1h 30m", "-5m", "1.5h", "30 m", "1w", "h1"):
            with self.subTest(text):
                with self.assertRaises(ValueError):
                    pause.parse_duration(text)
        with self.assertRaises(ValueError) as cm:
            pause.parse_duration("0m")
        self.assertIn("longer than zero", str(cm.exception))


class FlagFileTests(PauseTestCase):
    def test_pause_until_resumed(self):
        self.assertEqual(pause.status(), (False, None))
        self.assertIsNone(pause.pause())
        self.assertEqual(pause.status(), (True, None))
        self.assertTrue(pause.is_paused())
        self.assertEqual(pause_path().read_text(), "")
        self.assertTrue(pause.resume())
        self.assertFalse(pause.is_paused())
        self.assertFalse(pause.resume())                      # nothing to end

    def test_the_file_is_private(self):
        pause.pause()
        self.assertEqual(stat.S_IMODE(os.stat(pause_path()).st_mode), 0o600)
        self.assertFalse(pause_path().with_name("udevmon.paused.tmp").exists())   # atomic write

    def test_a_timed_pause_ends_by_itself_and_is_tidied_up(self):
        expiry = pause.pause(60, now=1000.0)
        self.assertEqual(expiry, 1060.0)
        self.assertEqual(pause.status(now=1059.9), (True, 1060.0))
        self.assertEqual(pause.status(now=1060.0), (False, None))
        self.assertFalse(pause_path().exists())                 # removed once over

    def test_pausing_again_replaces_it(self):
        pause.pause(60, now=1000.0)
        pause.pause(None)
        self.assertEqual(pause.status(now=5000.0), (True, None))
        pause.pause(10, now=1000.0)
        self.assertEqual(pause.status(now=1005.0), (True, 1010.0))

    def test_a_file_with_odd_content_pauses_until_resumed(self):
        pause_path().write_text("what is this")
        self.assertEqual(pause.status(), (True, None))
        pause_path().write_text("  \n")
        self.assertEqual(pause.status(), (True, None))

    def test_describe_until(self):
        self.assertIn("udevmon resume", pause.describe_until(None))
        now = 1_800_000_000.0
        self.assertRegex(pause.describe_until(now + 600, now), r"^until \d\d:\d\d$")
        self.assertRegex(pause.describe_until(now + 3 * 86400, now), r"^until \d{4}-\d\d-\d\d \d\d:\d\d$")

    def test_it_lives_in_the_runtime_directory_next_to_the_lock(self):
        self.assertEqual(pause_path().parent, daemon.lock_path().parent)
        self.assertEqual(pause_path().name, "udevmon.paused")


CFG = ('[devices.d]\nmatch = { serial = "S9x7a" }\n\n[devices.f]\nmatch = { fs_uuid = "U1" }\n\n'
       '[[actions]]\nname = "a"\ndevice = "d"\non = ["connected", "startup", "shutdown"]\nrun = ["true"]\n'
       '[[actions]]\nname = "m"\ndevice = "f"\non = ["mounted"]\nrun = ["true"]\n')
USB = Endpoint("udev", "/sys/1", {"serial": "S9x7a"})
FS = Endpoint("udisks", "/o/1", {"fs_uuid": "U1", "mountpoint": "/m"})


class EngineTests(unittest.TestCase):
    def engine(self, paused):
        self.runner = FakeRunner()
        self.flag = {"paused": paused}
        return Engine(C.parse(C.tomllib.loads(CFG)), self.runner, clock=lambda: 0.0,
                      paused=lambda: self.flag["paused"])

    def test_nothing_runs_while_paused_not_even_startup_and_shutdown(self):
        engine = self.engine(True)
        for kind in (CONNECTED, STARTUP, SHUTDOWN):
            self.assertEqual(engine.handle(Event(kind, USB)), [])
        self.assertEqual(engine.handle(Event(MOUNTED, FS)), [])
        self.assertEqual(self.runner.calls, [])

    def test_things_run_again_after_a_resume(self):
        engine = self.engine(True)
        engine.handle(Event(CONNECTED, USB))
        self.flag["paused"] = False
        engine.handle(Event(CONNECTED, USB))
        self.assertEqual(len(self.runner.calls), 1)

    def test_events_lost_during_a_pause_are_not_replayed(self):
        engine = self.engine(True)
        engine.handle(Event(MOUNTED, FS))
        self.flag["paused"] = False
        self.assertEqual(self.runner.calls, [])

    def test_the_start_and_end_of_a_pause_are_logged_once_each(self):
        engine = self.engine(False)
        with self.assertLogs("udevmon.engine", "INFO") as logs:
            engine.handle(Event(CONNECTED, USB))
            self.flag["paused"] = True
            engine.handle(Event(STARTUP, USB))
            engine.handle(Event(MOUNTED, FS))
            self.flag["paused"] = False
            engine.handle(Event(CONNECTED, USB))
        text = "\n".join(logs.output)
        self.assertEqual(text.count("automation paused"), 1)
        self.assertEqual(text.count("automation resumed"), 1)

    def test_explain_is_not_affected_by_a_pause(self):
        engine = self.engine(True)
        self.assertEqual([j.action.name for j in engine.plan(Event(CONNECTED, USB)).jobs], ["a"])

    def test_the_default_reads_the_flag_file(self):
        pause.resume()
        self.addCleanup(pause.resume)
        runner = FakeRunner()
        engine = Engine(C.parse(C.tomllib.loads(CFG)), runner, clock=lambda: 0.0)
        engine.handle(Event(CONNECTED, USB))
        self.assertEqual(len(runner.calls), 1)
        pause.pause()
        engine._last.clear()
        engine.handle(Event(CONNECTED, USB))
        self.assertEqual(len(runner.calls), 1)                  # skipped
        pause.resume()
        engine._last.clear()
        engine.handle(Event(CONNECTED, USB))
        self.assertEqual(len(runner.calls), 2)

    def test_the_shutdown_pass_of_the_daemon_is_held_back_too(self):
        engine = self.engine(True)
        d = daemon.Daemon.__new__(daemon.Daemon)
        d.engine = engine

        class Source:
            def present(self):
                return [USB]
        d.sources = [Source()]
        d.fire_shutdown()
        self.assertEqual(self.runner.calls, [])


class CliTests(PauseTestCase):
    def run_cli(self, *args, daemon_running=False):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.running_pid", return_value=123 if daemon_running else None), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(args))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_pause_and_resume(self):
        rc, out, _ = self.run_cli("pause", daemon_running=True)
        self.assertEqual(rc, 0)
        self.assertIn("Automation paused until you run `udevmon resume`.", out)
        self.assertNotIn("No daemon is running", out)
        self.assertTrue(pause.is_paused())
        rc, out, _ = self.run_cli("resume")
        self.assertIn("Automation resumed.", out)
        self.assertFalse(pause.is_paused())
        self.assertIn("was not paused", self.run_cli("resume")[1])

    def test_a_timed_pause(self):
        rc, out, _ = self.run_cli("pause", "--for", "30m", daemon_running=True)
        self.assertRegex(out, r"paused until \d\d:\d\d\.")
        self.assertIn("`udevmon resume` to continue sooner", out)
        paused, until = pause.status()
        self.assertTrue(paused and until is not None)
        rc, out, _ = self.run_cli("pause", "--for", "2h", daemon_running=True)
        self.assertIn("Already paused; now paused until", out)

    def test_it_works_without_a_daemon_and_says_so(self):
        rc, out, _ = self.run_cli("pause")
        self.assertEqual(rc, 0)
        self.assertIn("No daemon is running right now; the pause applies once one starts.", out)
        self.assertTrue(pause.is_paused())

    def test_a_bad_duration_changes_nothing(self):
        for bad in ("soon", "0m", "5"):
            rc, _, err = self.run_cli("pause", "--for", bad)
            self.assertEqual((rc, "error:" in err), (1, True))
        self.assertFalse(pause.is_paused())

    def test_editing_and_explain_mention_the_pause(self):
        d = tempfile.mkdtemp()
        cfg = Path(d) / "c.toml"
        pause.pause()
        out = io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=False), \
                contextlib.redirect_stdout(out):
            cli.main(["--config", str(cfg), "devices", "add", "a", "--match", "serial=S9x7a"])
        self.assertIn("Note: automation is paused (until you run `udevmon resume`): nothing runs.",
                      out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--config", str(cfg), "explain", "connected", "--device", "a"])
        self.assertTrue(out.getvalue().startswith("Note: automation is paused"))
        pause.resume()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["--config", str(cfg), "explain", "connected", "--device", "a"])
        self.assertNotIn("paused", out.getvalue())

    def test_a_failed_edit_does_not_add_the_note(self):
        pause.pause()
        d = tempfile.mkdtemp()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with self.assertRaises(SystemExit):
                cli.main(["--config", str(Path(d) / "c.toml"), "devices", "remove", "ghost"])
        self.assertNotIn("Note:", out.getvalue())

    def test_the_daemon_start_line_says_paused(self):
        d = daemon.Daemon(Path("/nonexistent/c.toml"), dry_run=True)
        d.cfg = C.parse(C.tomllib.loads(CFG))
        self.assertEqual(d.banner(), "udevmon running: 2 device(s), 2 action(s) [dry-run]")
        pause.pause()
        self.assertEqual(d.banner(), "udevmon running: 2 device(s), 2 action(s) [dry-run] [PAUSED]")


if __name__ == "__main__":
    unittest.main()
