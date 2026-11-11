# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Drop-in files: config.d/*.toml next to config.toml, read after it in file-name order."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli, completion
from udevmon import config as C
from udevmon import configedit as E
from udevmon import daemon
from udevmon.engine import Engine
from udevmon.events import MOUNTED, Endpoint, Event

MAIN = """\
[devices.key]
match = { fs_uuid = "U" }
tags = ["keepass"]

[[actions]]
name = "main-up"
device = "key"
on = ["mounted"]
run = ["/bin/true", "main"]
"""

KP = """\
# owned by kp-sync
[devices.key2]
match = { fs_uuid = "U2" }
tags = ["keepass"]

[[actions]]
name = "kp-up"
tag = "keepass"
on = ["mounted"]
run = ["/bin/true", "kp"]
"""

RCLONE = """\
[[actions]]
name = "rclone-up"
device = "key"
on = ["mounted"]
run = ["/bin/true", "rclone"]
"""


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.main = self.root / "config.toml"
        self.dir = self.root / "config.d"

    def write(self, path, text, mode=0o600):
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.write_text(text)
        os.chmod(path, mode)
        return path

    def standard(self):
        self.write(self.main, MAIN)
        self.write(self.dir / "90-rclone.toml", RCLONE)       # written first, read last
        self.write(self.dir / "50-kp.toml", KP)

    def load(self):
        return C.load(self.main)

    def problems(self):
        with self.assertRaises(C.ConfigError) as e:
            self.load()
        return e.exception.problems


class LoadTests(Base):
    def test_only_the_main_file_is_as_before(self):
        self.write(self.main, MAIN)
        cfg = self.load()
        self.assertEqual(cfg.files, [self.main])
        self.assertEqual(cfg.label("device", "key"), "")

    def test_merged_in_file_name_order_after_the_main_file(self):
        self.standard()
        cfg = self.load()
        self.assertEqual(cfg.files, [self.main, self.dir / "50-kp.toml",
                                     self.dir / "90-rclone.toml"])
        self.assertEqual(list(cfg.devices), ["key", "key2"])
        self.assertEqual([a.name for a in cfg.actions], ["main-up", "kp-up", "rclone-up"])
        self.assertEqual(cfg.label("action", "rclone-up"), "config.d/90-rclone.toml")
        self.assertEqual(cfg.label("device", "key2"), "config.d/50-kp.toml")
        self.assertEqual(cfg.label("action", "main-up"), "")

    def test_execution_follows_that_order(self):
        self.standard()
        engine = Engine(self.load(), runner=mock.Mock(), paused=lambda: False)
        jobs = engine.handle(Event(MOUNTED, Endpoint("udisks", "k", {"fs_uuid": "U"})))
        self.assertEqual([j.action.name for j in jobs], ["main-up", "kp-up", "rclone-up"])

    def test_references_cross_files(self):
        self.standard()                              # rclone-up uses device key of config.toml
        self.assertEqual(self.load().actions[2].devices, ("key",))

    def test_drop_ins_without_a_main_file(self):
        self.write(self.dir / "50-kp.toml", KP)
        cfg = C.load(self.main, must_exist=True)
        self.assertEqual(list(cfg.devices), ["key2"])

    def test_other_files_are_not_read(self):
        self.standard()
        for name in (".hidden.toml", "50-kp.toml.bak", "notes.txt", "x.toml.tmp"):
            self.write(self.dir / name, "this is not toml [")
        self.assertEqual(len(self.load().files), 3)

    def test_another_config_name_has_its_own_folder(self):
        other = self.write(self.root / "my.toml", MAIN)
        self.write(self.root / "my.d" / "x.toml", RCLONE)
        self.assertEqual(len(C.load(other).actions), 2)


class ProblemTests(Base):
    def test_a_name_in_two_files_names_both(self):
        self.standard()
        for kind, text in (("device", '[devices.key]\nmatch = { fs_uuid = "V" }\n'),
                           ("tag", '[tags.t]\n'),
                           ("action", RCLONE)):
            with self.subTest(kind):
                if kind == "tag":
                    self.write(self.main, MAIN + '\n[tags.t]\n')
                self.write(self.dir / "70-clash.toml", text)
                [problem] = self.problems()
                self.assertIn(f"{kind} ", problem)
                self.assertIn("70-clash.toml", problem)
                self.assertTrue("config.toml" in problem or "90-rclone.toml" in problem)
                self.write(self.main, MAIN)

    def test_daemon_settings_only_in_the_main_file(self):
        self.standard()
        self.write(self.dir / "70-x.toml", "[daemon]\ndebounce = 2\n")
        self.assertIn("a drop-in's [daemon] may only have script_dir", self.problems()[0])

    def test_problems_inside_a_drop_in_say_where(self):
        self.standard()
        self.write(self.dir / "70-x.toml", RCLONE.replace("rclone-up", "x") + """
[[actions]]
name = "bad"
device = "nope"
on = ["mounted"]
run = ["/bin/true"]
""")
        [problem] = self.problems()
        self.assertTrue(problem.startswith("config.d/70-x.toml: [[actions]] #2"), problem)

    def test_main_file_messages_are_unchanged(self):
        self.write(self.main, '[devices.x]\nmatch = 3\n')
        self.assertEqual(self.problems(), ["[devices.x]: 'match' must be a non-empty table"])

    def test_permissions(self):
        self.standard()
        os.chmod(self.dir / "50-kp.toml", 0o620)
        self.assertIn("group/world-writable", self.problems()[0])
        os.chmod(self.dir / "50-kp.toml", 0o600)
        os.chmod(self.dir, 0o770)
        self.assertIn("group/world-writable", self.problems()[0])

    def test_a_failed_reload_keeps_the_previous_configuration(self):
        self.standard()
        d = daemon.Daemon(self.main)
        d.cfg = self.load()
        d.engine = mock.Mock()
        self.write(self.dir / "70-clash.toml", RCLONE)
        with self.assertLogs("udevmon.daemon", "ERROR"):
            d.reload()
        self.assertEqual(len(d.cfg.actions), 3)
        d.engine.set_config.assert_not_called()


class FileOptionTests(Base):
    def test_ways_to_name_a_drop_in(self):
        want = self.dir / "50-kp.toml"
        for given in ("50-kp", "50-kp.toml", "config.d/50-kp.toml", str(want)):
            self.assertEqual(C.dropin_path(self.main, given), want, given)
        self.assertEqual(C.dropin_path(self.main, "config.toml"), self.main)
        for bad in ("../x", "/etc/x.toml", "a/b.toml", ".hidden", "config.d/a/b.toml"):
            with self.subTest(bad), self.assertRaises(C.ConfigError):
                C.dropin_path(self.main, bad)


class EditTests(Base):
    def setUp(self):
        super().setUp()
        self.standard()
        self.kp = self.dir / "50-kp.toml"
        self.rclone = self.dir / "90-rclone.toml"

    def test_add_to_a_new_drop_in(self):
        E.add_device(self.main, "key3", {"fs_uuid": "U3"}, file="60-new")
        new = self.dir / "60-new.toml"
        self.assertEqual(new.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.load().label("device", "key3"), "config.d/60-new.toml")
        self.assertEqual(self.main.read_text(), MAIN)

    def test_add_refuses_a_name_used_in_another_file(self):
        with self.assertRaises(C.ConfigError) as e:
            E.add_device(self.main, "key2", {"fs_uuid": "U9"})
        self.assertIn("50-kp.toml", str(e.exception))
        self.assertEqual(self.main.read_text(), MAIN)

    def test_existing_entries_are_edited_where_they_are(self):
        E.set_device_fields(self.main, "key2", description="kp drive")
        E.set_action_options(self.main, "rclone-up", timeout=30)
        self.assertEqual(self.main.read_text(), MAIN)
        self.assertIn('description = "kp drive"', self.kp.read_text())
        self.assertIn("# owned by kp-sync", self.kp.read_text())
        self.assertIn("timeout = 30", self.rclone.read_text())
        E.remove_action(self.main, "rclone-up")
        self.assertNotIn("rclone-up", self.rclone.read_text())
        self.assertEqual(self.main.read_text(), MAIN)

    def test_several_files_in_one_command(self):
        E.set_device_status(self.main, ["key", "key2"], "disabled")
        self.assertIn('status = "disabled"', self.main.read_text())
        self.assertIn('status = "disabled"', self.kp.read_text())
        E.set_action_enabled(self.main, ["main-up", "kp-up"], False)
        self.assertEqual({a.name: a.enabled for a in self.load().actions},
                         {"main-up": False, "kp-up": False, "rclone-up": True})

    def test_a_rename_fixes_references_in_every_file(self):
        report = E.rename_device(self.main, "key", "daily")
        self.assertIn("[devices.daily]", self.main.read_text())
        self.assertIn('device = "daily"', self.rclone.read_text())
        self.assertEqual(sorted(report.updated), ["main-up", "rclone-up"])
        E.rename_tag(self.main, "keepass", "kp")
        cfg = self.load()
        self.assertEqual(cfg.devices["daily"].tags, ("kp",))
        self.assertEqual(cfg.devices["key2"].tags, ("kp",))
        self.assertEqual(cfg.actions[1].tags, ("kp",))
        E.rename_action(self.main, "kp-up", "kp-sync-up")
        self.assertIn('name = "kp-sync-up"', self.kp.read_text())

    def test_a_rename_reports_mentions_in_drop_ins(self):
        self.write(self.kp, KP + "# the key drive is also used by rclone\n")
        report = E.rename_device(self.main, "key", "daily")
        self.assertIn("config.d/50-kp.toml", [label for label, _, _ in report.elsewhere])

    def test_a_failure_rolls_back_every_file(self):
        before = {p: p.read_text() for p in (self.main, self.kp)}

        def fail(cfg):
            raise C.ConfigError(["no"])
        with self.assertRaises(C.ConfigError):
            E._commit(self.main, {self.main: MAIN + "\n# x\n", self.kp: KP + "\n# y\n"}, fail)
        self.assertEqual({p: p.read_text() for p in before}, before)


class CliTests(Base):
    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.main), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_where_entries_come_from_is_shown(self):
        self.standard()
        rc, out, _ = self.run_cli("check")
        self.assertEqual(rc, 0)
        self.assertIn("config.d/50-kp.toml: 1 device(s), 0 tag rule(s), 1 action(s)", out)
        out = self.run_cli("devices", "list")[1]
        self.assertIn("[config.d/50-kp.toml]", out.splitlines()[1])
        self.assertNotIn("[config", out.splitlines()[0])
        self.assertIn("[config.d/90-rclone.toml]", self.run_cli("actions", "list")[1])
        self.assertIn("file: config.d/50-kp.toml", self.run_cli("devices", "show", "key2")[1])
        out = self.run_cli("explain", "mounted", "--device", "key")[1]
        self.assertIn("from config.d/90-rclone.toml", out)

    def test_check_with_only_the_main_file_lists_no_files(self):
        self.write(self.main, MAIN)
        self.assertNotIn("Files", self.run_cli("check")[1])

    def test_a_clash_fails_check_naming_both_files(self):
        self.standard()
        self.write(self.dir / "70-clash.toml", RCLONE)
        rc, out, _ = self.run_cli("check")
        self.assertEqual(rc, 1)
        self.assertIn("in both config.d/70-clash.toml and config.d/90-rclone.toml", out)

    def test_add_with_file(self):
        self.write(self.main, MAIN)
        rc, _, err = self.run_cli("actions", "add", "--device", "key", "mounted", "--name",
                                  "kp-up", "--file", "config.d/50-kp-sync-beta.toml",
                                  "--no-reload", "--", "/bin/true")
        self.assertEqual(rc, 0, err)
        self.assertIn("kp-up", (self.dir / "50-kp-sync-beta.toml").read_text())
        rc, _, err = self.run_cli("tags", "add", "x", "--file", "../evil", "--no-reload")
        self.assertEqual(rc, 1)
        self.assertFalse((self.root / "evil.toml").exists())

    def test_completion_offers_the_drop_ins(self):
        self.standard()
        words = ["udevmon", "--config", str(self.main), "devices", "add", "--file", ""]
        _, offered = completion.complete(cli.build_parser(), words, len(words) - 1)
        self.assertEqual(offered, ["config.d/", "config.d/50-kp.toml",
                                   "config.d/90-rclone.toml"])


if __name__ == "__main__":
    unittest.main()
