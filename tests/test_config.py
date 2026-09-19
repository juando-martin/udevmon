import os
import tempfile
import unittest
from pathlib import Path

from udevmon import config as C
from udevmon import configedit as E

SAMPLE = """\
# my comment
[daemon]
debounce = 0.5

[assets.disk]
description = "backup"
match = { fs_uuid = "3F2A-91BC" }

[[actions]]
name = "backup"
asset = "disk"
on = ["mounted"]
run = ["~/bin/backup.sh", "{mountpoint}"]
timeout = 30
"""


def write(dir_, text, mode=0o600):
    p = Path(dir_) / "config.toml"
    p.write_text(text)
    p.chmod(mode)
    return p


class ParseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_load_valid(self):
        cfg = C.load(write(self.tmp.name, SAMPLE))
        self.assertEqual(cfg.settings.debounce, 0.5)
        self.assertEqual(cfg.assets["disk"].match, {"fs_uuid": "3F2A-91BC"})
        self.assertEqual(cfg.actions[0].run, ("~/bin/backup.sh", "{mountpoint}"))
        self.assertEqual(cfg.actions[0].timeout, 30)

    def test_missing_file_is_empty(self):
        self.assertEqual(C.load(Path(self.tmp.name) / "nope.toml").assets, {})

    def test_permission_check(self):
        with self.assertRaises(C.ConfigError):
            C.load(write(self.tmp.name, SAMPLE, 0o666))

    def _bad(self, text):
        with self.assertRaises(C.ConfigError) as cm:
            C.parse(__import__("tomllib").loads(text))
        return "; ".join(cm.exception.problems)

    def test_validation_errors(self):
        self.assertIn("unknown asset", self._bad(
            '[[actions]]\nasset="x"\non=["mounted"]\nrun=["true"]'))
        base = '[assets.a]\nmatch={serial="1"}\n[[actions]]\nasset="a"\n'
        self.assertIn("'on' must", self._bad(base + 'on=["boom"]\nrun=["true"]'))
        self.assertIn("not available", self._bad(base + 'on=["connected"]\nrun=["x","{mountpoint}"]'))
        self.assertIn("unknown key", self._bad(base + 'on=["connected"]\nrun=["x"]\nrunn=1'))
        self.assertIn("non-empty table", self._bad('[assets.a]\nmatch={}'))
        self.assertIn("invalid name", self._bad('[assets."a b"]\nmatch={x="1"}'))

    def test_run_string_is_shlex_split(self):
        cfg = C.parse(__import__("tomllib").loads(
            '[assets.a]\nmatch={serial="S1x"}\n[[actions]]\nasset="a"\non="connected"\n'
            'run="notify-send \'hello world\'"'))
        self.assertEqual(cfg.actions[0].run, ("notify-send", "hello world"))
        self.assertEqual(cfg.actions[0].on, ("connected",))

    def test_weak_warning(self):
        cfg = C.parse({"assets": {"a": {"match": {"fs_label": "X"}}}})
        self.assertTrue(cfg.warnings)


class ExpandTests(unittest.TestCase):
    def test_expand(self):
        home = os.path.expanduser("~")
        self.assertEqual(C.expand(["~/x", "{mountpoint}/a b", "{{lit}}"], {"mountpoint": "/m"}),
                         [f"{home}/x", "/m/a b", "{lit}"])

    def test_missing_placeholder_raises(self):
        with self.assertRaises(KeyError):
            C.expand(["{mountpoint}"], {})

    def test_shell_braces_are_literal(self):
        argv = ["sh", "-c", "echo ${1:-none} ${HOME} ${mountpoint}; awk '{print $1}' {mountpoint}"]
        self.assertEqual(C.expand(argv, {"mountpoint": "/m"})[2],
                         "echo ${1:-none} ${HOME} ${mountpoint}; awk '{print $1}' /m")
        self.assertEqual(C.placeholders(argv[2]), ["mountpoint"])

    def test_value_with_braces_not_reinterpreted(self):
        self.assertEqual(C.expand(["{x}"], {"x": "{y}"}), ["{y}"])


class EditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = write(self.tmp.name, SAMPLE)

    def test_add_asset_preserves_comments(self):
        E.add_asset(self.path, "key", {"vendor_id": "1050", "serial": 'a"b'}, "My key")
        text = self.path.read_text()
        self.assertTrue(text.startswith("# my comment"))
        cfg = C.load(self.path)
        self.assertEqual(cfg.assets["key"].match["serial"], 'a"b')
        self.assertTrue(self.path.with_name("config.toml.bak").exists())

    def test_add_asset_to_new_file(self):
        p = Path(self.tmp.name) / "sub" / "c.toml"
        E.add_asset(p, "a", {"serial": "S1x"})
        self.assertIn("a", C.load(p).assets)
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)

    def test_duplicate_rejected_and_file_unchanged(self):
        before = self.path.read_text()
        with self.assertRaises(C.ConfigError):
            E.add_asset(self.path, "disk", {"fs_uuid": "z"})
        self.assertEqual(self.path.read_text(), before)

    def test_add_and_remove_action(self):
        name = E.add_action(self.path, None, "disk", ["unmounted"], ["echo", "{mountpoint}"], 5)
        self.assertEqual(name, "disk-unmounted")
        name2 = E.add_action(self.path, None, "disk", ["unmounted"], ["true"])
        self.assertEqual(name2, "disk-unmounted-2")
        E.remove_action(self.path, "disk-unmounted")
        names = [a.name for a in C.load(self.path).actions]
        self.assertEqual(names, ["backup", "disk-unmounted-2"])

    def test_remove_asset_blocked_when_used_then_ok(self):
        with self.assertRaises(C.ConfigError):
            E.remove_asset(self.path, "disk")
        E.remove_action(self.path, "backup")
        E.remove_asset(self.path, "disk")
        cfg = C.load(self.path)
        self.assertEqual((cfg.assets, cfg.actions), ({}, []))
        self.assertIn("# my comment", self.path.read_text())

    def test_invalid_action_rolls_back(self):
        before = self.path.read_text()
        with self.assertRaises(C.ConfigError):
            E.add_action(self.path, "bad", "disk", ["connected"], ["x", "{mountpoint}"])
        self.assertEqual(self.path.read_text(), before)


if __name__ == "__main__":
    unittest.main()
