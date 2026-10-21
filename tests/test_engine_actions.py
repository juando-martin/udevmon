import tempfile
import unittest
from pathlib import Path

from udevmon import actions as A
from udevmon import config as C
from udevmon.engine import Engine
from udevmon.events import CONNECTED, MOUNTED, UNMOUNTED, Endpoint, Event


class FakeRunner:
    def __init__(self):
        self.calls = []

    def submit(self, event, jobs, ended=None):
        self.calls.append((event.kind, [(j.action.name, j.device) for j in jobs]))


def cfg(text_dict):
    return C.parse(text_dict)


DATA = {
    "daemon": {"debounce": 1.0},
    "devices": {"disk": {"match": {"fs_uuid": "U1"}}, "key": {"match": {"serial": "S9x7"}}},
    "actions": [
        {"name": "a1", "device": "disk", "on": ["mounted"], "run": ["true"]},
        {"name": "a2", "device": "disk", "on": ["mounted"], "run": ["true"]},
        {"name": "k1", "device": "key", "on": ["connected"], "run": ["true"]},
    ],
}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.now = 100.0
        self.runner = FakeRunner()
        self.engine = Engine(cfg(DATA), self.runner, clock=lambda: self.now)

    def ev(self, kind, source, **props):
        return Event(kind, Endpoint(source, "k", props))

    def test_matching_and_order(self):
        self.engine.handle(self.ev(MOUNTED, "udisks", fs_uuid="U1", mountpoint="/m"))
        self.assertEqual(self.runner.calls, [(MOUNTED, [("a1", "disk"), ("a2", "disk")])])

    def test_no_match_or_wrong_kind(self):
        self.engine.handle(self.ev(MOUNTED, "udisks", fs_uuid="other"))
        self.engine.handle(self.ev(CONNECTED, "udev", fs_uuid="U1"))
        self.assertEqual(self.runner.calls, [])

    def test_debounce(self):
        e = self.ev(CONNECTED, "udev", serial="S9x7")
        self.engine.handle(e)
        self.now += 0.5
        self.engine.handle(e)
        self.assertEqual(len(self.runner.calls), 1)
        self.now += 1.0
        self.engine.handle(e)
        self.assertEqual(len(self.runner.calls), 2)

    def test_two_things_matching_one_device_are_not_repeats(self):
        # two YubiKeys without a serial in the match, present at startup
        for path in ("/usb/1", "/usb/2"):
            self.engine.handle(Event(CONNECTED, Endpoint("udev", path, {"serial": "S9x7"})))
        self.assertEqual(len(self.runner.calls), 2)

    def test_another_event_in_between_ends_the_repeat(self):
        # mounted, unmounted by an action, mounted again by the file manager: 0.4 s
        mounted = self.ev(MOUNTED, "udisks", fs_uuid="U1", mountpoint="/m")
        self.engine.handle(mounted)
        self.now += 0.15
        self.engine.handle(self.ev(UNMOUNTED, "udisks", fs_uuid="U1", mountpoint="/m"))
        self.now += 0.3
        self.engine.handle(mounted)
        self.assertEqual([k for k, _ in self.runner.calls], [MOUNTED, MOUNTED])

    def test_a_dropped_repeat_is_logged(self):
        e = self.ev(CONNECTED, "udev", serial="S9x7")
        self.engine.handle(e)
        with self.assertLogs("udevmon.engine", "INFO") as logs:
            self.engine.handle(e)
        self.assertIn("dropped: a repeat within 1s", logs.output[0])


class RunActionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def action(self, run, timeout=10):
        return C.Action("t", ("a",), (), ("mounted",), tuple(run), timeout)

    def event(self, **props):
        return Event(MOUNTED, Endpoint("udisks", "k", props))

    def test_runs_with_placeholders_and_env(self):
        out = self.dir / "out"
        rc = A.run_action(self.action(["sh", "-c", 'echo "$1|$UDEVMON_EVENT|$UDEVMON_DEVICE|$UDEVMON_FS_UUID" > "$2"',
                                       "sh", "{mountpoint}", str(out)]),
                          self.event(mountpoint="/mnt/x y", fs_uuid="U"), "disk")
        self.assertEqual(rc, 0)
        self.assertEqual(out.read_text().strip(), "/mnt/x y|mounted|disk|U")

    def test_value_is_single_argv_no_shell(self):
        out = self.dir / "out"
        marker = self.dir / "pwned"
        A.run_action(self.action(["sh", "-c", 'printf %s "$1" > "$2"', "sh", "{mountpoint}", str(out)]),
                     self.event(mountpoint=f"x; touch {marker}"), "disk")
        self.assertEqual(out.read_text(), f"x; touch {marker}")
        self.assertFalse(marker.exists())

    def test_missing_placeholder_skips(self):
        marker = self.dir / "ran"
        rc = A.run_action(self.action(["touch", str(marker), "{mountpoint}"]), self.event(), "disk")
        self.assertIsNone(rc)
        self.assertFalse(marker.exists())

    def test_nonzero_exit(self):
        self.assertEqual(A.run_action(self.action(["false"]), self.event(), "d"), 1)

    def test_timeout_kills_group(self):
        import time
        t = time.time()
        rc = A.run_action(self.action(["sh", "-c", "sleep 30 & wait"], timeout=0.5),
                          self.event(), "d")
        self.assertIsNone(rc)
        self.assertLess(time.time() - t, 10)

    def test_dry_run_and_bad_command(self):
        self.assertIsNone(A.run_action(self.action(["false"]), self.event(), "d", dry_run=True))
        self.assertIsNone(A.run_action(self.action(["/nonexistent/x"]), self.event(), "d"))


if __name__ == "__main__":
    unittest.main()
