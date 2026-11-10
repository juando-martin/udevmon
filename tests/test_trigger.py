# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`udevmon trigger DEVICE [EVENT]`: run a present device's actions now, through the daemon."""

import contextlib
import io
import threading
import unittest
from concurrent.futures import Future
from unittest import mock

from udevmon import cli, control, daemon, pause
from udevmon import config as C
from udevmon.actions import ActionRunner
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
        reply, wait = self.d.control({"cmd": "trigger", "device": device, "event": event,
                                      **extra})
        return reply, wait


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
        cases = [("nope", None, "no device 'nope'"),
                 ("gone", None, "not present"),
                 ("quiet", None, "ignored"),
                 ("key", "disconnected", "cannot trigger"),
                 ("key", "removed", "cannot trigger"),
                 ("key", "connected", "needs a USB device"),
                 ("yubi", "mounted", "needs a filesystem that is mounted")]
        for device, event, says in cases:
            with self.subTest(device=device, event=event):
                reply, _ = self.ask(device, event)
                self.assertFalse(reply["ok"])
                self.assertIn(says, reply["message"])
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
        self.d.runner.submit.assert_not_called()

    def test_usb_device(self):
        self.assertEqual(self.ask("yubi", "connected")[0]["things"][0]["actions"], ["plug"])

    def test_wait_reports_the_exit_statuses(self):
        future = Future()
        future.set_result([("up", 0), ("tagged", 3)])
        self.d.runner.submit.return_value = future
        reply, wait = self.ask("key", wait=True)
        self.assertEqual(wait(), {"statuses": [["up", 0], ["tagged", 3]]})

    def test_an_unknown_request(self):
        reply, _ = self.d.control({"cmd": "frobnicate"})
        self.assertFalse(reply["ok"])


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
            self.assertEqual(runner.submit(event, jobs).result(5), [("ok", 0), ("bad", 3)])


class SocketTests(unittest.TestCase):
    """The control socket, end to end, on a real GLib main loop."""

    def setUp(self):
        from gi.repository import GLib
        self.loop = GLib.MainLoop()
        self.requests = []

        def handler(req):
            self.requests.append(req)
            if req.get("wait"):
                return {"ok": True}, lambda: {"statuses": [["a", 0]]}
            return {"ok": True, "echo": req.get("device")}, None
        self.server = control.ControlServer(handler)
        self.server.start()
        running = threading.Event()
        GLib.idle_add(lambda: running.set() or False)
        thread = threading.Thread(target=self.loop.run, daemon=True)
        thread.start()
        running.wait(5)                  # so that quit() below never comes before run()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.loop.quit)
        self.addCleanup(self.server.stop)

    def test_a_request_and_its_answer(self):
        self.assertEqual(control.request({"cmd": "trigger", "device": "key"}),
                         {"ok": True, "echo": "key"})
        self.assertEqual(control.request({"wait": True}, timeout=None),
                         {"ok": True, "statuses": [["a", 0]]})

    def test_the_socket_is_private_and_removed_on_stop(self):
        import os
        import stat
        path = self.server.path
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.server.stop()
        self.assertFalse(path.exists())
        with self.assertRaises(control.NotRunning):
            control.request({"cmd": "trigger"})

    def test_garbage_gets_an_error(self):
        import socket
        with socket.socket(socket.AF_UNIX) as s:
            s.connect(str(self.server.path))
            s.sendall(b"not json\n")
            self.assertIn(b"bad request", s.recv(4096))
        self.assertEqual(self.requests, [])


class CliTests(unittest.TestCase):
    def run_cli(self, *args, running=(123, None), reply=None, error=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.running_daemon", return_value=running), \
                mock.patch("udevmon.control.request", return_value=reply,
                           side_effect=error) as request, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main([*args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue(), request

    def test_not_running(self):
        rc, _, err, request = self.run_cli("trigger", "key", running=None)
        self.assertEqual(rc, 1)
        self.assertIn("not running", err)
        request.assert_not_called()
        rc, _, err, _ = self.run_cli("trigger", "key", error=control.NotRunning("x"))
        self.assertIn("not running", err)

    def test_a_daemon_with_another_config(self):
        rc, _, err, _ = self.run_cli("--config", "/x/other.toml", "trigger", "key",
                                     running=(123, "/x/config.toml"))
        self.assertEqual(rc, 1)
        self.assertIn("uses /x/config.toml", err)

    def test_output(self):
        reply = {"ok": True, "event": "startup", "device": "key",
                 "things": [{"thing": "Filesystem KEY", "actions": ["up"],
                             "skipped": [["x", "the action is disabled"]]}]}
        rc, out, _, request = self.run_cli("trigger", "key", reply=reply)
        self.assertEqual(rc, 0)
        self.assertIn("startup for key (Filesystem KEY): up", out)
        self.assertIn("skipped x: the action is disabled", out)
        self.assertEqual(request.call_args.args[0],
                         {"cmd": "trigger", "device": "key", "event": "startup", "wait": False})

    def test_wait_fails_on_a_failed_action(self):
        reply = {"ok": True, "event": "mounted", "device": "key",
                 "things": [{"thing": "t", "actions": ["up"], "skipped": []}],
                 "statuses": [["up", 2]]}
        rc, out, _, _ = self.run_cli("trigger", "key", "mounted", "--wait", reply=reply)
        self.assertEqual(rc, 1)
        self.assertIn("up: exit status 2", out)

    def test_an_error_reply(self):
        rc, _, err, _ = self.run_cli("trigger", "key",
                                     reply={"ok": False, "message": "device 'key' is not present"})
        self.assertEqual(rc, 1)
        self.assertIn("not present", err)

    def test_only_events_that_make_sense(self):
        self.assertEqual(self.run_cli("trigger", "key", "removed")[0], 2)


if __name__ == "__main__":
    unittest.main()
