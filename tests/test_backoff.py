# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`retry_backoff`, and the retries of `shutdown` actions while the daemon stops."""

import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from udevmon import actions as A
from udevmon import cli, jsonout
from udevmon import config as C
from udevmon import configedit as E
from udevmon.engine import Job
from udevmon.events import MOUNTED, SHUTDOWN, Endpoint, Event

from tests.test_retries import FakeStop


def event(kind=MOUNTED):
    return Event(kind, Endpoint("udisks", "k", {"mountpoint": "/m"}))


def job(retries=3, delay=1.0, backoff=1.0, name="a"):
    return Job(C.Action(name, ("d",), (), ("mounted", "shutdown"), ("true",), 10,
                        retries=retries, retry_delay=delay, retry_backoff=backoff), "d", None)


class Base(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.statuses = []
        patcher = mock.patch.object(A, "run_action", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        paused = mock.patch.object(A.pause_flag, "is_paused", return_value=False)
        paused.start()
        self.addCleanup(paused.stop)

    def fake(self, action, event, device, tag=None, **kwargs):
        self.calls.append(action.name)
        return self.statuses[len(self.calls) - 1]


class BackoffTests(Base):
    def waits(self, j, statuses):
        self.statuses = statuses
        runner = A.ActionRunner(1)
        self.addCleanup(runner.shutdown, True)
        runner._stopping = stop = FakeStop()
        with self.assertLogs("udevmon.actions", "INFO") as logs:
            runner._run_one(event(), j, None)
        return stop.waits, [r.getMessage() for r in logs.records]

    def test_each_wait_is_the_factor_times_the_one_before(self):
        waits, messages = self.waits(job(retries=4, delay=2, backoff=2), [1, 1, 1, 1, 0])
        self.assertEqual(waits, [2, 4, 8, 16])
        self.assertIn("action 'a' attempt 3 of 5 failed with status 1; retrying in 8 s",
                      messages)

    def test_the_default_keeps_the_delay(self):
        waits, _ = self.waits(job(retries=3, delay=1.5), [1, 1, 1, 0])
        self.assertEqual(waits, [1.5, 1.5, 1.5])

    def test_a_fractional_factor(self):
        waits, _ = self.waits(job(retries=2, delay=2, backoff=1.5), [1, 1, 0])
        self.assertEqual(waits, [2, 3])


class ShutdownRetryTests(Base):
    def test_a_shutdown_event_keeps_retrying_while_the_daemon_stops(self):
        self.statuses = [1, 1, 0]
        runner = A.ActionRunner(1)
        runner._stopping.set()                          # the daemon is stopping
        with self.assertLogs("udevmon.actions", "INFO") as logs:
            status = runner._run_one(event(SHUTDOWN), job(retries=3, delay=0.01), None).status
        self.assertEqual((status, len(self.calls)), (0, 3))
        self.assertFalse(any("so not retrying" in r.getMessage() for r in logs.records))

    def test_another_event_stops_retrying(self):
        self.statuses = [1, 1, 0]
        runner = A.ActionRunner(1)
        runner._stopping.set()
        with self.assertLogs("udevmon.actions", "INFO") as logs:
            status = runner._run_one(event(MOUNTED), job(retries=3, delay=0.01), None).status
        self.assertEqual((status, len(self.calls)), (1, 1))
        self.assertTrue(any("so not retrying" in r.getMessage() for r in logs.records))

    def test_shutdown_without_waiting_cuts_a_shutdown_event_short(self):
        self.statuses = [1] * 10
        runner = A.ActionRunner(1)
        with self.assertLogs("udevmon.actions", "INFO"):
            future = runner.submit(event(SHUTDOWN), [job(retries=9, delay=30)])
            deadline = time.time() + 5
            while not self.calls and time.time() < deadline:
                time.sleep(0.01)
            time.sleep(0.1)                             # now in its 30 s wait
            t = time.time()
            runner.shutdown(wait=False)
            self.assertEqual([r[:2] for r in future.result(5)], [("a", 1)])
            self.assertLess(time.time() - t, 3)
        self.assertEqual(len(self.calls), 1)


class ConfigTests(unittest.TestCase):
    def parse(self, **extra):
        return C.parse({"devices": {"d": {"match": {"serial": "S"}}},
                        "actions": [{"name": "a", "device": "d", "on": ["mounted"],
                                     "run": ["true"], **extra}]})

    def test_default_and_value(self):
        self.assertEqual(self.parse().actions[0].retry_backoff, 1.0)
        self.assertEqual(self.parse(retries=3, retry_backoff=2).actions[0].retry_backoff, 2.0)

    def test_bad_values_are_refused(self):
        for extra, text in (({"retry_backoff": 0.5}, "'retry_backoff' must be a number, 1"),
                            ({"retry_backoff": "2"}, "'retry_backoff' must be a number, 1"),
                            ({"retry_backoff": True}, "'retry_backoff' must be a number, 1"),
                            ({"retry_backoff": 2, "detach": True}, "no meaning with 'detach'")):
            with self.subTest(extra):
                with self.assertRaises(C.ConfigError) as cm:
                    self.parse(**extra)
                self.assertIn(text, str(cm.exception))

    def test_json_lists_it(self):
        cfg = self.parse(retries=2, retry_backoff=3)
        self.assertEqual(jsonout.action_entry(cfg, cfg.actions[0])["retry_backoff"], 3.0)


class EditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.p.write_text('[devices.d]\nmatch = { serial = "S" }\n\n[[actions]]\nname = "a"\n'
                          'device = "d"\non = ["mounted"]\nrun = ["true"]\nretries = 4\n')
        self.p.chmod(0o600)

    def action(self, name="a"):
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

    def test_set_and_remove(self):
        E.set_action_options(self.p, "a", retry_backoff=2)
        self.assertEqual(self.action().retry_backoff, 2.0)
        self.assertIn("retry_backoff = 2\n", self.p.read_text())
        E.set_action_options(self.p, "a", retry_backoff=1.5)
        self.assertEqual(self.action().retry_backoff, 1.5)
        E.set_action_options(self.p, "a", retry_backoff=1)          # the default: no key
        self.assertNotIn("retry_backoff", self.p.read_text())

    def test_cli_add_set_and_list(self):
        rc, _, err = self.run_cli("actions", "add", "--device", "d", "--retries", "5",
                                  "--retry-backoff", "2", "--name", "c", "mounted", "--", "true")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.action("c").retry_backoff, 2.0)
        self.assertRegex(self.run_cli("actions", "list")[1], r"c .*\[backoff: 2\]")
        self.assertEqual(self.run_cli("actions", "set", "a", "--retry-backoff", "3")[0], 0)
        self.assertEqual(self.action().retry_backoff, 3.0)

    def test_cli_refuses_a_factor_below_one_and_detach(self):
        for args, text in ((("--retry-backoff", "0.5"), "--retry-backoff must be 1 or more"),
                           (("--retry-backoff", "2", "--detach"), "no meaning with --detach")):
            with self.subTest(args):
                rc, _, err = self.run_cli("actions", "add", "--device", "d", *args, "mounted",
                                          "--", "true")
                self.assertEqual(rc, 1)
                self.assertIn(text, err)


if __name__ == "__main__":
    unittest.main()
