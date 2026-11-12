# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""`check --json` findings: each error, warning and note with the entries it concerns."""

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from udevmon import cli
from udevmon import config as C


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.main = Path(tmp.name) / "config.toml"
        self.dropin = self.main.with_suffix(".d") / "50-tool.toml"

    def write(self, main="", dropin=None):
        self.main.write_text(main)
        self.main.chmod(0o600)
        if dropin is not None:
            self.dropin.parent.mkdir(mode=0o700, exist_ok=True)
            self.dropin.write_text(dropin)
            self.dropin.chmod(0o600)

    def check(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(["--config", str(self.main), "check", "--json"])
        return rc, json.loads(out.getvalue())

    def about(self, data, text):
        """The subjects of the one finding whose message contains ``text``."""
        (f,) = [f for f in data["findings"] if text in f["message"]]
        return f["level"], [(s["kind"], s["name"], s["dropin"], s["file"]) for s in f["subjects"]]


DEVICE = '[devices.{0}]\nmatch = {{ fs_uuid = "{1}" }}\n'


class LoadedTests(Base):
    def test_each_finding_says_which_entries_and_files(self):
        self.write(DEVICE.format("mine", "U") + 'status = "disabled"\n',
                   DEVICE.format("theirs", "U") + '[tags.solo]\nmatch = { fs_type = "x*" }\n'
                   '[[actions]]\nname = "a"\ntag = "solo"\non = ["mounted"]\n'
                   'allow_unnamed = true\nlock = "only"\nrun = ["true"]\n')
        rc, data = self.check()
        self.assertEqual(rc, 0)
        main, dropin = str(self.main), str(self.dropin)
        self.assertEqual(self.about(data, "'mine' is disabled"),
                         ("note", [("device", "mine", None, main)]))
        self.assertEqual(self.about(data, "have the same match"),
                         ("note", [("device", "mine", None, main),
                                   ("device", "theirs", "50-tool", dropin)]))
        self.assertEqual(self.about(data, "only one with lock"),
                         ("note", [("action", "a", "50-tool", dropin)]))
        level, subjects = self.about(data, "has a rule that is not strong")
        self.assertEqual(subjects, [("action", "a", "50-tool", dropin),
                                    ("tag", "solo", "50-tool", dropin)])

    def test_the_old_lists_are_the_findings_messages(self):
        self.write(DEVICE.format("a", "U") + 'status = "ignored"\n',
                   '[[actions]]\nname = "x"\ndevice = "a"\non = ["mounted"]\n'
                   'run = ["/no/such/program"]\n')
        rc, data = self.check()
        self.assertEqual([f["message"] for f in data["findings"] if f["level"] == "warning"],
                         data["warnings"])
        self.assertEqual([f["message"] for f in data["findings"] if f["level"] == "note"],
                         data["notes"])
        level, subjects = self.about(data, "/no/such/program")          # a command problem
        self.assertEqual((level, subjects[0][:3]), ("warning", ("action", "x", "50-tool")))

    def test_a_dropins_script_dir_that_is_not_a_folder(self):
        self.write("", '[daemon]\nscript_dir = "/no/such/dir"\n[devices.d]\n'
                       'match = { fs_uuid = "U" }\n[[actions]]\nname = "a"\ndevice = "d"\n'
                       'on = ["mounted"]\nrun = ["tool"]\n')
        rc, data = self.check()
        self.assertEqual(self.about(data, "'/no/such/dir' (of")[1][0][:3],
                         ("daemon", None, "50-tool"))

    def test_parse_without_files_still_names_the_entries(self):
        cfg = C.parse({"devices": {"d": {"match": {"fs_uuid": "U"}, "status": "disabled"}}})
        (f,) = cfg.findings
        self.assertEqual((f.level, f.subjects), ("note", (C.Subject("device", "d", None),)))
        self.assertEqual(cfg.notes, [f.message])


class ErrorTests(Base):
    def test_an_entry_error(self):
        self.write("", DEVICE.format("d", "U") + "bogus = 1\n")
        rc, data = self.check()
        self.assertEqual((rc, data["ok"]), (1, False))
        self.assertEqual(self.about(data, "unknown key 'bogus'"),
                         ("error", [("device", "d", "50-tool", str(self.dropin))]))
        self.assertEqual([f["message"] for f in data["findings"]], data["errors"])

    def test_an_action_error_before_and_after_its_name(self):
        self.write('[[actions]]\nname = "x"\ntag = "t"\non = ["nope"]\nrun = ["true"]\n'
                   '[[actions]]\ntag = 3\non = ["mounted"]\nrun = ["true"]\n')
        rc, data = self.check()
        self.assertEqual(self.about(data, "'on' must list")[1],
                         [("action", "x", None, str(self.main))])
        self.assertEqual(self.about(data, "'tag': must be")[1],
                         [("action", None, None, str(self.main))])

    def test_the_daemon_table(self):
        self.write("[daemon]\nmax_workers = 0\n")
        rc, data = self.check()
        self.assertEqual(self.about(data, "invalid debounce")[1],
                         [("daemon", None, None, str(self.main))])
        self.write("", "[daemon]\ndebounce = 2\n")
        rc, data = self.check()
        self.assertEqual(self.about(data, "may only have script_dir")[1],
                         [("daemon", None, "50-tool", str(self.dropin))])

    def test_defined_in_two_files(self):
        self.write(DEVICE.format("d", "U"), DEVICE.format("d", "V"))
        rc, data = self.check()
        self.assertEqual(self.about(data, "defined in both")[1],
                         [("device", "d", None, str(self.main)),
                          ("device", "d", "50-tool", str(self.dropin))])

    def test_a_file_error(self):
        self.write("not = [toml")
        rc, data = self.check()
        self.assertEqual(data["findings"][0]["subjects"],
                         [{"kind": "file", "name": None, "file": str(self.main), "dropin": None}])
        self.write("", "x = 1\n")
        rc, data = self.check()
        self.assertEqual(self.about(data, "unknown key 'x'")[1],
                         [("file", None, "50-tool", str(self.dropin))])

    def test_an_unsafe_file(self):
        self.write(DEVICE.format("d", "U"))
        self.main.chmod(0o666)
        rc, data = self.check()
        self.assertEqual(self.about(data, "group/world-writable")[1],
                         [("file", None, None, str(self.main))])

    def test_a_config_error_from_elsewhere_has_no_subjects(self):
        e = C.ConfigError(["something"])
        self.assertEqual(e.findings, [C.Finding("error", "something", ())])


if __name__ == "__main__":
    unittest.main()
