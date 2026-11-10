# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`lock = "name"`: actions sharing a lock never run at the same time, in event order."""

import contextlib
import io
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from udevmon import actions as A
from udevmon import cli, completion, explain
from udevmon import config as C
from udevmon import configedit as E
from udevmon.engine import Job
from udevmon.events import MOUNTED, Endpoint, Event

EVENT = Event(MOUNTED, Endpoint("udisks", "k", {"mountpoint": "/m"}))


def job(name, lock=None, **kw):
    return Job(C.Action(name, ("d",), (), ("mounted",), ("true",), 10, lock=lock, **kw), "d", None)


class Recorder:
    """A stand-in for run_action that records what ran, and can hold a job at a gate."""

    def __init__(self):
        self.log = []
        self.mutex = threading.Lock()
        self.running = self.peak = 0
        self.gates = {}                         # job name -> threading.Event to wait for
        self.raises = {}                        # job name -> exception to raise
        self.started = {}                       # job name -> threading.Event, set when it starts

    def gate(self, name):
        self.gates[name] = threading.Event()
        return self.gates[name]

    def started_event(self, name):
        self.started[name] = threading.Event()
        return self.started[name]

    def __call__(self, action, event, device, tag=None, **kwargs):
        with self.mutex:
            self.log.append(("start", action.name))
            self.running += 1
            self.peak = max(self.peak, self.running)
        if action.name in self.started:
            self.started[action.name].set()
        try:
            if action.name in self.gates:
                assert self.gates[action.name].wait(10), f"{action.name} was never released"
            if action.name in self.raises:
                raise self.raises[action.name]
        finally:
            with self.mutex:
                self.running -= 1
                self.log.append(("end", action.name))
        return 0

    def starts(self):
        return [n for kind, n in self.log if kind == "start"]


def until(predicate, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class Base(unittest.TestCase):
    def setUp(self):
        self.rec = Recorder()
        patcher = mock.patch.object(A, "run_action", self.rec)
        patcher.start()
        self.addCleanup(patcher.stop)

    def runner(self, workers=4):
        r = A.ActionRunner(workers)
        self.addCleanup(r.shutdown, True)
        return r


class FifoLockTests(unittest.TestCase):
    def test_turns_come_in_ticket_order_whatever_order_the_waiters_arrive(self):
        lock = A.FifoLock("x")
        tickets = [lock.take() for _ in range(4)]
        order, threads = [], []

        def worker(t):
            lock.wait(t)
            order.append(t)
            lock.release()
        for t in reversed(tickets[1:]):                           # the last ticket arrives first
            threads.append(threading.Thread(target=worker, args=(t,)))
            threads[-1].start()
            time.sleep(0.02)
        self.assertEqual(order, [])                               # ticket 0 (us) still has it
        lock.release()                                            # ... and now it is done
        for th in threads:
            th.join(5)
        self.assertEqual(order, [1, 2, 3])

    def test_wait_says_whether_it_had_to_wait(self):
        lock = A.FifoLock("x")
        a, b = lock.take(), lock.take()
        self.assertFalse(lock.wait(a))
        threading.Timer(0.1, lock.release).start()
        self.assertTrue(lock.wait(b))
        lock.release()


class RunnerTests(Base):
    def test_a_later_event_waits_for_the_earlier_one(self):
        gate = self.rec.gate("plug")
        seen = self.rec.started_event("plug")
        r = self.runner()
        r.submit(EVENT, [job("plug", "yk")])
        self.assertTrue(seen.wait(5))
        r.submit(EVENT, [job("unplug", "yk")])
        time.sleep(0.2)
        self.assertEqual(self.rec.starts(), ["plug"])             # the second must not start
        gate.set()
        self.assertTrue(until(lambda: self.rec.starts() == ["plug", "unplug"]))
        self.assertEqual(self.rec.log[:3], [("start", "plug"), ("end", "plug"),
                                            ("start", "unplug")])

    def test_order_and_exclusion_hold_under_contention(self):
        first = self.rec.gate("j0")
        seen = self.rec.started_event("j0")
        r = self.runner(4)
        r.submit(EVENT, [job("j0", "yk")])
        seen.wait(5)
        for i in range(1, 7):                                     # more waiters than workers
            r.submit(EVENT, [job(f"j{i}", "yk")])
        time.sleep(0.1)
        first.set()
        self.assertTrue(until(lambda: len(self.rec.starts()) == 7))
        self.assertEqual(self.rec.starts(), [f"j{i}" for i in range(7)])
        self.assertEqual(self.rec.peak, 1)

    def test_different_names_run_in_parallel(self):
        ga, gb = self.rec.gate("a"), self.rec.gate("b")
        r = self.runner()
        r.submit(EVENT, [job("a", "one")])
        r.submit(EVENT, [job("b", "two")])
        self.assertTrue(until(lambda: sorted(self.rec.starts()) == ["a", "b"]))   # both running
        ga.set(), gb.set()

    def test_actions_without_a_lock_are_not_held_back(self):
        gate = self.rec.gate("locked")
        seen = self.rec.started_event("locked")
        r = self.runner()
        r.submit(EVENT, [job("locked", "yk")])
        seen.wait(5)
        r.submit(EVENT, [job("free")])
        self.assertTrue(until(lambda: "free" in self.rec.starts()))
        gate.set()

    def test_a_crash_hands_the_turn_on(self):
        self.rec.raises["bad"] = RuntimeError("boom")
        r = self.runner()
        with self.assertLogs("udevmon.actions", "ERROR"):
            r.submit(EVENT, [job("bad", "yk")])
            r.submit(EVENT, [job("good", "yk")])
            self.assertTrue(until(lambda: self.rec.starts() == ["bad", "good"]))

    def test_even_an_exit_that_is_not_an_exception_hands_the_turns_on(self):
        self.rec.raises["bad"] = SystemExit()
        r = self.runner()
        r.submit(EVENT, [job("bad", "yk"), job("same-event", "yk")])   # its own later ticket
        r.submit(EVENT, [job("later", "yk")])                           # a later event
        self.assertTrue(until(lambda: "later" in self.rec.starts()))
        self.assertNotIn("same-event", self.rec.starts())               # it never ran, but did
        r.shutdown(wait=True)                                           # not block anyone

    def test_the_same_lock_twice_in_one_event_does_not_deadlock(self):
        r = self.runner(1)
        r.submit(EVENT, [job("one", "yk"), job("free"), job("two", "yk")])
        self.assertTrue(until(lambda: self.rec.starts() == ["one", "free", "two"]))

    def test_the_order_within_an_event_is_kept_with_mixed_locks(self):
        r = self.runner()
        r.submit(EVENT, [job("a", "x"), job("b"), job("c", "y"), job("d", "x")])
        self.assertTrue(until(lambda: len(self.rec.starts()) == 4))
        self.assertEqual(self.rec.starts(), ["a", "b", "c", "d"])

    def test_two_locks_taken_in_opposite_orders_by_two_events_cannot_deadlock(self):
        r = self.runner(2)
        r.submit(EVENT, [job("A", "x"), job("B", "y")])
        r.submit(EVENT, [job("C", "y"), job("D", "x")])
        self.assertTrue(until(lambda: len(self.rec.starts()) == 4))
        self.assertLess(self.rec.starts().index("A"), self.rec.starts().index("D"))
        self.assertLess(self.rec.starts().index("B"), self.rec.starts().index("C"))

    def test_only_a_job_that_had_to_wait_is_logged(self):
        gate = self.rec.gate("first")
        seen = self.rec.started_event("first")
        r = self.runner()
        with self.assertLogs("udevmon.actions", "INFO") as logs:
            r.submit(EVENT, [job("first", "yk")])
            seen.wait(5)
            r.submit(EVENT, [job("second", "yk")])
            time.sleep(0.1)
            gate.set()
            self.assertTrue(until(lambda: "second" in self.rec.starts()))
            time.sleep(0.1)
        messages = [r.getMessage() for r in logs.records]
        self.assertIn("action 'second' waited for lock 'yk'", messages)
        self.assertNotIn("action 'first' waited for lock 'yk'", messages)


class RealCommandTests(unittest.TestCase):
    def test_two_real_scripts_never_overlap_and_keep_their_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            script = Path(tmp) / "s.sh"
            script.write_text('#!/bin/bash\necho "start $1" >> "$2"\nsleep 0.3\n'
                              'echo "end $1" >> "$2"\n')
            script.chmod(0o755)

            def real(name):
                return Job(C.Action(name, ("d",), (), ("mounted",),
                                    (str(script), name, str(out)), 10, lock="yk"), "d", None)
            r = A.ActionRunner(4)
            with self.assertLogs("udevmon.actions", "INFO"):
                for n in ("plug", "unplug"):
                    r.submit(EVENT, [real(n)])
                r.shutdown(wait=True)
            self.assertEqual(out.read_text().split("\n")[:-1],
                             ["start plug", "end plug", "start unplug", "end unplug"])


class ConfigTests(unittest.TestCase):
    def parse(self, **extra):
        return C.parse({"devices": {"d": {"match": {"serial": "S"}}},
                        "actions": [{"name": "a", "device": "d", "on": ["mounted"],
                                     "run": ["true"], **extra}]})

    def test_default_and_a_name(self):
        self.assertIsNone(self.parse().actions[0].lock)
        self.assertEqual(self.parse(lock="yubikey").actions[0].lock, "yubikey")

    def test_only_a_name_is_accepted(self):
        for bad in ("two words", "", 5, ["x"], True):
            with self.subTest(bad):
                with self.assertRaises(C.ConfigError) as cm:
                    self.parse(lock=bad)
                self.assertIn("'lock' must be a name", str(cm.exception))

    def test_it_can_be_combined_with_detach(self):
        a = self.parse(lock="x", detach=True).actions[0]
        self.assertEqual((a.lock, a.detach), ("x", True))


class EditTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.p.write_text('[devices.d]\nmatch = { serial = "S" }\n\n[[actions]]\nname = "a"\n'
                          'device = "d"\non = ["mounted"]\nrun = ["true"]   # keep\n\n'
                          '[[actions]]\nname = "b"\ndevice = "d"\non = ["mounted"]\n'
                          'run = ["true"]\nlock = "yubikey"\n')
        self.p.chmod(0o600)

    def lock(self, name):
        return next(a for a in C.load(self.p).actions if a.name == name).lock

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
        E.set_action_options(self.p, "a", lock="disks")
        self.assertEqual(self.lock("a"), "disks")
        self.assertIn("# keep", self.p.read_text())
        E.set_action_options(self.p, "b", lock="other")
        self.assertEqual(self.lock("b"), "other")
        E.set_action_options(self.p, "b", lock="")
        self.assertIsNone(self.lock("b"))
        self.assertNotIn("lock", self.p.read_text().split('name = "b"')[1])

    def test_a_bad_name_is_refused_and_nothing_is_written(self):
        before = self.p.read_bytes()
        with self.assertRaises(C.ConfigError):
            E.set_action_options(self.p, "a", lock="not a name")
        self.assertEqual(self.p.read_bytes(), before)

    def test_an_edit_that_does_nothing_is_rolled_back(self):
        before = self.p.read_bytes()
        with mock.patch.object(E, "_edited", return_value=before.decode()):
            with self.assertRaises(C.ConfigError):
                E.set_action_options(self.p, "a", lock="x")
        self.assertEqual(self.p.read_bytes(), before)

    def test_cli(self):
        rc, _, _ = self.run_cli("actions", "add", "--device", "d", "--lock", "yk", "--name", "c",
                                "mounted", "--", "true")
        self.assertEqual(rc, 0)
        self.assertEqual(self.lock("c"), "yk")
        rc, out, _ = self.run_cli("actions", "list")
        self.assertRegex(out, r"c .*\[lock: yk\]")
        self.assertEqual(self.run_cli("actions", "set", "c", "--no-lock")[0], 0)
        self.assertIsNone(self.lock("c"))
        self.assertEqual(self.run_cli("actions", "set", "a", "--lock", "z")[0], 0)
        self.assertEqual(self.lock("a"), "z")
        self.assertEqual(self.run_cli("actions", "set", "a", "--lock", "z", "--no-lock")[0], 2)
        rc, _, err = self.run_cli("actions", "add", "--device", "d", "--lock", "no good",
                                  "mounted", "--", "true")
        self.assertEqual(rc, 1)
        self.assertIn("a lock name may contain only", err)

    def test_explain_shows_the_lock(self):
        c = C.load(self.p)
        text = "\n".join(explain.explain_endpoint(
            c, Endpoint("udisks", "k", {"serial": "S", "mountpoint": "/m"}), [MOUNTED]))
        self.assertIn("runs 'b' (device d, lock yubikey)", text)

    def test_completion_offers_the_names_in_use(self):
        parts = ["udevmon", "--config", str(self.p), "actions", "set", "a", "--lock", ""]
        self.assertEqual(completion.complete(cli.build_parser(), parts, len(parts) - 1)[1],
                         ["yubikey"])


if __name__ == "__main__":
    unittest.main()
