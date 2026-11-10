# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""contrib/rclone-config: publish a drive's rclone config while the drive is present.

Drives are folders in the per-run temp folder; lsblk is faked (in-process by
replacing mounted_identities, and on PATH for the command-line runs). The
runtime directory is the suite's private one (tests/__init__).
"""

import contextlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "contrib" / "rclone-config" / "rclone_config.py"


def load():
    spec = importlib.util.spec_from_file_location("rclone_config", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rc = load()

A, B, C = "SERIAL-A:AAAA-0001", "SERIAL-B:BBBB-0002", "SERIAL-C:CCCC-0003"


class Base(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.runtime = self.root / "run"
        self.runtime.mkdir(mode=0o700)
        self.env = {"XDG_RUNTIME_DIR": str(self.runtime)}
        old = os.environ.get("XDG_RUNTIME_DIR")
        os.environ["XDG_RUNTIME_DIR"] = str(self.runtime)
        self.addCleanup(os.environ.__setitem__, "XDG_RUNTIME_DIR", old)
        self.mounts = {}                         # resolved mount point -> identity
        original = rc.mounted_identities
        rc.mounted_identities = lambda: dict(self.mounts)
        self.addCleanup(setattr, rc, "mounted_identities", original)
        self.published = self.runtime / "rclone" / "rclone.conf"

    def drive(self, identity, content=b"[GoogleDrive]\ntype = drive\n", name=None):
        mount = self.root / (name or identity.split(":")[0])
        mount.mkdir()
        if content is not None:
            (mount / "config" / "rclone").mkdir(parents=True)
            (mount / "config" / "rclone" / "rclone.conf").write_bytes(content)
        self.mounts[str(mount.resolve())] = identity
        return mount

    def unplug(self, mount):
        del self.mounts[str(mount.resolve())]

    def up(self, mount, identity):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc.up(str(mount), identity)
        return err.getvalue()

    def down(self, mount, identity):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc.down(str(mount), identity)
        return err.getvalue()

    def failed_up(self, mount, identity):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(rc.Failure) as caught:
            rc.up(str(mount), identity)
        return str(caught.exception), err.getvalue()


class UpTests(Base):
    def test_publishes_privately(self):
        mount = self.drive(A, b"secret-a")
        self.up(mount, A)
        self.assertEqual(self.published.read_bytes(), b"secret-a")
        self.assertEqual(stat.S_IMODE(self.published.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.published.parent.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.runtime / "rclone-config").stat().st_mode), 0o700)

    def test_only_rclone_conf_is_copied(self):
        mount = self.drive(A)
        (mount / "config" / "rclone" / "filter.txt").write_text("x")
        self.up(mount, A)
        self.assertEqual(sorted(p.name for p in self.published.parent.iterdir()), ["rclone.conf"])

    def test_messages_never_contain_the_config(self):
        a, b = self.drive(A, b"TOKEN-A"), self.drive(B, b"TOKEN-B")
        text = self.up(a, A) + self.up(b, B) + self.down(a, A) + self.down(b, B)
        self.assertNotIn("TOKEN", text)

    def test_a_tagged_drive_without_a_config_is_an_error(self):
        mount = self.drive(A, content=None)
        message, _ = self.failed_up(mount, A)
        self.assertIn("has no config/rclone/rclone.conf", message)
        self.assertFalse(self.published.exists())

    def test_identity_must_match_the_mounted_filesystem(self):
        mount = self.drive(A, b"secret-a")
        message, _ = self.failed_up(mount, B)
        self.assertIn("is not the filesystem mounted at", message)
        self.assertFalse(self.published.exists())

    def test_a_symlinked_source_is_refused(self):
        mount = self.drive(A, content=None)
        outside = self.root / "outside.conf"
        outside.write_bytes(b"elsewhere")
        (mount / "config" / "rclone").mkdir(parents=True)
        (mount / "config" / "rclone" / "rclone.conf").symlink_to(outside)
        message, _ = self.failed_up(mount, A)
        self.assertIn("outside the drive", message)

    def test_a_symlink_inside_the_drive_is_still_not_followed(self):
        mount = self.drive(A, content=None)
        (mount / "config" / "rclone").mkdir(parents=True)
        (mount / "real.conf").write_bytes(b"x")
        (mount / "config" / "rclone" / "rclone.conf").symlink_to(mount / "real.conf")
        message, _ = self.failed_up(mount, A)
        self.assertIn("cannot open", message)

    def test_a_directory_in_place_of_the_config_is_refused(self):
        mount = self.drive(A, content=None)
        (mount / "config" / "rclone" / "rclone.conf").mkdir(parents=True)
        message, _ = self.failed_up(mount, A)
        self.assertIn("not a regular file", message)

    def test_a_fifo_in_place_of_the_config_does_not_hang(self):
        mount = self.drive(A, content=None)
        (mount / "config" / "rclone").mkdir(parents=True)
        os.mkfifo(mount / "config" / "rclone" / "rclone.conf")
        message, _ = self.failed_up(mount, A)
        self.assertIn("not a regular file", message)

    def test_a_huge_file_is_refused(self):
        mount = self.drive(A, b"x" * (rc.MAX_SIZE + 1))
        message, _ = self.failed_up(mount, A)
        self.assertIn("larger than", message)

    def test_a_symlinked_published_file_is_refused(self):
        mount = self.drive(A)
        self.published.parent.mkdir(mode=0o700)
        self.published.symlink_to(self.root / "target")
        message, _ = self.failed_up(mount, A)
        self.assertIn("must be a regular file", message)

    def test_up_again_keeps_what_rclone_wrote(self):
        mount = self.drive(A, b"secret-a")
        self.up(mount, A)
        self.published.write_bytes(b"secret-a + refreshed token")   # rclone's write-back
        self.up(mount, A)                                           # e.g. udevmon restarted
        self.assertEqual(self.published.read_bytes(), b"secret-a + refreshed token")

    def test_a_changed_config_on_the_published_drive_is_republished(self):
        mount = self.drive(A, b"old")
        self.up(mount, A)
        (mount / "config" / "rclone" / "rclone.conf").write_bytes(b"new")
        self.up(mount, A)
        self.assertEqual(self.published.read_bytes(), b"new")

    def test_the_first_drive_stays_published_and_a_difference_is_reported(self):
        a, b = self.drive(A, b"secret-a"), self.drive(B, b"secret-b")
        self.up(a, A)
        text = self.up(b, B)
        self.assertEqual(self.published.read_bytes(), b"secret-a")
        self.assertIn("different rclone configs", text)

    def test_equal_configs_are_not_reported(self):
        a, b = self.drive(A, b"same"), self.drive(B, b"same")
        self.up(a, A)
        self.assertNotIn("different", self.up(b, B))


class DownTests(Base):
    def test_last_drive_removes_the_config_and_its_folder(self):
        mount = self.drive(A)
        self.up(mount, A)
        self.unplug(mount)
        self.down(mount, A)
        self.assertFalse(self.published.exists())
        self.assertFalse(self.published.parent.exists())

    def test_other_files_next_to_it_are_left_alone(self):
        mount = self.drive(A)
        self.up(mount, A)
        (self.published.parent / "keep.txt").write_text("not ours")
        self.unplug(mount)
        self.down(mount, A)
        self.assertFalse(self.published.exists())
        self.assertTrue((self.published.parent / "keep.txt").exists())

    def test_removing_the_published_drive_switches_to_a_present_one(self):
        a, b = self.drive(A, b"secret-a"), self.drive(B, b"secret-b")
        self.up(a, A)
        self.up(b, B)
        self.unplug(a)
        self.down(a, A)
        self.assertEqual(self.published.read_bytes(), b"secret-b")
        self.unplug(b)
        self.down(b, B)
        self.assertFalse(self.published.exists())

    def test_the_switch_goes_to_the_drive_that_came_first(self):
        a, b, c = (self.drive(i, i.encode()) for i in (A, B, C))
        for mount, identity in ((a, A), (c, C), (b, B)):
            self.up(mount, identity)
        self.unplug(a)
        self.down(a, A)
        self.assertEqual(self.published.read_bytes(), C.encode())

    def test_removing_another_drive_keeps_the_published_one(self):
        a, b = self.drive(A, b"secret-a"), self.drive(B, b"secret-b")
        self.up(a, A)
        self.up(b, B)
        self.published.write_bytes(b"secret-a + refreshed")
        self.unplug(b)
        self.down(b, B)
        self.assertEqual(self.published.read_bytes(), b"secret-a + refreshed")

    def test_shutdown_removes_it_while_the_drive_is_still_mounted(self):
        mount = self.drive(A)
        self.up(mount, A)
        self.down(mount, A)                      # shutdown: no unplug
        self.assertFalse(self.published.exists())
        self.up(mount, A)                        # startup again
        self.assertTrue(self.published.exists())

    def test_down_twice_is_harmless(self):
        mount = self.drive(A)
        self.up(mount, A)
        self.unplug(mount)
        self.down(mount, A)
        self.down(mount, A)
        self.assertFalse(self.published.exists())

    def test_a_switch_to_an_unreadable_drive_skips_it(self):
        a, b = self.drive(A, b"secret-a"), self.drive(B, b"secret-b")
        self.up(a, A)
        self.up(b, B)
        (b / "config" / "rclone" / "rclone.conf").unlink()
        self.unplug(a)
        text = self.down(a, A)
        self.assertIn("ERROR:", text)
        self.assertFalse(self.published.exists())


class OrphanTests(Base):
    """A published config whose drive is not present is never kept."""

    def leftover(self, content=b"left by the old hook"):
        self.published.parent.mkdir(mode=0o700, exist_ok=True)
        self.published.write_bytes(content)

    def test_a_valid_up_replaces_a_leftover(self):
        self.leftover()
        mount = self.drive(A, b"secret-a")
        self.up(mount, A)
        self.assertEqual(self.published.read_bytes(), b"secret-a")

    def test_a_failed_up_deletes_a_leftover(self):
        self.leftover()
        mount = self.drive(A, content=None)
        self.failed_up(mount, A)
        self.assertFalse(self.published.exists())

    def test_a_down_deletes_a_leftover(self):
        self.leftover()
        self.down(self.root / "gone", A)
        self.assertFalse(self.published.exists())

    def test_a_missed_removal_is_healed_by_the_next_event(self):
        a, b = self.drive(A, b"secret-a"), self.drive(B, b"secret-b")
        self.up(a, A)
        self.unplug(a)                           # no down: udevmon was not running
        text = self.up(b, B)
        self.assertIn("forgetting drive " + A, text)
        self.assertEqual(self.published.read_bytes(), b"secret-b")

    def test_a_file_recreated_after_deletion_goes_at_the_next_down(self):
        mount = self.drive(A)
        self.up(mount, A)
        self.unplug(mount)
        self.down(mount, A)
        self.leftover(b"rclone saved a token after the deletion")
        self.down(mount, A)
        self.assertFalse(self.published.exists())

    def test_a_failed_up_on_a_drive_that_lost_its_config_unpublishes_it(self):
        mount = self.drive(A)
        self.up(mount, A)
        (mount / "config" / "rclone" / "rclone.conf").unlink()
        message, text = self.failed_up(mount, A)
        self.assertIn("has no", message)
        self.assertNotIn("ERROR:", text)         # reported once, by the caller
        self.assertFalse(self.published.exists())

    def test_a_tampered_record_is_dropped(self):
        mount = self.drive(A)
        self.up(mount, A)
        record = next((self.runtime / "rclone-config" / "sources").glob("*.json"))
        record.write_text(json.dumps({"identity": B, "mountpoint": "/", "hash": "", "since": 0}))
        self.unplug(mount)
        text = self.down(self.root / "other", C)   # an event of another drive
        self.assertIn("dropping unreadable drive record", text)
        self.assertFalse(self.published.exists())


class LsblkTests(unittest.TestCase):
    def test_partitions_inherit_the_disk_serial(self):
        data = {"blockdevices": [
            {"serial": "DISK1", "uuid": None, "mountpoints": [None], "children": [
                {"serial": None, "uuid": "AB12-CD34", "mountpoints": ["/media/u/D"]},
                {"serial": None, "uuid": "1111-2222", "mountpoints": [None]}]},
            {"serial": None, "uuid": "ROOT", "mountpoints": ["/"]}]}
        result = subprocess.CompletedProcess([], 0, json.dumps(data), "")
        original = rc.subprocess.run
        rc.subprocess.run = lambda *a, **k: result
        try:
            mounts = rc.mounted_identities()
        finally:
            rc.subprocess.run = original
        self.assertEqual(mounts, {str(Path("/media/u/D").resolve()): "DISK1:AB12-CD34"})


FAKE_LSBLK = """#!/bin/sh
cat "$FAKE_LSBLK_JSON"
"""


class CommandLineTests(Base):
    """The module run as a command, as udevmon would, under each available Python."""

    PYTHONS = list(dict.fromkeys(                 # a host may run it with its system 3.10
        os.path.realpath(p) for p in (sys.executable, shutil.which("python3.10")) if p))

    def setUp(self):
        super().setUp()
        bin_dir = self.root / "bin"
        bin_dir.mkdir()
        (bin_dir / "lsblk").write_text(FAKE_LSBLK)
        (bin_dir / "lsblk").chmod(0o755)
        self.table = self.root / "lsblk.json"
        self.env.update(PATH=f"{bin_dir}:/usr/bin:/bin", FAKE_LSBLK_JSON=str(self.table))

    def run_script(self, python, *args):
        self.table.write_text(json.dumps({"blockdevices": [
            {"serial": identity.split(":")[0], "uuid": None, "mountpoints": [None],
             "children": [{"serial": None, "uuid": identity.split(":")[1],
                           "mountpoints": [point]}]}
            for point, identity in self.mounts.items()]}))
        return subprocess.run([python, str(SCRIPT), *args], env=self.env,
                              capture_output=True, text=True)

    def test_up_status_down(self):
        for python in self.PYTHONS:
            with self.subTest(python=python):
                mount = self.drive(A, b"secret", name="D-" + Path(python).name)
                result = self.run_script(python, "up", str(mount), A)
                self.assertEqual((result.returncode, result.stderr.count("ERROR")), (0, 0),
                                 result.stderr)
                self.assertEqual(self.published.read_bytes(), b"secret")
                status = self.run_script(python, "status")
                self.assertIn(f"from drive: {A}", status.stdout)
                self.unplug(mount)
                result = self.run_script(python, "down", str(mount), A)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(self.published.exists())

    def test_errors_and_usage(self):
        mount = self.drive(A, content=None)
        result = self.run_script(sys.executable, "up", str(mount), A)
        self.assertEqual(result.returncode, 1)
        self.assertTrue(result.stderr.startswith("ERROR: "), result.stderr)
        self.assertEqual(self.run_script(sys.executable, "sideways").returncode, 2)
        bad = self.run_script(sys.executable, "up", str(mount), "no-colon")
        self.assertEqual(bad.returncode, 1)
        self.assertIn("invalid drive identity", bad.stderr)

    def test_no_runtime_directory_is_an_error(self):
        del self.env["XDG_RUNTIME_DIR"]
        result = self.run_script(sys.executable, "down", "/x", A)
        self.assertEqual(result.returncode, 1)
        self.assertIn("XDG_RUNTIME_DIR", result.stderr)

    def test_it_needs_only_the_standard_library(self):
        isolated = subprocess.run(
            [sys.executable, "-I", "-S", str(SCRIPT), "status"], env=self.env,
            capture_output=True, text=True)
        self.assertEqual(isolated.returncode, 0, isolated.stderr)


class PackagingTests(unittest.TestCase):
    def test_the_command_points_at_the_entry_function(self):
        try:
            import tomllib
        except ImportError:                      # Python 3.10
            self.skipTest("tomllib needs Python 3.11")
        project = tomllib.loads((SCRIPT.parent / "pyproject.toml").read_text())["project"]
        self.assertEqual(project["scripts"], {"rclone-config": "rclone_config:entry"})
        self.assertEqual(project["dependencies"], [])
        self.assertTrue(callable(rc.entry))


if __name__ == "__main__":
    unittest.main()
