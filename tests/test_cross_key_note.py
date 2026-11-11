# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`check` notes two devices whose matches use different keys but could select one thing."""

import unittest

from udevmon import config as C


def notes(devices):
    return C.parse({"devices": devices}).notes


class CouldMeetTests(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(C.thing_kind({"fs_uuid": "U"}), "filesystem")
        self.assertEqual(C.thing_kind({"serial": "S", "udev_ID_X": "1"}), "USB device")
        self.assertIsNone(C.thing_kind({"serial": "S", "fs_uuid": "U"}))     # nothing has both
        self.assertIsNone(C.thing_kind({"devnode": "/dev/x"}))               # either kind

    def test_could_meet(self):
        self.assertEqual(C.could_meet({"fs_uuid": "U"}, {"drive_serial": "D"}), "filesystem")
        self.assertEqual(C.could_meet({"fs_uuid": "U", "fs_label": "K"},
                                      {"fs_uuid": "u", "drive_serial": "D"}), "filesystem")
        self.assertEqual(C.could_meet({"serial": "S"}, {"vendor_id": "1234"}), "USB device")
        self.assertEqual(C.could_meet({"fs_uuid": ["A", "B"]}, {"fs_uuid": "b",
                                                                "fs_label": "x"}), "filesystem")

    def test_cannot_meet(self):
        self.assertIsNone(C.could_meet({"fs_uuid": "U"}, {"fs_uuid": "V"}))   # a key differs
        self.assertIsNone(C.could_meet({"fs_uuid": "U", "drive_serial": "D"},
                                       {"fs_uuid": "V", "fs_label": "K"}))
        self.assertIsNone(C.could_meet({"serial": "S"}, {"fs_uuid": "U"}))    # different kinds


class NoteTests(unittest.TestCase):
    def test_the_note(self):
        (note,) = notes({"a": {"match": {"fs_uuid": "U"}},
                         "b": {"match": {"drive_serial": "D"}}})
        self.assertIn("devices 'a' and 'b' match on different keys (fs_uuid; drive_serial), "
                      "so one filesystem could match both", note)
        self.assertIn("{device} = 'a'", note)

    def test_an_ignored_one(self):
        note = notes({"a": {"match": {"fs_uuid": "U"}, "status": "ignored"},
                      "b": {"match": {"drive_serial": "D"}}})[-1]       # after "is ignored"
        self.assertIn("'a' is ignored, so then nothing would run for 'b'", note)

    def test_quiet_when_they_cannot_meet(self):
        self.assertEqual(notes({"a": {"match": {"fs_uuid": "U"}},
                                "b": {"match": {"fs_uuid": "V", "drive_serial": "D"}},
                                "c": {"match": {"serial": "S"}}}), [])

    def test_containment_keeps_its_own_note(self):
        (note,) = notes({"a": {"match": {"fs_uuid": "U"}},
                         "b": {"match": {"fs_uuid": "U", "drive_serial": "D"}}})
        self.assertIn("never matches alone", note)


if __name__ == "__main__":
    unittest.main()
