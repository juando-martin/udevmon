"""`notify_on_failure`: a desktop notification when an action's last attempt fails.

Nothing is ever sent for real: `notify.send` is replaced in every test.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import config as C
from udevmon import notify
from udevmon.actions import ActionRunner
from udevmon.engine import Engine
from udevmon.events import Endpoint, Event


def config(run, **extra):
    return C.parse({"devices": {"key": {"match": {"fs_uuid": "U"}}},
                    "actions": [{"name": "sync", "device": "key", "on": ["mounted"],
                                 "run": run, "notify_on_failure": True, **extra}]})


class RunnerTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(notify, "send")
        self.send = patcher.start()
        self.addCleanup(patcher.stop)
        bus = mock.patch.object(notify, "session_bus_address", return_value="unix:path=/x")
        self.address = bus.start()
        self.addCleanup(bus.stop)

    def run_it(self, cfg, dry_run=False):
        runner = ActionRunner(1, dry_run=dry_run)
        self.addCleanup(runner.shutdown, True)
        engine = Engine(cfg, runner, paused=lambda: False)
        event = Event("mounted", Endpoint("udisks", "k", {"fs_uuid": "U", "instance": "i"}))
        jobs, _ = engine.trigger(event)
        with self.assertLogs("udevmon", "INFO"):
            return runner.submit(event, jobs).result(10)

    def test_a_non_zero_exit(self):
        self.run_it(config(["/bin/sh", "-c", "exit 3"]))
        summary, body, address = self.send.call_args.args
        self.assertEqual(summary, "udevmon: action sync failed (key)")
        self.assertEqual(body, "exit status 3")
        self.assertEqual(address, "unix:path=/x")

    def test_a_timeout(self):
        self.run_it(config(["/bin/sleep", "5"], timeout=0.2))
        self.assertEqual(self.send.call_args.args[1], "timed out after 0.2 s")

    def test_a_command_that_cannot_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.run_it(config([str(Path(tmp) / "missing")]))
        self.assertIn("could not start", self.send.call_args.args[1])

    def test_success_is_quiet(self):
        self.run_it(config(["/bin/true"]))
        self.send.assert_not_called()

    def test_off_by_default(self):
        cfg = C.parse({"devices": {"key": {"match": {"fs_uuid": "U"}}},
                       "actions": [{"name": "sync", "device": "key", "on": ["mounted"],
                                    "run": ["/bin/false"]}]})
        self.assertFalse(cfg.actions[0].notify_on_failure)
        self.run_it(cfg)
        self.send.assert_not_called()

    def test_retries_notify_only_after_the_last_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            counter = Path(tmp) / "n"
            script = f'echo x >> {counter}; [ $(wc -l < {counter}) -ge 3 ]'
            self.run_it(config(["/bin/sh", "-c", script], retries=2, retry_delay=0.01))
            self.send.assert_not_called()                  # third attempt succeeded
            counter.unlink()
            self.run_it(config(["/bin/sh", "-c", "exit 1"], retries=2, retry_delay=0.01))
        self.send.assert_called_once()
        self.assertEqual(self.send.call_args.args[1], "exit status 1 (after 3 attempts)")

    def test_no_session_bus_no_notification(self):
        self.address.return_value = None
        self.run_it(config(["/bin/false"]))
        self.send.assert_not_called()

    def test_a_sending_error_is_only_logged(self):
        self.send.side_effect = RuntimeError("no notification daemon")
        self.assertEqual(self.run_it(config(["/bin/false"])), [("sync", 1)])

    def test_a_dry_run_sends_nothing(self):
        self.run_it(config(["/bin/false"]), dry_run=True)
        self.send.assert_not_called()


class AddressTests(unittest.TestCase):
    def test_from_the_session_then_the_standard_bus(self):
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch("udevmon.session.update"):
            env = {"XDG_RUNTIME_DIR": tmp}
            self.assertIsNone(notify.session_bus_address(env))
            Path(tmp, "bus").touch()
            self.assertEqual(notify.session_bus_address(env), f"unix:path={tmp}/bus")
            env["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/1/bus"
            self.assertEqual(notify.session_bus_address(env), "unix:path=/run/user/1/bus")

    def test_the_suite_has_no_session_bus(self):
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", os.environ)


class ConfigTests(unittest.TestCase):
    def test_checked(self):
        with self.assertRaises(C.ConfigError):
            config(["/bin/true"], notify_on_failure="yes")
        with self.assertRaises(C.ConfigError) as e:
            config(["/bin/true"], detach=True)
        self.assertIn("detach", str(e.exception))


if __name__ == "__main__":
    unittest.main()
