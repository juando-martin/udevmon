# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`udevmon status` and the daemon's `results`: each action's last result."""

import contextlib
import io
import json
import unittest
from unittest import mock

from udevmon import cli, control, daemon
from udevmon.actions import Result

STATUS = {"pid": 42, "version": "9.9", "config": "/c/config.toml", "dry_run": False,
          "paused": False, "until": None, "devices": 3, "actions": 2}
RESULTS = [{"action": "keepass-up", "status": 1, "failure": None,
            "output": ["a", "b", "identity mismatch", "giving up"], "attempts": 2,
            "event": "mounted", "device": "key-daily", "time": 1794400000.0},
           {"action": "notify", "status": None, "failure": "timed out after 5 s",
            "output": [], "attempts": 1, "event": "connected", "device": None,
            "time": 1794300000.0}]


class DaemonResultsTests(unittest.TestCase):
    def test_the_last_result_of_each_action_newest_first(self):
        d = daemon.Daemon(None)
        d.record_result(Result("a", 1, time=10.0))
        d.record_result(Result("b", 0, time=20.0))
        d.record_result(Result("a", 0, time=30.0))              # replaces a's first one
        results, _ = d.control("results", {})
        self.assertEqual([(r["action"], r["status"]) for r in results["results"]],
                         [("a", 0), ("b", 0)])
        results, _ = d.control("results", {"action": "b"})
        self.assertEqual([r["action"] for r in results["results"]], ["b"])
        with self.assertRaises(control.BadParams):
            d.control("results", {"action": 3})


class FakeConnection:
    def __init__(self, error=None):
        self.error = error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    def call(self, method, params=None):
        if self.error:
            raise self.error
        return {"status": STATUS, "results": {"results": RESULTS}}[method]


class CliTests(unittest.TestCase):
    def run_cli(self, *args, running=(42, "/c/config.toml"), conn=None):
        out = io.StringIO()
        with mock.patch("udevmon.daemon.running_daemon", return_value=running), \
                mock.patch("udevmon.control.Connection", return_value=conn or FakeConnection()), \
                contextlib.redirect_stdout(out):
            rc = cli.main(["status", *args])
        return rc, out.getvalue()

    def test_running(self):
        rc, out = self.run_cli()
        self.assertEqual(rc, 0)
        self.assertIn("udevmon 9.9 is running (pid 42) with /c/config.toml: 3 device(s), "
                      "2 action(s).", out)
        self.assertRegex(out, r"keepass-up +\d{4}-\d\d-\d\d \d\d:\d\d:\d\d  mounted key-daily: "
                              r"exit status 1 after 2 attempts")
        self.assertIn("      b\n      identity mismatch\n      giving up", out)
        self.assertIn("notify", out)
        self.assertIn("connected: timed out after 5 s", out)

    def test_json(self):
        rc, out = self.run_cli("--json")
        data = json.loads(out)
        self.assertEqual((rc, data["exit_status"], data["running"], data["config"]),
                         (0, 0, True, "/c/config.toml"))
        self.assertEqual(data["results"], RESULTS)

    def test_not_running(self):
        rc, out = self.run_cli(running=None)
        self.assertEqual((rc, out), (cli.STATUS_NOT_RUNNING, "udevmon is not running.\n"))
        rc, out = self.run_cli("--json", running=None)
        self.assertEqual(json.loads(out), {"exit_status": 3, "running": False, "pid": None,
                                           "results": []})

    def test_an_older_daemon(self):
        rc, out = self.run_cli(conn=FakeConnection(control.ProtocolError("an older protocol")))
        self.assertEqual(rc, cli.STATUS_NO_ANSWER)
        self.assertIn("an older protocol", out)


if __name__ == "__main__":
    unittest.main()
