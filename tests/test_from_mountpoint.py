# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`devices add --from-mountpoint` and `present --json`: naming a plugged drive from a script."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli
from udevmon import config as C
from udevmon.events import Endpoint
from udevmon.sources import udisks as D

from .test_sources import DRV, FS, P, managed


def fs(**props):
    base = {"fs_uuid": "U-1", "drive_serial": "SER1", "fs_label": "KEY",
            "mountpoint": "/media/u/KEY", "instance": "i1"}
    return Endpoint("udisks", "/org/x", {k: v for k, v in {**base, **props}.items() if v})


class TrackerTests(unittest.TestCase):
    def test_finds_the_filesystem_by_any_of_its_mount_points(self):
        objects = managed(mounted=True)
        objects[P][FS]["MountPoints"] = [list(b"/run/media/u/BACKUP\0"), list(b"/mnt/b\0")]
        t = D.UdisksTracker()
        t.load(objects)
        found = t.mounted_at("/mnt/b/")
        self.assertEqual(found.props["fs_uuid"], "U-1")
        self.assertEqual(found.props["mountpoint"], "/mnt/b")
        self.assertIsNone(t.mounted_at("/mnt"))

    def test_symlinks_are_resolved(self):
        with tempfile.TemporaryDirectory() as tmp:
            real, link = Path(tmp) / "real", Path(tmp) / "link"
            real.mkdir()
            link.symlink_to(real)
            objects = managed(mounted=True)
            objects[P][FS]["MountPoints"] = [list(str(real).encode() + b"\0")]
            t = D.UdisksTracker()
            t.load(objects)
            self.assertIsNotNone(t.mounted_at(str(link)))

    def test_not_mounted_is_none(self):
        t = D.UdisksTracker()
        t.load(managed(mounted=False))
        self.assertIsNone(t.mounted_at("/run/media/u/BACKUP"))


class MatchTests(unittest.TestCase):
    def test_takes_fs_uuid_and_drive_serial(self):
        self.assertEqual(cli.match_from_mount(C.Config(), fs(), "/media/u/KEY"),
                         {"fs_uuid": "U-1", "drive_serial": "SER1"})

    def test_refusals(self):
        cfg = C.parse({"devices": {"old": {"match": {"fs_uuid": "u-1"}}}})
        for endpoint, cfg_, says in ((None, C.Config(), "not the mount point"),
                                     (fs(fs_uuid=None), C.Config(), "no UUID"),
                                     (fs(drive_serial=None), C.Config(), "no serial"),
                                     (fs(drive_serial="0000"), C.Config(), "placeholder"),
                                     (fs(), cfg, "'old'")):
            with self.subTest(says):
                with self.assertRaises(C.ConfigError) as e:
                    cli.match_from_mount(cfg_, endpoint, "/media/u/KEY")
                self.assertIn(says, str(e.exception))


class CliTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfg = Path(tmp.name) / "config.toml"

    def run_cli(self, *args, endpoint=None, snapshot=()):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(D.UdisksSource, "mounted_at", return_value=endpoint), \
                mock.patch.object(D.UdisksSource, "snapshot", return_value=list(snapshot)), \
                mock.patch("udevmon.sources.udev.UdevSource.snapshot", return_value=[]), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.cfg), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_adds_the_device_and_explain_would_see_it(self):
        rc, out, err = self.run_cli("devices", "add", "key-daily", "--from-mountpoint",
                                    "/media/u/KEY", "--tag", "keepass", "--no-reload",
                                    endpoint=fs())
        self.assertEqual(rc, 0, err)
        d = C.load(self.cfg).devices["key-daily"]
        self.assertEqual(d.match, {"fs_uuid": "U-1", "drive_serial": "SER1"})
        self.assertEqual(d.tags, ("keepass",))
        self.assertIn("Filesystem KEY", d.description)
        from udevmon.matching import resolve
        self.assertEqual([x.name for x in resolve(C.load(self.cfg), fs().props).named],
                         ["key-daily"])

    def test_a_refusal_writes_nothing(self):
        rc, _, err = self.run_cli("devices", "add", "k", "--from-mountpoint", "/nope",
                                  "--no-reload", endpoint=None)
        self.assertEqual(rc, 1)
        self.assertIn("not the mount point", err)
        self.assertFalse(self.cfg.exists())

    def test_match_and_from_mountpoint_exclude_each_other(self):
        rc, _, _ = self.run_cli("devices", "add", "k", "--from-mountpoint", "/x",
                                "--match", "fs_uuid=U")
        self.assertEqual(rc, 2)

    def test_present_json(self):
        self.cfg.write_text('[devices.key]\nmatch = { fs_uuid = "U-1" }\ntags = ["keepass"]\n')
        os.chmod(self.cfg, 0o600)
        rc, out, err = self.run_cli("present", "--json", snapshot=[fs()])
        self.assertEqual(rc, 0, err)
        [entry] = json.loads(out)
        self.assertEqual(entry["kind"], "filesystem")
        self.assertEqual(entry["devices"], ["key"])
        self.assertEqual(entry["tags"], ["keepass"])
        self.assertEqual(entry["props"]["drive_serial"], "SER1")
        self.assertEqual(entry["suggested_match"], {"fs_uuid": "U-1"})


if __name__ == "__main__":
    unittest.main()
