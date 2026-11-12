# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`retries` and `retry_delay`: run again after a non-zero exit."""

import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from udevmon import actions as A
from udevmon import cli, explain
from udevmon import config as C
from udevmon import configedit as E
from udevmon.engine import Job
from udevmon.events import MOUNTED, Endpoint, Event

EVENT = Event(MOUNTED, Endpoint("udisks", "k", {"mountpoint": "/m"}))


def job(name="a", retries=0, delay=1.0, lock=None):
    return Job(C.Action(name, ("d",), (), ("mounted",), ("true",), 10, lock=lock,
                        retries=retries, retry_delay=delay), "d", None)


class FakeStop:
    """Stands in for the runner's stopping event: records the waits, never really waits."""

    def __init__(self, stop_after=None):
        self.waits, self.stop_after = [], stop_after

    def wait(self, delay):
        self.waits.append(delay)
        return self.stop_after is not None and len(self.waits) >= self.stop_after

    def set(self):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.statuses = []                     # what run_action returns, in order
        patcher = mock.patch.object(A, "run_action", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        paused = mock.patch.object(A.pause_flag, "is_paused", return_value=False)
        self.paused = paused.start()
        self.addCleanup(paused.stop)

    def fake(self, action, event, device, tag=None, **kwargs):
        self.calls.append(action.name)
        return self.statuses[len(self.calls) - 1]

    def run_job(self, j, stop=None):
        runner = A.ActionRunner(1)
        self.addCleanup(runner.shutdown, True)
        runner._stopping = stop or FakeStop()
        with self.assertLogs("udevmon.actions", "INFO") as logs:
            log = logs
            runner._run_one(EVENT, j, None)
            A.log.info("end")                       # assertLogs needs at least one record
        return runner, [r.getMessage() for r in log.records]


class RetryTests(Base):
    def test_it_succeeds_on_the_third_attempt(self):
        self.statuses = [1, 1, 0]
        stop = FakeStop()
        _, messages = self.run_job(job(retries=5, delay=2.5), stop)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(stop.waits, [2.5, 2.5])                # the delay is honoured
        self.assertIn("action 'a' attempt 1 of 6 failed with status 1; retrying in 2.5 s",
                      messages)
        self.assertIn("action 'a' attempt 2 of 6 failed with status 1; retrying in 2.5 s",
                      messages)
        self.assertFalse(any("gave up" in m for m in messages))

    def test_it_gives_up_after_the_last_retry(self):
        self.statuses = [1, 2, 3]
        stop = FakeStop()
        _, messages = self.run_job(job(retries=2), stop)
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(len(stop.waits), 2)                     # no wait after the last one
        self.assertIn("action 'a' gave up after 3 attempts (last status 3)", messages)

    def test_no_retries_by_default(self):
        self.statuses = [1]
        stop = FakeStop()
        _, messages = self.run_job(job(), stop)
        self.assertEqual((len(self.calls), stop.waits), (1, []))
        self.assertFalse(any("gave up" in m or "retrying" in m for m in messages))

    def test_success_the_first_time_never_waits(self):
        self.statuses = [0]
        stop = FakeStop()
        self.run_job(job(retries=3), stop)
        self.assertEqual((len(self.calls), stop.waits), (1, []))

    def test_a_timeout_a_skip_or_a_failure_to_start_is_not_retried(self):
        self.statuses = [None]                                   # what run_action returns
        stop = FakeStop()
        self.run_job(job(retries=3), stop)
        self.assertEqual((len(self.calls), stop.waits), (1, []))

    def test_it_stops_when_automation_is_paused(self):
        self.statuses = [1, 1, 0]
        self.paused.side_effect = [False, True]
        stop = FakeStop()
        _, messages = self.run_job(job(retries=5), stop)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(any("automation is paused, so not retrying" in m for m in messages))

    def test_it_stops_when_the_daemon_is_stopping(self):
        self.statuses = [1, 1, 1, 1]
        stop = FakeStop(stop_after=2)
        _, messages = self.run_job(job(retries=5), stop)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(any("the daemon is stopping, so not retrying" in m for m in messages))

    def test_a_real_shutdown_interrupts_the_wait(self):
        self.statuses = [1] * 100
        runner = A.ActionRunner(1)
        with self.assertLogs("udevmon.actions", "INFO"):
            runner.submit(EVENT, [job(retries=100, delay=30)])
            deadline = time.time() + 5
            while not self.calls and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.1)                                      # now it sits in its 30 s wait
            t = time.time()
            runner.shutdown(wait=True)
            self.assertLess(time.time() - t, 3)
        self.assertEqual(len(self.calls), 1)

    def test_the_lock_is_held_across_the_attempts(self):
        self.statuses = [1, 1, 0, 0]
        stop = FakeStop()
        runner = A.ActionRunner(2)
        self.addCleanup(runner.shutdown, True)
        runner._stopping = stop
        with self.assertLogs("udevmon.actions", "INFO"):
            runner.submit(EVENT, [job("first", retries=2, lock="yk")])
            runner.submit(EVENT, [job("second", lock="yk")])
            runner.shutdown(wait=True)
        self.assertEqual(self.calls, ["first", "first", "first", "second"])


class RealCommandTests(unittest.TestCase):
    def test_a_script_that_fails_twice_then_succeeds(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter, script = Path(tmp) / "n", Path(tmp) / "s.sh"
            script.write_text(f'#!/bin/bash\necho x >> "{counter}"\n'
                              f'[ "$(wc -l < "{counter}")" -ge 3 ]\n')
            script.chmod(0o755)
            j = Job(C.Action("flaky", ("d",), (), ("mounted",), (str(script),), 10, retries=5,
                             retry_delay=0.05), "d", None)
            runner = A.ActionRunner(1)
            with self.assertLogs("udevmon.actions", "INFO"):
                runner.submit(EVENT, [j]).result(timeout=10)
                runner.shutdown(wait=True)
            self.assertEqual(len(counter.read_text().split()), 3)


class ConfigTests(unittest.TestCase):
    def parse(self, **extra):
        return C.parse({"devices": {"d": {"match": {"serial": "S"}}},
                        "actions": [{"name": "a", "device": "d", "on": ["mounted"],
                                     "run": ["true"], **extra}]})

    def test_defaults_and_values(self):
        a = self.parse().actions[0]
        self.assertEqual((a.retries, a.retry_delay), (0, 1.0))
        a = self.parse(retries=10, retry_delay=2).actions[0]
        self.assertEqual((a.retries, a.retry_delay), (10, 2.0))
        self.assertEqual(self.parse(retries=0).actions[0].retries, 0)

    def test_bad_values_are_refused(self):
        for extra, text in (({"retries": -1}, "'retries' must be a whole number"),
                            ({"retries": 1.5}, "'retries' must be a whole number"),
                            ({"retries": "3"}, "'retries' must be a whole number"),
                            ({"retries": True}, "'retries' must be a whole number"),
                            ({"retry_delay": 0}, "'retry_delay' must be a number"),
                            ({"retry_delay": -2}, "'retry_delay' must be a number"),
                            ({"retry_delay": "1"}, "'retry_delay' must be a number"),
                            ({"retries": 2, "detach": True}, "no meaning with 'detach'"),
                            ({"retry_delay": 2, "detach": True}, "no meaning with 'detach'")):
            with self.subTest(extra):
                with self.assertRaises(C.ConfigError) as cm:
                    self.parse(**extra)
                self.assertIn(text, str(cm.exception))


class EditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.p.write_text('[devices.d]\nmatch = { serial = "S" }\n\n[[actions]]\nname = "a"\n'
                          'device = "d"\non = ["mounted"]\nrun = ["true"]   # keep\n\n'
                          '[[actions]]\nname = "b"\ndevice = "d"\non = ["mounted"]\n'
                          'run = ["true"]\nretries = 4\nretry_delay = 0.5\n')
        self.p.chmod(0o600)

    def action(self, name):
        return next(a for a in C.load(self.p).actions if a.name == name)

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=False), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.p), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_set_change_and_remove(self):
        E.set_action_options(self.p, "a", retries=10, retry_delay=1)
        a = self.action("a")
        self.assertEqual((a.retries, a.retry_delay), (10, 1.0))
        self.assertIn("# keep", self.p.read_text())
        E.set_action_options(self.p, "b", retries=0)
        self.assertEqual(self.action("b").retries, 0)
        self.assertNotIn("retries", self.p.read_text().split('name = "b"')[1])
        self.assertEqual(self.action("b").retry_delay, 0.5)      # the delay stays
        E.set_action_options(self.p, "b", retry_delay=2.25)
        self.assertEqual(self.action("b").retry_delay, 2.25)

    def test_a_detached_action_cannot_be_given_retries(self):
        before = self.p.read_bytes()
        E.set_action_options(self.p, "a", detach=True)
        with self.assertRaises(C.ConfigError):
            E.set_action_options(self.p, "a", retries=3)
        self.p.write_bytes(before)

    def test_an_edit_that_does_nothing_is_rolled_back(self):
        before = self.p.read_bytes()
        with mock.patch.object(E, "_edited", return_value=before.decode()):
            with self.assertRaises(C.ConfigError):
                E.set_action_options(self.p, "a", retries=3)
        self.assertEqual(self.p.read_bytes(), before)

    def test_a_delay_edit_that_does_nothing_is_rolled_back_too(self):
        before = self.p.read_bytes()
        with mock.patch.object(E, "_edited", return_value=before.decode()):
            with self.assertRaises(C.ConfigError):
                E.set_action_options(self.p, "b", retry_delay=9)
        self.assertEqual(self.p.read_bytes(), before)

    def test_cli(self):
        rc, _, _ = self.run_cli("actions", "add", "--device", "d", "--retries", "10",
                                "--retry-delay", "2", "--name", "c", "mounted", "--", "true")
        self.assertEqual(rc, 0)
        c = self.action("c")
        self.assertEqual((c.retries, c.retry_delay), (10, 2.0))
        self.assertIn("retries = 10", self.p.read_text())
        self.assertRegex(self.run_cli("actions", "list")[1], r"c .*\[retries: 10\]")
        self.assertEqual(self.run_cli("actions", "set", "a", "--retries", "3")[0], 0)
        self.assertEqual(self.action("a").retries, 3)
        self.assertEqual(self.run_cli("actions", "set", "a", "--retries", "0")[0], 0)
        self.assertEqual(self.action("a").retries, 0)

    def test_cli_refuses_bad_numbers_and_detach_together(self):
        for args, text in ((("--retries", "-1"), "--retries must be 0 or more"),
                           (("--retry-delay", "0"), "--retry-delay must be greater than 0"),
                           (("--retries", "2", "--detach"), "no meaning with --detach")):
            with self.subTest(args):
                rc, _, err = self.run_cli("actions", "add", "--device", "d", *args, "mounted",
                                          "--", "true")
                self.assertEqual(rc, 1)
                self.assertIn(text, err)
        for args, text in ((("--retries", "-2"), "--retries must be 0 or more"),
                           (("--retry-delay", "0"), "--retry-delay must be greater than 0")):
            with self.subTest(("set", *args)):
                rc, _, err = self.run_cli("actions", "set", "a", *args)
                self.assertEqual(rc, 1)
                self.assertIn(text, err)

    def test_explain_shows_it(self):
        text = "\n".join(explain.explain_endpoint(
            C.load(self.p), Endpoint("udisks", "k", {"serial": "S", "mountpoint": "/m"}),
            [MOUNTED]))
        self.assertIn("runs 'b' (device d, up to 4 retries)", text)


if __name__ == "__main__":
    unittest.main()
