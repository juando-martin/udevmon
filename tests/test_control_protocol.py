# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""The control socket: JSON-RPC 2.0 end to end, the daemon's methods, and the CLI on them."""

import contextlib
import io
import json
import os
import socket
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli, control, daemon, pause
from udevmon import config as C
from udevmon.engine import Engine
from udevmon.events import CONNECTED, Endpoint, Event


def until(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class Server(unittest.TestCase):
    """A ControlServer on a real GLib main loop, with a handler the test controls."""

    def setUp(self):
        from gi.repository import GLib
        self.loop = GLib.MainLoop()
        self.calls = []
        self.subs = []

        def handler(method, params):
            self.calls.append((method, params))
            if method == "echo":
                return {"params": params}, None
            if method == "wait":
                return {"first": 1}, lambda: {"then": 2}
            if method == "refuse":
                raise control.Refused("nope", "refused here", problems=["a", "b"])
            if method == "bad":
                raise control.BadParams("'x' must be a number")
            if method == "crash":
                raise RuntimeError("boom")
            if method == "subscribe":
                closed = threading.Event()
                sub = control.Subscription(on_close=lambda s: closed.set())
                sub.closed_event = closed
                self.subs.append(sub)
                return {}, sub
            raise control.UnknownMethod(method)
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

    def raw(self, *lines, replies=1):
        """Send raw lines on one connection and return the decoded reply lines."""
        with socket.socket(socket.AF_UNIX) as s:
            s.settimeout(5)
            s.connect(str(self.server.path))
            s.sendall(b"".join(line + b"\n" for line in lines))
            got = b""
            while got.count(b"\n") < replies:
                chunk = s.recv(65536)
                if not chunk:
                    break
                got += chunk
        return [json.loads(x) for x in got.splitlines()]


class ProtocolTests(Server):
    def test_a_call_and_its_result(self):
        self.assertEqual(control.call("echo", {"a": 1}), {"params": {"a": 1}})
        self.assertEqual(self.calls, [("echo", {"a": 1})])

    def test_several_requests_on_one_connection(self):
        with control.Connection() as conn:
            self.assertEqual(conn.call("echo", {"n": 1}), {"params": {"n": 1}})
            self.assertEqual(conn.call("echo", {"n": 2}), {"params": {"n": 2}})
            self.assertEqual(conn.call("wait", timeout=None), {"first": 1, "then": 2})

    def test_a_refusal_keeps_its_code_and_data(self):
        with self.assertRaises(control.Refused) as cm:
            control.call("refuse")
        e = cm.exception
        self.assertEqual((e.error, e.message, e.code, e.data),
                         ("nope", "refused here", control.REFUSED, {"problems": ["a", "b"]}))

    def test_standard_errors(self):
        for method, code, error in (("frobnicate", control.METHOD_NOT_FOUND, "unknown-method"),
                                    ("bad", control.INVALID_PARAMS, "bad-request"),
                                    ("crash", control.INTERNAL_ERROR, "internal")):
            with self.subTest(method):
                with self.assertLogs("udevmon.control", "ERROR") if method == "crash" \
                        else contextlib.nullcontext():
                    with self.assertRaises(control.Refused) as cm:
                        control.call(method)
                self.assertEqual((cm.exception.code, cm.exception.error), (code, error))

    def test_parse_error_and_invalid_requests(self):
        (reply,) = self.raw(b"not json")
        self.assertEqual((reply["id"], reply["error"]["code"]), (None, control.PARSE_ERROR))
        (reply,) = self.raw(b'{"id": 4, "method": "echo"}')           # no "jsonrpc"
        self.assertEqual((reply["id"], reply["error"]["code"]), (4, control.INVALID_REQUEST))
        (reply,) = self.raw(b'{"jsonrpc": "2.0", "id": 5, "method": "echo", "params": [1]}')
        self.assertEqual(reply["error"]["code"], control.INVALID_PARAMS)
        (reply,) = self.raw(b"[]")
        self.assertEqual(reply["error"]["code"], control.INVALID_REQUEST)
        self.assertEqual(self.calls, [])

    def test_a_notification_gets_no_answer(self):
        replies = self.raw(b'{"jsonrpc": "2.0", "method": "echo", "params": {"n": 1}}',
                           b'{"jsonrpc": "2.0", "id": 9, "method": "echo"}')
        self.assertEqual(replies, [{"jsonrpc": "2.0", "id": 9, "result": {"params": {}}}])
        self.assertEqual([m for m, _ in self.calls], ["echo", "echo"])

    def test_a_batch_gets_an_array_without_the_notifications(self):
        batch = json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "echo", "params": {"n": 1}},
                            {"jsonrpc": "2.0", "method": "echo"},
                            {"jsonrpc": "2.0", "id": 2, "method": "frobnicate"}]).encode()
        (replies,) = self.raw(batch)
        self.assertEqual([r["id"] for r in replies], [1, 2])
        self.assertEqual(replies[0]["result"], {"params": {"n": 1}})
        self.assertEqual(replies[1]["error"]["code"], control.METHOD_NOT_FOUND)

    def test_subscribe_sends_notifications_and_ends_with_the_connection(self):
        conn = control.Connection()
        self.assertEqual(conn.call("subscribe"), {})
        self.assertTrue(until(lambda: self.subs))
        sub = self.subs[0]
        sub.publish("event", {"kind": "connected"})
        self.assertEqual(conn.notification(), ("event", {"kind": "connected"}))
        sub.publish("event", {"kind": "mounted"})
        self.assertEqual(conn.call("echo"), {"params": {}})          # calls still work
        self.assertEqual(conn.notification(), ("event", {"kind": "mounted"}))
        conn.close()
        self.assertTrue(sub.closed_event.wait(5))
        self.assertTrue(sub.closed)

    def test_stopping_the_server_ends_a_subscription(self):
        conn = control.Connection()
        self.addCleanup(conn.close)
        conn.call("subscribe")
        self.assertTrue(until(lambda: self.subs))
        self.server.stop()
        self.assertTrue(self.subs[0].closed_event.wait(5))
        with self.assertRaises((control.NotRunning, OSError)):
            conn.notification()

    def test_the_socket_is_private_and_removed_on_stop(self):
        path = self.server.path
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.server.stop()
        self.assertFalse(path.exists())
        with self.assertRaises(control.NotRunning):
            control.call("echo")

    def test_another_users_connection_is_refused(self):
        with mock.patch.object(control, "_peer_uid", return_value=os.getuid() + 1):
            (reply,) = self.raw(b'{"jsonrpc": "2.0", "id": 1, "method": "echo"}')
        self.assertIn("not your daemon", reply["error"]["message"])
        self.assertEqual(self.calls, [])

    def test_too_many_connections(self):
        with mock.patch.object(control, "MAX_CONNECTIONS", 1):
            first = control.Connection()
            self.addCleanup(first.close)
            first.call("echo")
            with self.assertRaises(control.Refused) as cm:
                control.call("echo")
            self.assertEqual(cm.exception.error, "busy")


class SubscriptionTests(unittest.TestCase):
    def test_a_full_queue_counts_the_lost_events_and_says_so_first(self):
        with mock.patch.object(control, "QUEUE_SIZE", 3):
            sub = control.Subscription()
        for n in range(5):
            sub.publish("event", {"n": n})
        self.assertEqual(sub._next(), [("lost", {"count": 2}), ("event", {"n": 0})])
        self.assertEqual(sub._next(), [("event", {"n": 1})])
        sub.close()
        self.assertIsNone(sub._next())                # closed: nothing more is sent
        sub.publish("event", {"n": 9})                # and nothing more is queued
        self.assertTrue(sub.closed)


class OlderDaemonTests(unittest.TestCase):
    def test_an_answer_in_the_old_protocol_says_to_restart(self):
        with tempfile.TemporaryDirectory(dir="/tmp", prefix="udm-") as tmp:
            path = Path(tmp) / "s"
            server = socket.socket(socket.AF_UNIX)
            server.bind(str(path))
            server.listen(1)

            def answer():
                conn, _ = server.accept()
                with conn:
                    conn.recv(4096)
                    conn.sendall(b'{"ok": false, "error": "bad-request", "message": "x"}\n')
            t = threading.Thread(target=answer)
            t.start()
            with self.assertRaises(control.ProtocolError) as cm:
                control.call("hello", path=path)
            t.join(5)
            server.close()
        self.assertIn("older protocol", str(cm.exception))


class Source:
    def __init__(self, *things):
        self.things = list(things)

    def present(self, **kw):
        return self.things


class DaemonMethodTests(unittest.TestCase):
    def setUp(self):
        pause.resume()
        self.addCleanup(pause.resume)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg_path = Path(tmp.name) / "config.toml"
        self.cfg_path.write_text('[devices.key]\nmatch = { serial = "S" }\n')
        self.cfg_path.chmod(0o600)
        self.d = daemon.Daemon(self.cfg_path)
        self.d.cfg = C.load(self.cfg_path)
        self.d.runner = mock.Mock()
        self.d.engine = Engine(self.d.cfg, self.d.runner, paused=lambda: False)
        self.usb = Endpoint("udev", "/sys/k", {"serial": "S", "instance": "k"})
        self.d.sources = [Source(self.usb)]

    def call(self, method, **params):
        return self.d.control(method, params)

    def test_hello(self):
        result, _ = self.call("hello")
        self.assertEqual(result["protocol"], control.PROTOCOL)
        self.assertEqual(result["features"], list(cli.FEATURES))
        self.assertIn("version", result)

    def test_status(self):
        result, _ = self.call("status")
        self.assertEqual((result["config"], result["devices"], result["paused"]),
                         (os.path.realpath(self.cfg_path), 1, False))

    def test_present(self):
        result, _ = self.call("present")
        self.assertEqual(result["things"], [{"source": "udev", "key": "/sys/k",
                                             "props": {"serial": "S", "instance": "k"}}])
        with self.assertRaises(control.BadParams):
            self.call("present", system="no")

    def test_pause_and_resume_use_the_flag_file(self):
        with self.assertLogs("udevmon.daemon", "INFO"):
            result, _ = self.call("pause", seconds=60)
        self.assertEqual((result["paused"], result["was_paused"]), (True, False))
        self.assertTrue(pause.is_paused())
        self.assertAlmostEqual(result["until"], pause.status()[1], places=2)
        with self.assertLogs("udevmon.daemon", "INFO"):
            self.assertEqual(self.call("resume")[0], {"was_paused": True})
        self.assertFalse(pause.is_paused())
        self.assertEqual(self.call("resume")[0], {"was_paused": False})
        for bad in (0, -1, "1h", True):
            with self.subTest(bad):
                with self.assertRaises(control.BadParams):
                    self.call("pause", seconds=bad)

    def test_reload_answers_with_counts_and_refuses_a_bad_config(self):
        self.cfg_path.write_text('[devices.key]\nmatch = { serial = "S" }\n'
                                 '[devices.two]\nmatch = { serial = "T" }\n')
        with self.assertLogs("udevmon.daemon", "INFO"):
            result, _ = self.call("reload", config=str(self.cfg_path))
        self.assertEqual((result["devices"], result["restart_needed"]), (2, False))
        self.cfg_path.write_text("[devices.key]\nmatch = 3\n")
        with self.assertLogs("udevmon.daemon", "ERROR"):
            with self.assertRaises(control.Refused) as cm:
                self.call("reload")
        self.assertEqual(cm.exception.error, "bad-config")
        self.assertTrue(cm.exception.data["problems"])
        self.assertEqual(len(self.d.cfg.devices), 2)                 # the old one is kept

    def test_reload_refuses_another_config(self):
        with self.assertRaises(control.Refused) as cm:
            self.call("reload", config="/elsewhere/config.toml")
        self.assertEqual(cm.exception.error, "other-config")

    def test_subscribers_get_every_event_before_the_engine(self):
        result, sub = self.call("subscribe")
        self.assertIsInstance(sub, control.Subscription)
        self.assertEqual(result["seq"], 0)
        event = Event(CONNECTED, self.usb, time=123.0)
        self.d._emit(event)
        self.assertEqual(sub._next(), [("event", {"seq": 1, "kind": "connected", "time": 123.0,
                                                  "source": "udev", "key": "/sys/k",
                                                  "props": {"serial": "S", "instance": "k"}})])
        sub.close()
        self.assertEqual(self.d._subscribers, set())

    def test_unknown_method(self):
        with self.assertRaises(control.UnknownMethod):
            self.call("frobnicate")


class CliTests(unittest.TestCase):
    def run_cli(self, *args, running=123, result=None, error=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.running_daemon",
                        return_value=(running, None) if running else None), \
                mock.patch("udevmon.control.call", return_value=result,
                           side_effect=error) as call, \
                mock.patch("os.kill") as kill, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(args))
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue(), call, kill

    def test_reload_reports_what_the_daemon_loaded(self):
        rc, out, err, call, kill = self.run_cli(
            "reload", result={"devices": 3, "actions": 2, "warnings": ["w1"],
                              "restart_needed": True})
        self.assertEqual(rc, 0)
        self.assertIn("Reloaded the running daemon: 3 device(s), 2 action(s).", out)
        self.assertIn("restart the daemon", out)
        self.assertIn("warning: w1", err)
        self.assertEqual(call.call_args.args[0], "reload")
        kill.assert_not_called()

    def test_reload_fails_on_a_bad_config(self):
        rc, _, err, _, kill = self.run_cli(
            "reload", error=control.Refused("bad-config", "the configuration has errors",
                                            problems=["[devices.x]: bad"]))
        self.assertEqual(rc, 1)
        self.assertIn("the configuration has errors", err)
        self.assertIn("[devices.x]: bad", err)
        kill.assert_not_called()

    def test_reload_falls_back_to_a_signal(self):
        for error in (control.NotRunning("no socket"), control.ProtocolError("older")):
            with self.subTest(error):
                rc, out, _, _, kill = self.run_cli("reload", error=error)
                self.assertEqual(rc, 0)
                self.assertIn("Sent reload", out)
                kill.assert_called_once()

    def test_reload_with_no_daemon(self):
        rc, _, err, call, _ = self.run_cli("reload", running=None)
        self.assertEqual(rc, 1)
        self.assertIn("not running", err)
        call.assert_not_called()

    def test_pause_and_resume_go_through_the_daemon(self):
        with mock.patch("udevmon.daemon.running_pid", return_value=123):
            rc, out, _, call, _ = self.run_cli(
                "pause", "--for", "1h", result={"paused": True, "until": None, "was_paused": False})
            self.assertEqual(call.call_args.args, ("pause", {"seconds": 3600.0}))
            self.assertIn("Automation paused", out)
            self.assertFalse(pause.is_paused())          # the (mocked) daemon writes the file
            rc, out, _, call, _ = self.run_cli("resume", result={"was_paused": True})
            self.assertEqual(call.call_args.args[0], "resume")
            self.assertIn("Automation resumed.", out)

    def test_pause_writes_the_file_itself_when_the_daemon_does_not_answer(self):
        self.addCleanup(pause.resume)
        with mock.patch("udevmon.daemon.running_pid", return_value=123):
            rc, out, _, _, _ = self.run_cli("pause", error=control.ProtocolError("older"))
        self.assertEqual(rc, 0)
        self.assertTrue(pause.is_paused())


class FakeConnection:
    """Stands in for control.Connection: answers calls and hands out notifications."""

    def __init__(self, config, notifications=(), present=()):
        self.config, self.notes, self.things = config, list(notifications), list(present)
        self.calls, self.closed = [], False
        self.sock = mock.Mock()

    def call(self, method, params=None):
        self.calls.append(method)
        return {"status": {"config": self.config},
                "subscribe": {"seq": 0, "present": self.things}}[method]

    def has_buffered(self):
        return bool(self.notes)

    def notification(self):
        if not self.notes:
            raise control.NotRunning("closed")
        return self.notes.pop(0)

    def close(self):
        self.closed = True


class ScanThroughDaemonTests(unittest.TestCase):
    def setUp(self):
        from udevmon import scan
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Path(tmp.name) / "config.toml"
        self.cfg.write_text("")
        self.cfg.chmod(0o600)
        self.scanner = scan.Scanner(self.cfg, prompt=False, present=True)
        self.scanner.loop = mock.Mock()
        self.thing = {"source": "udev", "key": "/sys/k",
                      "props": {"serial": "S", "vendor_id": "1", "product_id": "2"}}

    def through(self, conn, running=123):
        with mock.patch("udevmon.daemon.running_pid", return_value=running), \
                mock.patch("udevmon.control.Connection", return_value=conn):
            return self.scanner._through_daemon()

    def test_what_is_present_comes_with_the_subscription(self):
        conn = FakeConnection(str(self.cfg), present=[self.thing])
        got, present = self.through(conn)
        self.assertIs(got, conn)
        self.assertEqual(conn.calls, ["status", "subscribe"])
        self.assertEqual(present, [self.thing])

    def test_not_through_a_daemon_with_another_config_or_none(self):
        conn = FakeConnection("/elsewhere/config.toml")
        self.assertEqual(self.through(conn), (None, []))
        self.assertTrue(conn.closed)
        self.assertEqual(self.through(FakeConnection(str(self.cfg)), running=None), (None, []))
        broken = FakeConnection(str(self.cfg))
        broken.call = mock.Mock(side_effect=control.ProtocolError("older"))
        self.assertEqual(self.through(broken), (None, []))

    def test_events_and_lost_notices_are_shown_and_a_stop_ends_the_scan(self):
        conn = FakeConnection(str(self.cfg), notifications=[
            ("event", {"kind": "connected", "time": 0.0, **self.thing}),
            ("lost", {"count": 4})])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertTrue(self.scanner._drain(conn))          # what is buffered
            self.assertFalse(self.scanner._drain(conn, read=True))   # the daemon is gone
        text = out.getvalue()
        self.assertIn("connected", text)
        self.assertIn("4 event(s) not shown", text)
        self.assertIn("The daemon stopped", text)
        self.scanner.loop.quit.assert_called_once()
        self.assertIn("udev:/sys/k", self.scanner.pending)


if __name__ == "__main__":
    unittest.main()
