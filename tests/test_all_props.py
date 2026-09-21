"""`scan --all-props` and `present --all-props`: every property, raw udev ones included."""

import contextlib
import io
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli
from udevmon.events import CONNECTED, STARTUP, Endpoint, Event
from udevmon.scan import Scanner, format_all_props, format_props

KEY = Endpoint("udev", "/sys/k", {
    "vendor_id": "1050", "product_id": "0407", "port_path": "3-4",
    "devnode": "/dev/bus/usb/003/006", "syspath": "/sys/k", "subsystem": "usb",
    "udev_id_model": "Yubikey_4", "udev_usec_initialized": "9878235508"})


class FormatTests(unittest.TestCase):
    def test_sorted_one_per_line_with_the_raw_properties(self):
        lines = format_all_props(KEY)
        self.assertEqual([l.split(" = ")[0] for l in lines], sorted(KEY.props))
        self.assertIn("udev_id_model = Yubikey_4", lines)

    def test_volatile_ones_are_marked(self):
        lines = format_all_props(KEY)
        marked = {l.split(" = ")[0] for l in lines if "[changes per plug]" in l}
        self.assertEqual(marked, {"devnode", "syspath", "udev_usec_initialized"})
        self.assertIn("port_path = 3-4", lines)               # not volatile: no marker

    def test_the_default_line_still_hides_them(self):
        text = format_props(KEY)
        self.assertNotIn("udev_", text)
        self.assertNotIn("syspath", text)


class PresentTests(unittest.TestCase):
    def run_present(self, *args):
        out = io.StringIO()
        with mock.patch("udevmon.sources.udev.UdevSource.snapshot", return_value=[KEY]), \
                mock.patch("udevmon.sources.udisks.UdisksSource.snapshot", return_value=[]), \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["--config", "/nonexistent/c.toml", "present", *args]), 0)
        return out.getvalue()

    def test_default_output_is_unchanged(self):
        text = self.run_present()
        self.assertIn("vendor_id=1050", text)
        self.assertNotIn("udev_usec_initialized", text)

    def test_all_props_lists_everything(self):
        text = self.run_present("--all-props")
        self.assertIn("    udev_usec_initialized = 9878235508   [changes per plug]", text)
        self.assertIn("    udev_id_model = Yubikey_4", text)
        self.assertNotIn("vendor_id=1050", text)              # the compact line is replaced


class ScanTests(unittest.TestCase):
    def emit(self, kind, all_props):
        scanner = Scanner(Path("/nonexistent/c.toml"), prompt=False, all_props=all_props)
        out = io.StringIO()
        with mock.patch("gi.repository.GLib.timeout_add"), contextlib.redirect_stdout(out):
            scanner._emit(Event(kind, KEY))
        return out.getvalue()

    def test_connected_event(self):
        self.assertNotIn("udev_id_model", self.emit(CONNECTED, False))
        text = self.emit(CONNECTED, True)
        self.assertIn("udev_id_model = Yubikey_4", text)
        self.assertNotIn("vendor_id=1050", text)

    def test_things_already_present(self):
        self.assertNotIn("udev_id_model", self.emit(STARTUP, False))
        self.assertIn("udev_usec_initialized = 9878235508", self.emit(STARTUP, True))

    def test_the_flag_reaches_the_scanner(self):
        with mock.patch("udevmon.scan.Scanner") as scanner:
            scanner.return_value.run.return_value = 0
            cli.main(["--config", "/nonexistent/c.toml", "scan", "--no-prompt", "--all-props"])
        self.assertTrue(scanner.call_args.kwargs["all_props"])


if __name__ == "__main__":
    unittest.main()
