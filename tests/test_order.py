# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`order` on an action: within one event, lower runs first; ties keep config order."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli
from udevmon import config as C
from udevmon import configedit as E
from udevmon import explain
from udevmon.engine import Engine
from udevmon.events import MOUNTED, UNMOUNTED, Endpoint, Event

MAIN = """\
[devices.key]
match = { fs_uuid = "U", drive_serial = "S" }
tags = ["keepass", "rclone"]
"""

KP_SYNC = """\
[[actions]]
name = "kp-up"
tag = "keepass"
on = ["startup", "mounted"]
run = ["/bin/true", "kp", "up"]

[[actions]]
name = "kp-down"
tag = "keepass"
on = ["unmounted"]
run = ["/bin/true", "kp", "down"]
"""

RCLONE = """\
[[actions]]
name = "rclone-config-up"
tag = "rclone"
on = ["startup", "mounted"]
order = 10
run = ["/bin/true", "rclone", "up"]

[[actions]]
name = "rclone-config-down"
tag = "rclone"
on = ["unmounted", "shutdown"]
order = 90
run = ["/bin/true", "rclone", "down"]
"""

THING = Endpoint("udisks", "k", {"fs_uuid": "U", "drive_serial": "S", "mountpoint": "/m",
                                 "instance": "i"})


def run_order(cfg, kind):
    engine = Engine(cfg, runner=mock.Mock(), paused=lambda: False)
    return [j.action.name for j in engine.handle(Event(kind, THING))]


class Files(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.main = Path(tmp.name) / "config.toml"
        self.dir = Path(tmp.name) / "config.d"
        self.dir.mkdir(mode=0o700)

    def write(self, path, text):
        path.write_text(text)
        os.chmod(path, 0o600)


class DropInTests(Files):
    def test_the_provider_nests_around_kp_sync_in_any_file_order(self):
        """The case from kp-sync's request: each tool in its own drop-in."""
        self.write(self.main, MAIN)
        for kp_name, rclone_name in (("50-kp-sync.toml", "90-rclone-config.toml"),
                                     ("90-kp-sync.toml", "10-rclone-config.toml")):
            with self.subTest(kp=kp_name, rclone=rclone_name):
                for f in self.dir.iterdir():
                    f.unlink()
                self.write(self.dir / kp_name, KP_SYNC)
                self.write(self.dir / rclone_name, RCLONE)
                cfg = C.load(self.main)
                self.assertEqual(run_order(cfg, MOUNTED), ["rclone-config-up", "kp-up"])
                self.assertEqual(run_order(cfg, UNMOUNTED), ["kp-down", "rclone-config-down"])


class ParseTests(unittest.TestCase):
    def config(self, *orders):
        return C.parse({"devices": {"d": {"match": {"fs_uuid": "U"}}},
                        "actions": [{"name": f"a{i}", "device": "d", "on": ["mounted"],
                                     "run": ["/bin/true"],
                                     **({} if o is None else {"order": o})}
                                    for i, o in enumerate(orders)]})

    def test_without_order_config_order(self):
        cfg = self.config(None, None, None)
        self.assertEqual([a.order for a in cfg.actions], [50, 50, 50])
        self.assertEqual(run_order(cfg, MOUNTED), ["a0", "a1", "a2"])

    def test_sorted_and_ties_keep_config_order(self):
        cfg = self.config(60, None, -5, 50, 10)
        self.assertEqual(run_order(cfg, MOUNTED), ["a2", "a4", "a1", "a3", "a0"])

    def test_explain_shows_the_same_order(self):
        cfg = self.config(60, None, 10)
        lines = explain.explain_endpoint(cfg, THING, [MOUNTED])
        runs = [line.split("'")[1] for line in lines if " runs " in line]
        self.assertEqual(runs, ["a2", "a1", "a0"])
        self.assertTrue(any("order 10" in line for line in lines))
        self.assertFalse(any("order 50" in line for line in lines))

    def test_bad_values(self):
        for bad in ("10", 1.5, True):
            with self.subTest(bad), self.assertRaises(C.ConfigError) as e:
                self.config(bad)
            self.assertIn("'order' must be a whole number", str(e.exception))


class EditTests(Files):
    def setUp(self):
        super().setUp()
        self.write(self.main, MAIN)

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(["--config", str(self.main), *args])
        return rc, out.getvalue(), err.getvalue()

    def test_add_set_and_list(self):
        self.assertEqual(self.cli("actions", "add", "--tag", "keepass", "mounted", "--name",
                                  "late", "--no-reload", "--", "/bin/true")[0], 0)
        rc, _, err = self.cli("actions", "add", "--tag", "rclone", "mounted", "--name",
                              "early", "--order", "10", "--no-reload", "--", "/bin/true")
        self.assertEqual(rc, 0, err)
        self.assertIn("order = 10", self.main.read_text())
        out = self.cli("actions", "list")[1].splitlines()
        self.assertEqual([line.split()[0] for line in out], ["early", "late"])
        self.assertIn("[order: 10]", out[0])
        self.assertNotIn("order", out[1])
        E.set_action_options(self.main, "early", order=50)            # the default: no key
        self.assertNotIn("order", self.main.read_text())
        self.assertEqual(self.cli("actions", "set", "late", "--order", "5", "--no-reload")[0], 0)
        self.assertEqual([a.order for a in C.load(self.main).actions], [5, 50])

    def test_add_with_the_default_writes_no_key(self):
        E.add_action(self.main, "x", [], ["keepass"], ["mounted"], ["/bin/true"], order=50)
        self.assertNotIn("order", self.main.read_text())


if __name__ == "__main__":
    unittest.main()
