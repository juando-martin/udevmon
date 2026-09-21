"""`{udev_NAME}` placeholders: raw udev properties usable in a command."""

import unittest
from unittest import mock

from udevmon import actions as A
from udevmon import config as C
from udevmon import explain
from udevmon.events import CONNECTED, MOUNTED, Endpoint, Event


def parse(run, **action):
    return C.parse({"devices": {"d": {"match": {"serial": "S"}}},
                    "actions": [{"name": "a", "device": "d", "on": ["connected"], "run": run,
                                 **action}]})


class ConfigTests(unittest.TestCase):
    def test_any_udev_property_is_accepted_for_every_event(self):
        for kind in ("connected", "disconnected", "mounted", "unmounted", "removed",
                     "startup", "shutdown"):
            with self.subTest(kind):
                parse(["echo", "{udev_id_model}", "{udev_usec_initialized}"], on=[kind])

    def test_other_unknown_placeholders_are_still_refused(self):
        for name in ("udev", "udevx", "id_model", "nonsense"):
            with self.subTest(name):
                with self.assertRaises(C.ConfigError) as cm:
                    parse(["echo", "{%s}" % name])
                self.assertIn(name, str(cm.exception))

    def test_they_are_hardware_values_for_the_unnamed_warning(self):
        c = C.parse({"tags": {"t": {"match": {"fs_type": "ext4"}}},
                     "actions": [{"name": "a", "tag": "t", "on": ["mounted"],
                                  "allow_unnamed": True, "run": ["echo", "{udev_id_model}"]}]})
        self.assertTrue(any("hardware-supplied" in w and "udev_id_model" in w
                            for w in c.warnings))

    def test_a_brace_pair_that_is_not_a_placeholder_is_left_alone(self):
        parse(["awk", "{print $1}", "${udev_x}"])              # `$` before the brace: literal


class RunTests(unittest.TestCase):
    def action(self, run):
        return C.Action("t", ("d",), (), ("connected",), tuple(run), 10)

    def test_the_value_is_filled_in_as_one_argument(self):
        with mock.patch("subprocess.Popen") as popen:
            popen.return_value.communicate.return_value = ("", None)
            popen.return_value.returncode = 0
            A.run_action(self.action(["echo", "{udev_id_model}"]),
                         Event(CONNECTED, Endpoint("udev", "k", {"udev_id_model": "Yubikey 4"})),
                         "d")
        self.assertEqual(popen.call_args.args[0], ["echo", "Yubikey 4"])

    def test_a_thing_that_lacks_it_skips_the_action(self):
        with mock.patch("subprocess.Popen") as popen:
            rc = A.run_action(self.action(["echo", "{udev_id_model}"]),
                              Event(CONNECTED, Endpoint("udev", "k", {})), "d")
        self.assertIsNone(rc)
        popen.assert_not_called()

    def test_explain_says_when_it_is_not_available(self):
        c = parse(["echo", "{udev_id_model}"])
        text = "\n".join(explain.explain_endpoint(c, Endpoint("udev", "k", {"serial": "S"}),
                                                  [CONNECTED]))
        self.assertIn("{udev_id_model} is not available", text)
        text = "\n".join(explain.explain_endpoint(
            c, Endpoint("udev", "k", {"serial": "S", "udev_id_model": "X"}), [CONNECTED]))
        self.assertIn("echo X", text)


if __name__ == "__main__":
    unittest.main()
