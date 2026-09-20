import tempfile
import tomllib
import unittest
from pathlib import Path

from udevmon import cli
from udevmon import config as C
from udevmon import configedit as E

LEGACY = """\
# keep me
[assets.disk]
description = "backup"   # asset comment
match = { fs_uuid = "3F2A-91BC" }

[[actions]]
name = "backup"
asset = "disk"
on = ["mounted"]
run = ["true"]
"""


class LegacyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text(LEGACY)
        self.path.chmod(0o600)

    def test_legacy_names_accepted_with_warning(self):
        cfg = C.load(self.path)
        self.assertTrue(cfg.deprecated)
        self.assertEqual(cfg.devices["disk"].match, {"fs_uuid": "3F2A-91BC"})
        self.assertEqual(cfg.actions[0].device, "disk")
        self.assertTrue(any("migrate" in w for w in cfg.warnings))

    def test_mixing_old_and_new_is_an_error(self):
        with self.assertRaises(C.ConfigError):
            C.parse(tomllib.loads('[assets.a]\nmatch={serial="1"}\n[devices.b]\nmatch={serial="2"}'))
        with self.assertRaises(C.ConfigError):
            C.parse(tomllib.loads('[devices.a]\nmatch={serial="1"}\n[[actions]]\n'
                                  'asset="a"\ndevice="a"\non="mounted"\nrun="true"'))

    def test_edits_refused_until_migrated(self):
        before = self.path.read_text()
        with self.assertRaises(C.ConfigError) as cm:
            E.add_device(self.path, "x", {"serial": "S1x"})
        self.assertIn("migrate", str(cm.exception))
        self.assertEqual(self.path.read_text(), before)

    def test_migrate_preserves_comments_and_meaning(self):
        old = C.load(self.path)
        self.assertTrue(E.migrate(self.path))
        text = self.path.read_text()
        self.assertIn("# keep me", text)
        self.assertIn("# asset comment", text)        # comments are left as written
        self.assertIn("[devices.disk]", text)
        self.assertIn('device = "disk"', text)
        new = C.load(self.path)
        self.assertFalse(new.deprecated)
        self.assertEqual((new.devices, new.actions), (old.devices, old.actions))
        self.assertTrue(self.path.with_name("config.toml.bak").exists())
        self.assertFalse(E.migrate(self.path))         # idempotent
        E.add_device(self.path, "x", {"serial": "S1x"})  # editing works again

    def test_cli_migrate_and_assets_alias(self):
        import contextlib, io
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.main(["--config", str(self.path), "migrate"]), 0)
            self.assertEqual(cli.main(["--config", str(self.path), "assets", "list"]), 0)
            self.assertEqual(cli.main(["--config", str(self.path), "devices"]), 0)
        self.assertIn("Migrated", out.getvalue())
        self.assertIn("deprecated", err.getvalue())
        self.assertIn("disk", out.getvalue())


if __name__ == "__main__":
    unittest.main()
