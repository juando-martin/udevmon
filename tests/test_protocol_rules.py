# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Control socket rules: reply size, timeouts, strict params, versioning, subscriptions."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from udevmon import control, daemon, pause
from udevmon import config as C
from udevmon.actions import Result
from udevmon.engine import Engine
from udevmon.events import CONNECTED, MOUNTED, Endpoint, Event


class LoopServer(unittest.TestCase):
    """A ControlServer on a real GLib main loop with the handlers in ``self.methods``."""

    def setUp(self):
        from gi.repository import GLib
        self.loop = GLib.MainLoop()
        self.ran = []

        def handler(method, params):
            self.ran.append(method)
            return self.methods[method](params)
        self.methods = {}
        self.server = control.ControlServer(handler)
        self.server.start()
        running = threading.Event()
        GLib.idle_add(lambda: running.set() or False)
        thread = threading.Thread(target=self.loop.run, daemon=True)
        thread.start()
        running.wait(5)
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.loop.quit)
        self.addCleanup(self.server.stop)


class ReplySizeTests(LoopServer):
    def test_a_reply_may_be_longer_than_a_request(self):
        big = "x" * (3 * control.MAX_REQUEST)
        self.methods["big"] = lambda p: ({"text": big}, None)
        self.assertEqual(len(control.call("big")["text"]), len(big))

    def test_a_request_may_not(self):
        with self.assertRaises((control.Refused, control.NotRunning, OSError)) as cm:
            control.call("big", {"text": "x" * (2 * control.MAX_REQUEST)})
        if isinstance(cm.exception, control.Refused):
            self.assertEqual(cm.exception.code, control.INVALID_REQUEST)
        self.assertEqual(self.ran, [])


class TimeoutTests(LoopServer):
    def test_a_request_that_started_may_still_take_effect(self):
        done = threading.Event()

        def slow(params):
            time.sleep(0.5)
            done.set()
            return {}, None
        self.methods["slow"] = slow
        with mock.patch.object(control, "HANDLE_TIMEOUT", 0.1):
            with self.assertRaises(control.Refused) as cm:
                control.call("slow")
        self.assertEqual(cm.exception.error, "still-running")
        self.assertTrue(done.wait(5))                   # it did complete

    def test_a_request_that_never_started_never_runs(self):
        release = threading.Event()
        self.methods["block"] = lambda p: (release.wait(5), ({}, None))[1]
        self.methods["late"] = lambda p: ({}, None)
        conn = control.Connection()               # accepted (on the loop) before it blocks
        self.addCleanup(conn.close)
        self.assertEqual(conn.call("late"), {})
        blocker = threading.Thread(target=control.call, args=("block",), daemon=True)
        blocker.start()
        self.assertTrue(_until(lambda: "block" in self.ran))
        with mock.patch.object(control, "HANDLE_TIMEOUT", 0.2):
            with self.assertRaises(control.Refused) as cm:
                conn.call("late")
        self.assertEqual(cm.exception.error, "not-run")
        release.set()
        blocker.join(5)
        time.sleep(0.2)
        self.assertEqual(self.ran, ["late", "block"])    # the second "late" never ran


def _until(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class StrictParamsTests(unittest.TestCase):
    def test_unknown_missing_and_wrong_types_are_refused(self):
        for method, params, says in (
                ("trigger", {"device": "k", "wiat": True}, "unknown parameter 'wiat'"),
                ("trigger", {}, "'device' is required"),
                ("trigger", {"device": 3}, "'device' must be a string"),
                ("trigger", {"device": "k", "wait": 1}, "'wait' must be true or false"),
                ("pause", {"seconds": True}, "'seconds' must be a number or null"),
                ("status", {"x": 1}, "it takes: none"),
                ("subscribe", {"kinds": "mounted"}, "'kinds' must be a list of strings"),
                ("hello", {"protocol": 1.0}, "'protocol' must be a whole number")):
            with self.subTest(method=method, params=params):
                with self.assertRaises(control.BadParams) as cm:
                    daemon.check_params(method, params)
                self.assertIn(says, cm.exception.message)
                self.assertEqual(cm.exception.code, control.INVALID_PARAMS)

    def test_what_fits_passes(self):
        daemon.check_params("trigger", {"device": "k", "event": None, "wait": False})
        daemon.check_params("pause", {"seconds": None})
        daemon.check_params("pause", {"seconds": 1.5})
        daemon.check_params("subscribe", {"notify": ["event"], "kinds": [], "system": False})

    def test_every_method_has_its_params_listed(self):
        self.assertEqual(set(daemon.PARAMS), set(daemon.Daemon.METHODS))


class Source:
    def __init__(self, *things):
        self.things = list(things)

    def present(self, **kw):
        return self.things


class DaemonBase(unittest.TestCase):
    def setUp(self):
        pause.resume()
        self.addCleanup(pause.resume)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "config.toml"
        path.write_text('[devices.key]\nmatch = { serial = "S" }\ntags = ["keys"]\n'
                        '[devices.other]\nmatch = { serial = "O" }\n'
                        '[[actions]]\nname = "up"\ndevice = "key"\non = ["connected"]\n'
                        'run = ["/bin/true"]\n')
        path.chmod(0o600)
        self.d = daemon.Daemon(path)
        self.d.cfg = C.load(path)
        self.d.runner = mock.Mock()
        self.d.engine = Engine(self.d.cfg, self.d.runner, paused=lambda: False,
                               on_handled=self.d._handled)
        self.key = Endpoint("udev", "/sys/key", {"serial": "S", "instance": "k"})
        self.other = Endpoint("udev", "/sys/other", {"serial": "O", "instance": "o"})
        self.d.sources = [Source(self.key)]

    def subscribe(self, **params):
        result, sub = self.d.control("subscribe", params)
        self.addCleanup(sub.close)
        return result, sub

    def drain(self, sub):
        out = []
        while not sub._queue.empty():
            out += sub._next() or []
        return out


class VersioningTests(DaemonBase):
    def test_hello_lists_the_methods_and_refuses_another_protocol(self):
        result, _ = self.d.control("hello", {"protocol": control.PROTOCOL})
        self.assertEqual(result["methods"], sorted(daemon.Daemon.METHODS))
        with self.assertRaises(control.Refused) as cm:
            self.d.control("hello", {"protocol": control.PROTOCOL + 1})
        self.assertEqual((cm.exception.error, cm.exception.data),
                         ("protocol-mismatch", {"protocol": control.PROTOCOL}))


class SubscribeTests(DaemonBase):
    def test_the_snapshot_and_the_numbers_line_up(self):
        self.d._emit(Event(CONNECTED, self.other))                 # seq 1, before
        result, sub = self.subscribe()
        self.assertEqual(result["seq"], 1)
        self.assertEqual([t["key"] for t in result["present"]], ["/sys/key"])
        self.d._emit(Event(CONNECTED, self.key))
        ((method, params),) = self.drain(sub)
        self.assertEqual((method, params["seq"]), ("event", 2))
        self.assertEqual(self.d.control("present", {})[0]["seq"], 2)

    def test_filters(self):
        _, by_kind = self.subscribe(kinds=["mounted"])
        _, by_device = self.subscribe(devices=["key"])
        _, by_tag = self.subscribe(tags=["keys"])
        _, everything = self.subscribe()
        self.d._emit(Event(CONNECTED, self.key))
        self.d._emit(Event(CONNECTED, self.other))
        self.assertEqual(self.drain(by_kind), [])
        self.assertEqual([p["key"] for _, p in self.drain(by_device)], ["/sys/key"])
        self.assertEqual([p["key"] for _, p in self.drain(by_tag)], ["/sys/key"])
        self.assertEqual(len(self.drain(everything)), 2)

    def test_handled_and_result_notifications(self):
        _, sub = self.subscribe(notify=["handled", "result"], devices=["key"])
        self.d._emit(Event(CONNECTED, self.key))
        ((method, handled),) = self.drain(sub)
        self.assertEqual(method, "handled")
        self.assertEqual((handled["seq"], handled["outcome"], handled["devices"],
                          handled["tags"], handled["trigger"]),
                         (1, "ran", ["key"], ["keys"], False))
        self.assertEqual(handled["actions"], [{"action": "up", "device": "key", "tag": None}])
        self.d.record_result(Result("up", 2, output=("nope",), seq=1))
        ((method, result),) = self.drain(sub)
        self.assertEqual((method, result["status"], result["output"], result["seq"]),
                         ("result", 2, ["nope"], 1))
        self.d.record_result(Result("up", 0, seq=99))               # another event's
        self.assertEqual(self.drain(sub), [])

    def test_what_the_engine_did_when_nothing_runs(self):
        _, sub = self.subscribe(notify=["handled"])
        self.d._emit(Event(MOUNTED, self.other))                     # no action for it
        self.d._emit(Event(CONNECTED, Endpoint("udev", "/sys/x", {"serial": "X"})))
        outcomes = [p["outcome"] for _, p in self.drain(sub)]
        self.assertEqual(outcomes, ["nothing-to-run", "unmatched"])
        self.assertEqual(self.d._interest, {})                       # nothing left to route

    def test_a_trigger_is_reported_but_not_announced_as_an_event(self):
        _, sub = self.subscribe(notify=["event", "handled"])
        self.d.control("trigger", {"device": "key", "event": "connected"})
        ((method, handled),) = self.drain(sub)
        self.assertEqual((method, handled["trigger"], handled["outcome"]),
                         ("handled", True, "ran"))

    def test_bad_values(self):
        for params, says in (({"notify": ["events"]}, "'notify' takes"),
                             ({"notify": []}, "'notify' takes"),
                             ({"kinds": ["plugged"]}, "not an event kind")):
            with self.subTest(params):
                with self.assertRaises(control.BadParams) as cm:
                    self.d.control("subscribe", params)
                self.assertIn(says, cm.exception.message)


class EngineOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.cfg = C.parse({"devices": {"key": {"match": {"serial": "S"}},
                                        "quiet": {"match": {"serial": "Q"}, "status": "ignored"}},
                            "actions": [{"name": "up", "device": "key", "on": ["connected"],
                                         "run": ["/bin/true"]}]})
        self.seen = []
        self.paused = False
        self.engine = Engine(self.cfg, mock.Mock(), paused=lambda: self.paused,
                             on_handled=self.seen.append)

    def outcome(self, serial, kind=CONNECTED):
        self.engine.handle(Event(kind, Endpoint("udev", f"/sys/{serial}", {"serial": serial})))
        return self.seen[-1].outcome

    def test_each_outcome(self):
        self.assertEqual(self.outcome("S"), "ran")
        self.assertEqual(self.outcome("S"), "debounced")
        self.assertEqual(self.outcome("S", MOUNTED), "nothing-to-run")
        self.assertEqual(self.outcome("Q"), "ignored")
        self.assertEqual(self.outcome("Z"), "unmatched")
        self.paused = True
        with self.assertLogs("udevmon.engine", "INFO"):
            self.assertEqual(self.outcome("S", "disconnected"), "paused")


if __name__ == "__main__":
    unittest.main()
