# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`udevmon trigger DEVICE [EVENT]`: run a present device's actions now, through the daemon."""

import contextlib
import io
import json
import unittest
from concurrent.futures import Future
from unittest import mock

from udevmon import cli, control, daemon, pause
from udevmon import config as C
from udevmon.actions import ActionRunner, Result
from udevmon.engine import Engine
from udevmon.events import Endpoint, Event

CONFIG = {
    "devices": {"key": {"match": {"fs_uuid": "U"}, "tags": ["keepass"]},
                "off": {"match": {"fs_uuid": "OFF"}, "status": "disabled", "tags": ["keepass"]},
                "gone": {"match": {"fs_uuid": "GONE"}},
                "quiet": {"match": {"fs_uuid": "Q"}, "status": "ignored"},
                "yubi": {"match": {"serial": "Y1"}}},
    "actions": [
        {"name": "up", "device": ["key", "off"], "on": ["startup", "mounted"],
         "run": ["/bin/true", "{mountpoint}"]},
        {"name": "tagged", "tag": "keepass", "on": ["startup"], "run": ["/bin/true"]},
        {"name": "down", "device": "key", "on": ["unmounted"], "run": ["/bin/true", "{surprise}"]},
        {"name": "plug", "device": "yubi", "on": ["connected"], "run": ["/bin/true"]},
    ]}


def fs(uuid, mountpoint="/media/u/KEY"):
    props = {"fs_uuid": uuid, "instance": "i-" + uuid}
    if mountpoint:
        props["mountpoint"] = mountpoint
    return Endpoint("udisks", "/org/" + uuid, props)


class Source:
    def __init__(self, *things):
        self.things = list(things)

    def present(self):
        return self.things


class Base(unittest.TestCase):
    def setUp(self):
        pause.resume()
        self.addCleanup(pause.resume)
        self.d = daemon.Daemon(None)
        self.d.cfg = C.parse(CONFIG)
        self.d.runner = mock.Mock()
        self.d.runner.submit.return_value = Future()
        self.d.engine = Engine(self.d.cfg, self.d.runner, paused=lambda: False)
        self.d.sources = [Source(fs("U"), fs("OFF"), fs("Q")),
                          Source(Endpoint("udev", "/sys/y", {"serial": "Y1", "instance": "y"}))]

    def ask(self, device, event=None, **extra):
        """The trigger request's answer, as {"ok", ...result} or {"ok", "error", "message"}."""
        try:
            result, wait = self.d.control("trigger", {"device": device, "event": event, **extra})
        except control.Refused as e:
            return {"ok": False, "error": e.error, "message": e.message}, None
        return {"ok": True, **result}, wait


class ControlTests(Base):
    def test_startup_is_the_default_and_runs_like_a_real_event(self):
        with self.assertLogs("udevmon.engine", "INFO") as logs:
            reply, wait = self.ask("key")
        self.assertTrue(reply["ok"], reply)
        self.assertIsNone(wait)
        self.assertEqual(reply["things"][0]["actions"], ["up", "tagged"])
        event, jobs = self.d.runner.submit.call_args.args
        self.assertEqual((event.kind, event.props["mountpoint"]), ("startup", "/media/u/KEY"))
        self.assertEqual(len(self.d.runner.submit.call_args.args), 2)     # ends nothing
        self.assertTrue(any("udevmon trigger" in line for line in logs.output))

    def test_never_dropped_as_a_repeat(self):
        for _ in range(3):
            self.assertEqual(self.ask("key", "mounted")[0]["things"][0]["actions"], ["up"])

    def test_disabled_device_skips_its_own_actions_only(self):
        reply, _ = self.ask("off")
        thing = reply["things"][0]
        self.assertEqual(thing["actions"], ["tagged"])
        self.assertIn("disabled", dict(thing["skipped"])["up"])

    def test_refusals(self):
        cases = [("nope", None, "no device 'nope'", "unknown-device"),
                 ("gone", None, "not present", "not-present"),
                 ("quiet", None, "ignored", "ignored"),
                 ("key", "disconnected", "cannot trigger", "bad-request"),
                 ("key", "removed", "cannot trigger", "bad-request"),
                 ("key", "connected", "needs a USB device", "wrong-event"),
                 ("yubi", "mounted", "needs a filesystem that is mounted", "wrong-event")]
        for device, event, says, error in cases:
            with self.subTest(device=device, event=event):
                reply, _ = self.ask(device, event)
                self.assertFalse(reply["ok"])
                self.assertIn(says, reply["message"])
                self.assertEqual(reply["error"], error)
        self.d.runner.submit.assert_not_called()

    def test_mounted_needs_a_mount(self):
        self.d.sources[0].things[0] = fs("U", mountpoint=None)
        self.assertIn("mounted", self.ask("key", "mounted")[0]["message"])
        self.assertTrue(self.ask("key", "unmounted")[0]["ok"])

    def test_unmounted_is_a_clean_one(self):
        self.ask("key", "unmounted")
        event = self.d.runner.submit.call_args.args[0]
        self.assertEqual(event.props["surprise"], "0")

    def test_paused(self):
        pause.pause()
        reply, _ = self.ask("key")
        self.assertIn("paused", reply["message"])
        self.assertEqual(reply["error"], "paused")
        self.d.runner.submit.assert_not_called()

    def test_usb_device(self):
        self.assertEqual(self.ask("yubi", "connected")[0]["things"][0]["actions"], ["plug"])

    def test_wait_reports_the_exit_statuses(self):
        future = Future()
        future.set_result([Result("up", 0, event="startup", device="key", time=5.0),
                           Result("tagged", 3, output=("no profile 'x'",), attempts=2)])
        self.d.runner.submit.return_value = future
        reply, wait = self.ask("key", wait=True)
        statuses = wait()["statuses"]
        self.assertEqual(statuses[0], {"action": "up", "status": 0, "failure": None,
                                       "output": [], "attempts": 1, "event": "startup",
                                       "device": "key", "time": 5.0, "seq": 0})
        self.assertEqual((statuses[1]["status"], statuses[1]["output"]), (3, ["no profile 'x'"]))

    def test_an_unknown_method(self):
        with self.assertRaises(control.UnknownMethod) as cm:
            self.d.control("frobnicate", {})
        self.assertEqual((cm.exception.error, cm.exception.code),
                         ("unknown-method", control.METHOD_NOT_FOUND))


class RunnerTests(unittest.TestCase):
    def test_submit_reports_each_status(self):
        cfg = C.parse({"devices": {"d": {"match": {"fs_uuid": "U"}}},
                       "actions": [{"name": "ok", "device": "d", "on": ["startup"],
                                    "run": ["/bin/sh", "-c", "exit 0"]},
                                   {"name": "bad", "device": "d", "on": ["startup"],
                                    "run": ["/bin/sh", "-c", "exit 3"]}]})
        runner = ActionRunner(1)
        self.addCleanup(runner.shutdown, True)
        engine = Engine(cfg, runner, paused=lambda: False)
        event = Event("startup", fs("U"))
        jobs, _ = engine.trigger(event)
        with self.assertLogs("udevmon.actions", "INFO"):
            self.assertEqual([r[:2] for r in runner.submit(event, jobs).result(5)],
                             [("ok", 0), ("bad", 3)])


class CliTests(unittest.TestCase):
    def run_cli(self, *args, running=(123, None), reply=None, error=None):
        """Run the CLI with ``control.call`` answering ``reply`` (a refusal when its ``ok``
        is false) or raising ``error``."""
        out, err = io.StringIO(), io.StringIO()
        if reply is not None and not reply.get("ok", True):
            error, reply = control.Refused(reply["error"], reply["message"]), None
        elif reply is not None:
            reply = {k: v for k, v in reply.items() if k != "ok"}
        with mock.patch("udevmon.daemon.running_daemon", return_value=running), \
                mock.patch("udevmon.control.call", return_value=reply,
                           side_effect=error) as request, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main([*args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue(), request

    def test_not_running(self):
        rc, _, err, request = self.run_cli("trigger", "key", running=None)
        self.assertEqual(rc, 3)
        self.assertIn("not running", err)
        request.assert_not_called()
        rc, _, err, _ = self.run_cli("trigger", "key", error=control.NotRunning("x"))
        self.assertEqual(rc, 3)
        self.assertIn("not running", err)

    def test_no_usable_answer(self):
        rc, _, err, _ = self.run_cli("trigger", "key", error=ValueError("bad JSON"))
        self.assertEqual(rc, 7)
        self.assertIn("no usable answer", err)
        rc, _, err, _ = self.run_cli("trigger", "key",
                                     error=control.ProtocolError("an older protocol"))
        self.assertEqual(rc, 7)
        self.assertIn("an older protocol", err)

    def test_a_daemon_with_another_config(self):
        rc, _, err, _ = self.run_cli("--config", "/x/other.toml", "trigger", "key",
                                     running=(123, "/x/config.toml"))
        self.assertEqual(rc, 5)
        self.assertIn("uses /x/config.toml", err)

    def test_output(self):
        reply = {"ok": True, "event": "startup", "device": "key",
                 "things": [{"thing": "Filesystem KEY", "actions": ["up"],
                             "skipped": [["x", "the action is disabled"]]}]}
        rc, out, _, request = self.run_cli("trigger", "key", reply=reply)
        self.assertEqual(rc, 0)
        self.assertIn("startup for key (Filesystem KEY): up", out)
        self.assertIn("skipped x: the action is disabled", out)
        self.assertEqual(request.call_args.args,
                         ("trigger", {"device": "key", "event": "startup", "wait": False}))

    def test_wait_fails_on_a_failed_action(self):
        reply = {"ok": True, "event": "mounted", "device": "key",
                 "things": [{"thing": "t", "actions": ["up"], "skipped": []}],
                 "statuses": [{"action": "up", "status": 2, "failure": None,
                               "output": ["identity mismatch", "giving up"]}]}
        rc, out, _, _ = self.run_cli("trigger", "key", "mounted", "--wait", reply=reply)
        self.assertEqual(rc, 1)
        self.assertIn("up: exit status 2", out)
        self.assertIn("its last output:\n      identity mismatch\n      giving up", out)

    def test_a_timeout_says_so(self):
        reply = {"ok": True, "event": "mounted", "device": "key",
                 "things": [{"thing": "t", "actions": ["up"], "skipped": []}],
                 "statuses": [{"action": "up", "status": None,
                               "failure": "timed out after 5 s", "output": ["half"]}]}
        rc, out, _, _ = self.run_cli("trigger", "key", "mounted", "--wait", reply=reply)
        self.assertEqual(rc, 1)
        self.assertIn("up: no exit status (timed out after 5 s)", out)
        self.assertIn("      half", out)

    def test_an_error_reply(self):
        rc, _, err, _ = self.run_cli("trigger", "key",
                                     reply={"ok": False, "error": "not-present",
                                            "message": "device 'key' is not present"})
        self.assertEqual(rc, 6)
        self.assertIn("not present", err)

    def test_an_unknown_device_has_its_own_status(self):
        rc, _, err, _ = self.run_cli("trigger", "nope",
                                     reply={"ok": False, "error": "unknown-device",
                                            "message": "no device 'nope'"})
        self.assertEqual(rc, 4)

    def test_json(self):
        reply = {"ok": True, "event": "mounted", "device": "key",
                 "things": [{"thing": "t", "actions": ["up", "x"],
                             "skipped": [["y", "the action is disabled"]]}],
                 "statuses": [{"action": "up", "status": 0, "output": ["fine"]},
                              {"action": "x", "status": None, "failure": "detached"}]}
        rc, out, _, _ = self.run_cli("trigger", "key", "mounted", "--wait", "--json",
                                     reply=reply)
        data = json.loads(out)
        self.assertEqual(rc, 1)                       # no status counts as a failure
        self.assertEqual(data["exit_status"], 1)
        self.assertEqual(data["statuses"], [
            {"action": "up", "status": 0, "failure": None, "output": ["fine"]},
            {"action": "x", "status": None, "failure": "detached", "output": []}])
        self.assertEqual(data["things"][0]["skipped"],
                         [{"action": "y", "reason": "the action is disabled"}])
        rc, out, _, _ = self.run_cli("trigger", "key", "--json", reply={**reply, "statuses": []})
        self.assertEqual((rc, json.loads(out)["statuses"]), (0, None))

    def test_json_errors(self):
        rc, out, err, _ = self.run_cli("trigger", "key", "--json", running=None)
        self.assertEqual((rc, err), (3, ""))
        self.assertEqual({k: v for k, v in json.loads(out).items() if k != "message"},
                         {"exit_status": 3, "ok": False, "error": "not-running"})
        rc, out, _, _ = self.run_cli("trigger", "key", "--json",
                                     reply={"ok": False, "error": "paused", "message": "m"})
        self.assertEqual((rc, json.loads(out)["error"]), (6, "paused"))

    def test_only_events_that_make_sense(self):
        self.assertEqual(self.run_cli("trigger", "key", "removed")[0], 2)


if __name__ == "__main__":
    unittest.main()
