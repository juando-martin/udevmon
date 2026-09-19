import unittest

from udevmon.events import Device
from udevmon.matching import (MEDIUM, STRONG, WEAK, build_match, default_match_keys,
                              is_suspicious_serial, matches, strength)


class MatchingTests(unittest.TestCase):
    def test_matches_all_pairs_case_insensitive(self):
        props = {"vendor_id": "1050", "serial": "ABC123", "extra": "x"}
        self.assertTrue(matches({"vendor_id": "1050", "serial": "abc123"}, props))
        self.assertFalse(matches({"vendor_id": "1050", "serial": "nope"}, props))
        self.assertFalse(matches({"missing": "1"}, props))
        self.assertFalse(matches({}, props))

    def test_strength(self):
        self.assertEqual(strength({"fs_uuid": "X"}), STRONG)
        self.assertEqual(strength({"vendor_id": "1", "product_id": "2", "serial": "S9x7"}), STRONG)
        self.assertEqual(strength({"vendor_id": "1", "product_id": "2", "serial": "0000000"}), WEAK)
        self.assertEqual(strength({"vendor_id": "1", "product_id": "2", "port_path": "1-2"}), MEDIUM)
        self.assertEqual(strength({"vendor_id": "1", "product_id": "2"}), WEAK)
        self.assertEqual(strength({"fs_label": "BACKUP"}), WEAK)

    def test_suspicious_serials(self):
        for s in ("", "0000", "AAAAAAAA", "0123456789ABCDEF", "1234567890"):
            self.assertTrue(is_suspicious_serial(s), s)
        self.assertFalse(is_suspicious_serial("323434393551"))

    def test_default_keys(self):
        usb = Device("udev", "k", {"vendor_id": "1", "product_id": "2", "serial": "S9x7",
                                   "port_path": "1-2"})
        self.assertEqual(default_match_keys(usb), ["vendor_id", "product_id", "serial"])
        noserial = Device("udev", "k", {"vendor_id": "1", "product_id": "2", "port_path": "1-2"})
        self.assertEqual(default_match_keys(noserial), ["vendor_id", "product_id", "port_path"])
        fs = Device("udisks", "k", {"fs_uuid": "U", "fs_label": "L", "part_uuid": "P"})
        self.assertEqual(default_match_keys(fs), ["fs_uuid"])
        fs2 = Device("udisks", "k", {"fs_label": "L"})
        self.assertEqual(default_match_keys(fs2), ["fs_label"])
        self.assertEqual(build_match(usb, ["vendor_id", "serial"]),
                         {"vendor_id": "1", "serial": "S9x7"})


if __name__ == "__main__":
    unittest.main()
