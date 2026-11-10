# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""The runner waits for the command, not for its output pipe, and `detach` does not wait."""

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from udevmon import actions as A
from udevmon import cli, explain
from udevmon import config as C
from udevmon import configedit as E
from udevmon.events import MOUNTED, Endpoint, Event

EVENT = Event(MOUNTED, Endpoint("udisks", "k", {"mountpoint": "/m"}))


def until(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def script(self, name, body):
        p = self.dir / name
        p.write_text("#!/bin/bash\n" + body)
        p.chmod(0o755)
        return str(p)

    def action(self, run, timeout=10, detach=False):
        return C.Action("t", ("d",), (), ("mounted",), tuple(run), timeout, detach=detach)

    def run_action(self, action):
        """(status, seconds taken, log records so far) -- the reader may still add more."""
        t = time.time()
        with self.assertLogs("udevmon.actions", "INFO") as logs:
            rc = A.run_action(action, EVENT, "d")
        return rc, time.time() - t, logs


class WaitsForTheCommandTests(Base):
    def test_the_output_is_logged_line_by_line_before_the_exit(self):
        rc, took, logs = self.run_action(self.action([self.script("s.sh", "echo one\necho two\n")]))
        self.assertEqual(rc, 0)
        lines = [r.getMessage() for r in logs.records]
        self.assertEqual([l for l in lines if l.startswith("[t]")], ["[t] one", "[t] two"])
        self.assertLess(lines.index("[t] two"), lines.index("action 't' exited with status 0"))

    def test_the_exit_status_is_returned_and_logged(self):
        rc, _, logs = self.run_action(self.action([self.script("f.sh", "exit 3\n")]))
        self.assertEqual(rc, 3)
        self.assertTrue(any(r.levelname == "WARNING" and "status 3" in r.getMessage()
                            for r in logs.records))

    def test_a_script_that_leaves_a_program_running_returns_at_once(self):
        """The regression: `program &` keeps the output pipe open, and the runner used to wait
        for the pipe, i.e. for the whole timeout."""
        late = self.script("bg.sh", 'echo start\n( sleep 1; echo "late line" ) &\ndisown\n'
                                    'echo end\n')
        t = time.time()
        with self.assertLogs("udevmon.actions", "INFO") as logs:
            rc = A.run_action(self.action([late], timeout=8), EVENT, "d")
            took = time.time() - t
            self.assertEqual(rc, 0)
            self.assertLess(took, 0.9, "the runner waited for the background program")
            # ... and what the program writes afterwards is still logged
            self.assertTrue(until(lambda: any(r.getMessage() == "[t] late line"
                                              for r in logs.records), 4))
        self.assertFalse(any("timed out" in r.getMessage() for r in logs.records))

    def test_a_script_that_hangs_is_still_killed_at_the_timeout(self):
        marker = self.dir / "survived"
        hang = self.script("hang.sh", f'sleep 30 && touch "{marker}"\n')
        t = time.time()
        with self.assertLogs("udevmon.actions", "INFO") as logs:
            rc = A.run_action(self.action([hang], timeout=0.5), EVENT, "d")
        self.assertIsNone(rc)
        self.assertLess(time.time() - t, 3)            # SIGTERM at once, not SIGKILL after 5 s
        self.assertTrue(any("timed out" in r.getMessage() for r in logs.records))
        time.sleep(0.3)
        self.assertFalse(marker.exists())

    def test_a_command_that_cannot_start_returns_none(self):
        with self.assertLogs("udevmon.actions", "ERROR"):
            self.assertIsNone(A.run_action(self.action(["/nonexistent/x"]), EVENT, "d"))

    def test_the_output_pipe_is_closed_when_the_command_is_done(self):
        procs = []
        real = A.subprocess.Popen

        def capture(*args, **kwargs):
            procs.append(real(*args, **kwargs))
            return procs[-1]
        with mock.patch.object(A.subprocess, "Popen", side_effect=capture), \
                self.assertLogs("udevmon.actions", "INFO"):
            A.run_action(self.action([self.script("q.sh", "echo hi\n")]), EVENT, "d")
        self.assertTrue(until(lambda: procs[0].stdout.closed))


class DetachTests(Base):
    def test_it_starts_the_command_and_does_not_wait(self):
        slow = self.script("slow.sh", 'echo started\nsleep 1\necho finished\n')
        t = time.time()
        with self.assertLogs("udevmon.actions", "INFO") as logs:
            rc = A.run_action(self.action([slow], detach=True), EVENT, "d")
            took = time.time() - t
            self.assertIsNone(rc)
            self.assertLess(took, 0.5)
            got = lambda text: any(r.getMessage() == text for r in logs.records)
            self.assertTrue(until(lambda: got("[t] finished"), 5))
            self.assertTrue(until(lambda: got("detached action 't' exited with status 0"), 5))
        self.assertTrue(any("starting (detached)" in r.getMessage() for r in logs.records))

    def test_no_timeout_applies(self):
        slow = self.script("slow2.sh", "sleep 1\necho done\n")
        with self.assertLogs("udevmon.actions", "INFO") as logs:
            A.run_action(self.action([slow], timeout=0.2, detach=True), EVENT, "d")
            self.assertTrue(until(lambda: any(r.getMessage() == "[t] done"
                                              for r in logs.records), 5))
        self.assertFalse(any("timed out" in r.getMessage() for r in logs.records))

    def test_a_failing_detached_command_is_logged_as_a_warning(self):
        with self.assertLogs("udevmon.actions", "INFO") as logs:
            A.run_action(self.action([self.script("bad.sh", "exit 2\n")], detach=True),
                         EVENT, "d")
            self.assertTrue(until(lambda: any(r.levelname == "WARNING" and "status 2"
                                              in r.getMessage() for r in logs.records), 5))

    def test_it_runs_in_its_own_session(self):
        out = self.dir / "sid"
        sid = self.script("sid.sh", f'ps -o sid= -p $$ > "{out}"\n')
        A.run_action(self.action([sid], detach=True), EVENT, "d")
        self.assertTrue(until(out.exists))
        self.assertTrue(until(lambda: out.read_text().strip() != ""))
        self.assertNotEqual(int(out.read_text()), os.getsid(0))

    def test_a_dry_run_starts_nothing(self):
        marker = self.dir / "ran"
        with self.assertLogs("udevmon.actions", "INFO"):
            A.run_action(self.action(["touch", str(marker)], detach=True), EVENT, "d",
                         dry_run=True)
        time.sleep(0.2)
        self.assertFalse(marker.exists())

    def test_a_command_that_cannot_start_is_reported(self):
        with self.assertLogs("udevmon.actions", "ERROR") as logs:
            self.assertIsNone(A.run_action(self.action(["/nonexistent/x"], detach=True),
                                           EVENT, "d"))
        self.assertIn("failed to start", logs.output[0])

    def test_the_actions_after_it_are_not_held_up(self):
        first = self.script("first.sh", "sleep 2\n")
        second = self.dir / "second"
        runner = A.ActionRunner(1)
        from udevmon.engine import Job
        jobs = [Job(self.action([first], detach=True), "d", None),
                Job(self.action(["touch", str(second)]), "d", None)]
        with self.assertLogs("udevmon.actions", "INFO"):
            runner.submit(EVENT, jobs)
            self.assertTrue(until(second.exists, 1.5))            # long before `first` ends
        runner.shutdown(wait=True)


class ConfigTests(unittest.TestCase):
    def parse(self, **extra):
        return C.parse({"devices": {"d": {"match": {"serial": "S"}}},
                        "actions": [{"name": "a", "device": "d", "on": ["mounted"],
                                     "run": ["true"], **extra}]})

    def test_default_and_true(self):
        self.assertFalse(self.parse().actions[0].detach)
        self.assertTrue(self.parse(detach=True).actions[0].detach)

    def test_it_must_be_a_boolean(self):
        with self.assertRaises(C.ConfigError) as cm:
            self.parse(detach="yes")
        self.assertIn("'detach' must be true or false", str(cm.exception))

    def test_a_timeout_makes_no_sense_with_it(self):
        with self.assertRaises(C.ConfigError) as cm:
            self.parse(detach=True, timeout=30)
        self.assertIn("'timeout' has no meaning with 'detach'", str(cm.exception))
        self.parse(detach=False, timeout=30)                      # fine


class EditTests(Base):
    def setUp(self):
        super().setUp()
        self.p = self.dir / "c.toml"
        self.p.write_text('[devices.d]\nmatch = { serial = "S" }\n\n[[actions]]\nname = "a"\n'
                          'device = "d"\non = ["mounted"]\nrun = ["true"]   # keep\n\n'
                          '[[actions]]\nname = "slow"\ndevice = "d"\non = ["mounted"]\n'
                          'run = ["true"]\ntimeout = 30\n')
        self.p.chmod(0o600)

    def detach(self, name):
        return next(a for a in C.load(self.p).actions if a.name == name).detach

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.p), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_set_and_clear_keep_the_rest(self):
        E.set_action_options(self.p, "a", detach=True)
        self.assertTrue(self.detach("a"))
        self.assertIn("# keep", self.p.read_text())
        E.set_action_options(self.p, "a", detach=False)
        self.assertFalse(self.detach("a"))
        self.assertNotIn("detach", self.p.read_text())

    def test_an_edit_that_does_nothing_is_rolled_back(self):
        before = self.p.read_bytes()
        with mock.patch.object(E, "_edited", return_value=before.decode()):
            with self.assertRaises(C.ConfigError):
                E.set_action_options(self.p, "a", detach=True)
        self.assertEqual(self.p.read_bytes(), before)

    def test_an_action_with_a_timeout_cannot_be_detached(self):
        before = self.p.read_bytes()
        with self.assertRaises(C.ConfigError) as cm:
            E.set_action_options(self.p, "slow", detach=True)
        self.assertIn("'timeout' has no meaning", str(cm.exception))
        self.assertEqual(self.p.read_bytes(), before)

    def test_cli_add_set_list(self):
        rc, out, _ = self.run_cli("actions", "add", "--device", "d", "--detach", "--name", "k",
                                  "mounted", "--", "keepassxc")
        self.assertEqual(rc, 0)
        self.assertTrue(self.detach("k"))
        self.assertIn("detach = true", self.p.read_text())
        rc, out, _ = self.run_cli("actions", "list")
        self.assertRegex(out, r"k .*\[detach\]")
        rc, _, _ = self.run_cli("actions", "set", "k", "--no-detach")
        self.assertFalse(self.detach("k"))
        rc, _, _ = self.run_cli("actions", "set", "a", "--detach")
        self.assertTrue(self.detach("a"))
        rc, _, _ = self.run_cli("actions", "set", "a", "--detach", "--no-detach")
        self.assertEqual(rc, 2)                                  # mutually exclusive

    def test_cli_add_refuses_a_timeout_with_detach(self):
        rc, _, err = self.run_cli("actions", "add", "--device", "d", "--detach", "--timeout", "5",
                                  "mounted", "--", "true")
        self.assertEqual(rc, 1)
        self.assertIn("--timeout has no meaning with --detach", err)

    def test_explain_says_detached(self):
        E.set_action_options(self.p, "a", detach=True)
        c = C.load(self.p)
        text = "\n".join(explain.explain_endpoint(
            c, Endpoint("udisks", "k", {"serial": "S", "mountpoint": "/m"}), [MOUNTED]))
        self.assertIn("runs 'a' (device d, detached)", text)


if __name__ == "__main__":
    unittest.main()
