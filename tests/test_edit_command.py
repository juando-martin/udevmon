# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`udevmon edit`: edit a copy, check every file, then save (or not)."""

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli, edit

MAIN = '[devices.key]\nmatch = { fs_uuid = "A", drive_serial = "S" }\ntags = ["t"]\n'
DROPIN = '[[actions]]\nname = "sync"\ntag = "t"\non = ["mounted"]\nrun = ["/bin/true"]\n'


def editor(*texts):
    """A fake editor that writes each text in turn (one per opening) and returns 0."""
    texts = list(texts)
    seen = []

    def run(path):
        seen.append(path.read_text())
        path.write_text(texts.pop(0))
        return 0
    run.seen = seen
    return run


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.main = self.dir / "config.toml"
        self.main.write_text(MAIN)
        (self.dir / "config.d").mkdir(mode=0o700)
        self.dropin = self.dir / "config.d" / "50-tool.toml"
        self.dropin.write_text(DROPIN)
        for f in (self.main, self.dropin):
            os.chmod(f, 0o600)

    def edit(self, target, run, ask=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            result = edit.edit_file(self.main, target, editor=run, ask=ask)
        return result, out.getvalue(), err.getvalue()

    def leftovers(self):
        return sorted(p.name for p in self.dir.rglob("*"))


class EditTests(Base):
    def test_a_valid_change_is_saved_with_a_backup(self):
        new = DROPIN.replace("/bin/true", "/bin/false")
        run = editor(new)
        (rc, saved), out, _ = self.edit(self.dropin, run)
        self.assertEqual((rc, saved), (0, True))
        self.assertEqual(run.seen, [DROPIN])                   # the editor got the file
        self.assertEqual(self.dropin.read_text(), new)
        self.assertEqual((self.dir / "config.d" / "50-tool.toml.bak").read_text(), DROPIN)
        self.assertEqual(stat.S_IMODE(self.dropin.stat().st_mode), 0o600)
        self.assertIn(f"Saved {self.dropin} (the old version is in 50-tool.toml.bak).", out)

    def test_no_change(self):
        (rc, saved), out, _ = self.edit(self.main, editor(MAIN))
        self.assertEqual((rc, saved, out), (0, False, "No changes.\n"))
        self.assertNotIn("config.toml.bak", self.leftovers())

    def test_the_check_covers_every_file(self):
        """Renaming the tag in the main file breaks nothing there, but the drop-in's
        action then targets a tag nobody carries: a warning, still saved."""
        (rc, saved), _, err = self.edit(self.main, editor(MAIN.replace('"t"', '"u"')))
        self.assertEqual((rc, saved), (0, True))
        self.assertIn("warning: action 'sync': tag 't' is carried by no device", err)

    def test_errors_offer_another_try(self):
        bad = MAIN + "[devices.key]\n"                          # a duplicate table
        good = MAIN.replace('"A"', '"B"')
        run = editor(bad, good)
        questions = []
        (rc, saved), _, err = self.edit(self.main, run, ask=lambda q: questions.append(q) or "e")
        self.assertEqual((rc, saved), (0, True))
        self.assertEqual(len(questions), 1)
        self.assertEqual(run.seen, [MAIN, bad])                 # the second try starts from it
        self.assertIn("error:", err)
        self.assertEqual(self.main.read_text(), good)

    def test_an_error_names_the_real_file_not_the_copy(self):
        bad = DROPIN.replace('tag = "t"', 'device = "nope"')
        (rc, saved), _, err = self.edit(self.dropin, editor(bad), ask=lambda q: "d")
        self.assertEqual((rc, saved), (1, False))
        self.assertIn("nope", err)
        self.assertNotIn("udevmon-edit", err)
        self.assertIn("Discarded", _)

    def test_without_a_terminal_errors_discard(self):
        (rc, saved), out, _ = self.edit(self.main, editor("not toml ["))
        self.assertEqual((rc, saved), (1, False))
        self.assertEqual(self.main.read_text(), MAIN)
        self.assertEqual(self.leftovers(), ["50-tool.toml", "config.d", "config.toml"])

    def test_ctrl_d_at_the_question_discards(self):
        def eof(_):
            raise EOFError
        (rc, saved), _, _ = self.edit(self.main, editor("x = ["), ask=eof)
        self.assertEqual((rc, saved), (1, False))

    def test_a_failing_editor_saves_nothing(self):
        def broken(path):
            path.write_text(MAIN + "# half done\n")
            return 1
        (rc, saved), out, _ = self.edit(self.main, broken)
        self.assertEqual((rc, saved), (1, False))
        self.assertIn("exited with status 1", out)
        self.assertEqual(self.main.read_text(), MAIN)

    def test_a_change_made_meanwhile_is_not_overwritten(self):
        def meanwhile(path):
            self.dropin.write_text(DROPIN + "# written by the tool\n")
            path.write_text(DROPIN.replace("/bin/true", "/bin/false"))
            return 0
        (rc, saved), _, err = self.edit(self.dropin, meanwhile)
        self.assertEqual((rc, saved), (1, False))
        self.assertIn("written by the tool", self.dropin.read_text())
        kept = self.dir / "config.d" / "50-tool.toml.edit"
        self.assertIn("/bin/false", kept.read_text())
        self.assertIn(str(kept), err)
        self.assertEqual(stat.S_IMODE(kept.stat().st_mode), 0o600)

    def test_a_new_dropin(self):
        target = self.dir / "config.d" / "60-new.toml"
        text = '[devices.other]\nmatch = { fs_uuid = "Z" }\n'
        (rc, saved), out, _ = self.edit(target, editor(text))
        self.assertEqual((rc, saved), (0, True))
        self.assertEqual(target.read_text(), text)
        self.assertNotIn(".bak", out)

    def test_an_untouched_new_file_is_not_created(self):
        target = self.dir / "config.d" / "60-new.toml"
        (rc, saved), out, _ = self.edit(target, editor(""))
        self.assertEqual((rc, saved, target.exists()), (0, False, False))


class EditorChoiceTests(unittest.TestCase):
    def test_visual_then_editor_then_a_default(self):
        self.assertEqual(edit.editor_command({"VISUAL": "code --wait", "EDITOR": "nano"}),
                         ["code", "--wait"])
        self.assertEqual(edit.editor_command({"EDITOR": "nano -w"}), ["nano", "-w"])
        with mock.patch("shutil.which", return_value=None):
            self.assertEqual(edit.editor_command({"EDITOR": "  "}), ["vi"])
        with mock.patch("shutil.which", return_value="/usr/bin/editor"):
            self.assertEqual(edit.editor_command({}), ["editor"])


class CommandTests(Base):
    def run_cli(self, *args, editor_script):
        """`udevmon edit` with $EDITOR a real script: the whole path, from the CLI."""
        script = self.dir / "fake-editor"
        script.write_text("#!/bin/sh\n" + editor_script)
        script.chmod(0o700)
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, {"VISUAL": str(script)}), \
                mock.patch("udevmon.daemon.reload_running", return_value=True) as reload, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.main), "edit", *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue(), reload

    def test_edit_a_dropin_and_reload(self):
        rc, out, _, reload = self.run_cli("50-tool", editor_script="sed -i s/true/false/ \"$1\"\n")
        self.assertEqual(rc, 0)
        self.assertIn("/bin/false", self.dropin.read_text())
        self.assertIn("Reloaded the running daemon.", out)
        reload.assert_called_once()

    def test_no_reload_without_a_change(self):
        rc, out, _, reload = self.run_cli(editor_script="exit 0\n")
        self.assertEqual((rc, out), (0, "No changes.\n"))
        reload.assert_not_called()

    def test_no_reload_option(self):
        rc, _, _, reload = self.run_cli("--no-reload", editor_script="echo '# hi' >> \"$1\"\n")
        self.assertEqual(rc, 0)
        reload.assert_not_called()

    def test_a_file_that_would_not_be_read_is_refused(self):
        rc, _, err, _ = self.run_cli("/etc/hosts", editor_script="exit 0\n")
        self.assertEqual(rc, 1)
        self.assertIn("error: edit /etc/hosts: only", err)


if __name__ == "__main__":
    unittest.main()
