import contextlib
import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_engine_actions import FakeRunner
from udevmon import actions as A
from udevmon import cli
from udevmon import config as C
from udevmon import configedit as E
from udevmon.config import tomllib
from udevmon.engine import Engine
from udevmon.events import MOUNTED, SHUTDOWN, STARTUP, Endpoint, Event
from udevmon.matching import matches, resolve, strength


def parse(text):
    return C.parse(tomllib.loads(text))


BASE = """
[devices.backup-disk]
match = { fs_uuid = "U-BACKUP" }
tags = ["backup"]

[devices.data-disk]
match = { fs_uuid = "U-DATA" }

[tags.storage]
match = { fs_type = ["ext4", "exfat"] }
"""


class RuleMatchingTests(unittest.TestCase):
    def test_list_values_are_any_of(self):
        rule = {"fs_type": ("ext4", "exfat")}
        self.assertTrue(matches(rule, {"fs_type": "EXFAT"}))
        self.assertFalse(matches(rule, {"fs_type": "vfat"}))
        self.assertFalse(matches(rule, {}))

    def test_list_values_never_count_as_identity(self):
        self.assertEqual(strength({"fs_uuid": ("a", "b")}), "weak")
        self.assertEqual(strength({"fs_uuid": ("a",)}), "strong")

    def test_resolve(self):
        cfg = parse(BASE)
        r = resolve(cfg, {"fs_uuid": "U-BACKUP", "fs_type": "ext4"})
        self.assertEqual(([d.name for d in r.named], r.tags), (["backup-disk"], ["backup", "storage"]))
        r = resolve(cfg, {"fs_uuid": "other", "fs_type": "exfat"})
        self.assertEqual((r.named, r.tags, r.is_named), ([], ["storage"], False))
        r = resolve(cfg, {"fs_uuid": "other", "fs_type": "vfat"})
        self.assertEqual((r.named, r.tags), ([], []))
        r = resolve(cfg, {"fs_uuid": "U-DATA", "fs_type": "vfat"})
        self.assertEqual(([d.name for d in r.named], r.tags), (["data-disk"], []))


class ValidationTests(unittest.TestCase):
    def bad(self, text):
        with self.assertRaises(C.ConfigError) as cm:
            parse(text)
        return "; ".join(cm.exception.problems)

    def act(self, body):
        return BASE + '\n[[actions]]\non = ["mounted"]\nrun = ["true"]\n' + body

    def test_exactly_one_target(self):
        self.assertIn("exactly one", self.bad(self.act('device = "data-disk"\ntag = "backup"')))
        self.assertIn("exactly one", self.bad(self.act('')))
        self.assertIn("unknown device", self.bad(self.act('device = ["data-disk", "nope"]')))
        self.assertIn("name or a non-empty list", self.bad(self.act('tag = []')))

    def test_allow_unnamed_rules(self):
        self.assertIn("only applies", self.bad(self.act('device = "data-disk"\nallow_unnamed = true')))
        self.assertIn("true or false", self.bad(self.act('tag = "storage"\nallow_unnamed = "yes"')))

    def test_rule_errors(self):
        self.assertIn("non-empty list", self.bad('[tags.x]\nmatch = { fs_type = [] }'))
        self.assertIn("changes between plugs", self.bad('[tags.x]\nmatch = { mountpoint = "/m" }'))
        self.assertIn("changes between plugs", self.bad('[tags.x]\nmatch = { udev_devname = "/dev/x" }'))
        self.assertIn("string", self.bad('[tags.x]\nmatch = { fs_type = [1.5] }'))
        self.assertIn("invalid name", self.bad('[tags."a b"]\nmatch = { fs_type = "x" }'))

    def test_tag_placeholder_only_for_tag_actions(self):
        self.assertIn("only available", self.bad(self.act('device = "data-disk"').replace(
            'run = ["true"]', 'run = ["echo", "{tag}"]')))
        cfg = parse(self.act('tag = "backup"').replace('run = ["true"]', 'run = ["echo", "{tag}", "{device}"]'))
        self.assertEqual(cfg.actions[0].tags, ("backup",))

    def test_device_and_tag_lists_normalised(self):
        cfg = parse(self.act('tag = ["backup", "storage", "backup"]'))
        self.assertEqual(cfg.actions[0].tags, ("backup", "storage"))
        self.assertEqual(cfg.actions[0].targets, ("backup", "storage"))

    def test_notes_and_warnings(self):
        cfg = parse(self.act('tag = "storage"'))                       # rule tag, not stated
        self.assertTrue(any("allow_unnamed" in n for n in cfg.notes))
        cfg = parse(self.act('tag = "storage"\nallow_unnamed = false'))  # decided: silent
        self.assertFalse(any("Set allow_unnamed" in n for n in cfg.notes))
        cfg = parse(self.act('tag = "backup"'))                        # explicit-only tag: no note
        self.assertFalse(cfg.notes)
        cfg = parse(self.act('tag = "storage"\nallow_unnamed = true'))
        self.assertTrue(any("not strong" in n for n in cfg.notes))
        cfg = parse(self.act('tag = "ghost"'))
        self.assertTrue(any("never runs" in w for w in cfg.warnings))

    def test_allow_unnamed_placeholder_warnings(self):
        text = self.act('tag = "storage"\nallow_unnamed = true').replace(
            'run = ["true"]', 'run = ["echo", "{device}", "{fs_label}", "{mountpoint}", "{tag}"]')
        cfg = parse(text)
        joined = " ".join(cfg.warnings)
        self.assertIn("{device}", joined)
        self.assertIn("fs_label", joined)
        self.assertIn("mountpoint", joined)
        self.assertNotIn("'tag'", joined)

    def test_unused_tag_note_and_volatile_device_key(self):
        cfg = parse('[devices.d]\nmatch = { devnode = "/dev/sdb1" }\ntags = ["lonely"]')
        self.assertTrue(any("lonely" in n for n in cfg.notes))
        self.assertTrue(any("volatile" in w for w in cfg.warnings))


class GateTests(unittest.TestCase):
    """Spec 5.5/5.6/7: who may run which actions."""

    def setUp(self):
        self.now = 100.0
        self.runner = FakeRunner()

    def engine(self, actions, debounce=1.0):
        cfg = parse(BASE + f"\n[daemon]\ndebounce = {debounce}\n" + actions) \
            if False else parse(f"[daemon]\ndebounce = {debounce}\n" + BASE + actions)
        return Engine(cfg, self.runner, clock=lambda: self.now)

    def ev(self, kind=MOUNTED, key="k", **props):
        return Event(kind, Endpoint("udisks", key, props))

    ACTIONS = """
[[actions]]
name = "open"
tag = "storage"
on = ["mounted", "startup", "shutdown"]
allow_unnamed = true
run = ["true"]

[[actions]]
name = "closed"
tag = "storage"
on = ["mounted"]
allow_unnamed = false
run = ["true"]

[[actions]]
name = "unstated"
tag = "storage"
on = ["mounted"]
run = ["true"]
"""

    def test_unnamed_thing_runs_only_allowed_actions(self):
        eng = self.engine(self.ACTIONS)
        jobs = eng.handle(self.ev(fs_uuid="stranger", fs_type="ext4"))
        self.assertEqual([(j.action.name, j.device, j.tag) for j in jobs], [("open", None, "storage")])

    def test_named_thing_meeting_a_rule_runs_everything(self):
        eng = self.engine(self.ACTIONS)
        jobs = eng.handle(self.ev(fs_uuid="U-DATA", fs_type="ext4"))
        self.assertEqual([(j.action.name, j.device) for j in jobs],
                         [("open", "data-disk"), ("closed", "data-disk"), ("unstated", "data-disk")])

    def test_named_thing_not_matching_the_rule_is_untouched(self):
        eng = self.engine(self.ACTIONS)
        self.assertEqual(eng.handle(self.ev(fs_uuid="U-DATA", fs_type="vfat")), [])

    def test_startup_and_shutdown_follow_the_same_gate(self):
        eng = self.engine(self.ACTIONS)
        for kind in (STARTUP, SHUTDOWN):
            self.now += 10
            jobs = eng.handle(self.ev(kind, fs_uuid="stranger", fs_type="exfat"))
            self.assertEqual([j.action.name for j in jobs], ["open"], kind)

    def test_no_tags_no_named_means_nothing(self):
        eng = self.engine(self.ACTIONS)
        self.assertEqual(eng.handle(self.ev(fs_uuid="stranger", fs_type="vfat")), [])

    def test_device_lists_and_once_per_event(self):
        eng = self.engine("""
[[actions]]
name = "pair"
device = ["data-disk", "backup-disk"]
on = ["mounted"]
run = ["true"]

[[actions]]
name = "both-ways"
tag = ["backup", "storage"]
on = ["mounted"]
run = ["true"]
""")
        jobs = eng.handle(self.ev(fs_uuid="U-BACKUP", fs_type="ext4"))
        self.assertEqual([(j.action.name, j.device, j.tag) for j in jobs],
                         [("pair", "backup-disk", None), ("both-ways", "backup-disk", "backup")])

    def test_tag_placeholder_is_first_in_action_order(self):
        eng = self.engine("""
[[actions]]
name = "t"
tag = ["storage", "backup"]
on = ["mounted"]
run = ["true"]
""")
        jobs = eng.handle(self.ev(fs_uuid="U-BACKUP", fs_type="ext4"))
        self.assertEqual(jobs[0].tag, "storage")

    def test_debounce_is_per_thing(self):
        eng = self.engine(self.ACTIONS)
        a = self.ev(key="/obj/a", fs_uuid="x", fs_type="ext4")
        b = self.ev(key="/obj/b", fs_uuid="y", fs_type="ext4")
        eng.handle(a)
        self.assertEqual(eng.handle(a), [])           # same unnamed thing: debounced
        self.assertEqual(len(eng.handle(b)), 1)       # a different unnamed thing: not
        self.now += 5
        self.assertEqual(len(eng.handle(a)), 1)

    def test_several_named_devices_matching_is_tolerated(self):
        eng = self.engine("""
[devices.twin]
match = { fs_uuid = "U-DATA" }

[[actions]]
name = "n"
device = ["twin", "data-disk"]
on = ["mounted"]
run = ["true"]
""")
        jobs = eng.handle(self.ev(fs_uuid="U-DATA"))
        self.assertEqual([(j.action.name, j.device) for j in jobs], [("n", "data-disk")])


class RunTagActionTests(unittest.TestCase):
    def run_it(self, run, device, tag, **props):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out"
            action = C.Action("t", (), ("storage",), ("mounted",),
                              tuple(a.replace("OUT", str(out)) for a in run), 10, True)
            rc = A.run_action(action, Event(MOUNTED, Endpoint("udisks", "k", props)), device, tag)
            return rc, out.read_text() if out.exists() else None

    def test_tag_and_device_available(self):
        rc, text = self.run_it(["sh", "-c", 'echo "$1|$2|$UDEVMON_TAG|$UDEVMON_DEVICE" > OUT',
                                "sh", "{tag}", "{device}"], "disk", "storage")
        self.assertEqual((rc, text.strip()), (0, "storage|disk|storage|disk"))

    def test_unnamed_thing_has_no_device(self):
        rc, text = self.run_it(["sh", "-c", 'echo "[$UDEVMON_DEVICE][$UDEVMON_TAG]" > OUT'],
                               None, "storage")
        self.assertEqual((rc, text.strip()), (0, "[][storage]"))
        rc, text = self.run_it(["sh", "-c", 'echo "$1" > OUT', "sh", "{device}"], None, "storage")
        self.assertEqual((rc, text), (None, None))         # skipped, never run with an empty value


class ConfigEditTagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "config.toml"
        self.path.write_text("# my file\n" + BASE)
        self.path.chmod(0o600)

    def tags(self, name):
        return C.load(self.path).devices[name].tags

    def test_add_device_with_tags(self):
        E.add_device(self.path, "new", {"serial": "S9x7"}, "d", ["a", "b", "a"])
        self.assertEqual(self.tags("new"), ("a", "b"))

    def test_set_device_tags(self):
        E.set_device_tags(self.path, "data-disk", add=["x", "y"])          # inserts a line
        self.assertEqual(self.tags("data-disk"), ("x", "y"))
        E.set_device_tags(self.path, "data-disk", add=["z"], remove=["x"])
        self.assertEqual(self.tags("data-disk"), ("y", "z"))
        E.set_device_tags(self.path, "backup-disk", remove=["backup"])     # last one: line removed
        self.assertEqual(self.tags("backup-disk"), ())
        self.assertNotIn('tags = []', self.path.read_text())
        self.assertTrue(self.path.read_text().startswith("# my file"))
        self.assertEqual(C.load(self.path).devices["data-disk"].match, {"fs_uuid": "U-DATA"})

    def test_untag_errors(self):
        before = self.path.read_text()
        with self.assertRaises(C.ConfigError):
            E.set_device_tags(self.path, "data-disk", remove=["nope"])
        with self.assertRaises(C.ConfigError):
            E.set_device_tags(self.path, "ghost", add=["x"])
        with self.assertRaises(C.ConfigError):
            E.set_device_tags(self.path, "data-disk", add=["bad name"])
        self.assertEqual(self.path.read_text(), before)

    def test_multiline_tags_are_edited_in_place(self):
        self.path.write_text('[devices.d]\nmatch = { serial = "S9x7a" }\ntags = [\n  "a",\n  "b",\n]\n')
        E.set_device_tags(self.path, "d", add=["c"])
        self.assertEqual(self.tags("d"), ("a", "b", "c"))
        self.assertIn('tags = [\n  "a",\n  "b",\n  "c",\n]', self.path.read_text())   # layout kept
        E.set_device_tags(self.path, "d", remove=["a"])
        self.assertEqual(self.tags("d"), ("b", "c"))

    def test_tag_rules_and_actions(self):
        E.add_tag(self.path, "flash", {"vendor_id": ["0781", "058f"], "fs_type": "vfat"}, "usb sticks")
        rule = C.load(self.path).tags["flash"].match
        self.assertEqual(rule, {"vendor_id": ("0781", "058f"), "fs_type": ("vfat",)})
        with self.assertRaises(C.ConfigError):
            E.add_tag(self.path, "flash", {"vendor_id": "1"})
        name = E.add_action(self.path, None, [], ["flash"], ["mounted"], ["true"], None, True)
        self.assertEqual(name, "flash-mounted")
        action = [a for a in C.load(self.path).actions if a.name == name][0]
        self.assertEqual((action.tags, action.allow_unnamed), (("flash",), True))
        with self.assertRaises(C.ConfigError):
            E.remove_tag(self.path, "flash")                      # used by an action
        E.remove_action(self.path, name)
        E.remove_tag(self.path, "flash")
        self.assertNotIn("flash", C.load(self.path).tags)
        self.assertTrue(self.path.read_text().startswith("# my file"))
        with self.assertRaises(C.ConfigError):
            E.remove_tag(self.path, "backup")                     # only carried by a device

    def test_remove_device_blocked_by_tag_free_device_actions_only(self):
        E.add_action(self.path, "n", ["data-disk"], [], ["mounted"], ["true"])
        with self.assertRaises(C.ConfigError):
            E.remove_device(self.path, "data-disk")


class CliTagTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "config.toml"

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.cfg), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue()

    def test_flow(self):
        ok = lambda *a: self.assertEqual(self.run_cli(*a)[0], 0, a)
        ok("devices", "add", "disk", "--match", "fs_uuid=U1", "--tag", "backup")
        ok("devices", "tag", "disk", "extra", "more")
        ok("tags", "add", "storage", "--match", "fs_type=ext4", "--match", "fs_type=exfat",
           "--description", "my drives")
        self.assertEqual(C.load(self.cfg).tags["storage"].match, {"fs_type": ("ext4", "exfat")})
        ok("actions", "add", "--tag", "storage", "--tag", "backup", "mounted", "--allow-unnamed",
           "--name", "note", "--", "notify-send", "{tag}")
        ok("actions", "add", "--tag", "backup", "mounted", "--named-only", "--", "true")
        ok("actions", "add", "--device", "disk", "mounted", "--", "true")
        acts = {a.name: a for a in C.load(self.cfg).actions}
        self.assertEqual((acts["note"].tags, acts["note"].allow_unnamed), (("storage", "backup"), True))
        self.assertIs(acts["backup-mounted"].allow_unnamed, False)
        self.assertIsNone(acts["disk-mounted"].allow_unnamed)
        rc, out, _ = self.run_cli("tags", "list")
        self.assertIn("storage", out); self.assertIn("members: disk", out)
        self.assertIn("allow_unnamed", self.run_cli("tags", "show", "storage")[1])
        self.assertIn("backup", self.run_cli("devices", "show", "disk")[1])
        self.assertIn("tag:storage,backup", self.run_cli("actions", "list")[1])
        ok("devices", "untag", "disk", "extra")
        rc, out, _ = self.run_cli("check")
        self.assertEqual(rc, 0)
        self.assertIn("tag rule(s)", out)

    def test_check_prints_notes(self):
        self.run_cli("devices", "add", "d", "--match", "serial=S9x7")
        self.run_cli("tags", "add", "storage", "--match", "fs_type=ext4")
        self.run_cli("actions", "add", "--tag", "storage", "mounted", "--", "true")
        rc, out, _ = self.run_cli("check")
        self.assertEqual(rc, 0)
        self.assertIn("note:", out)
        self.assertIn("allow_unnamed", out)

    def test_argument_errors(self):
        self.run_cli("devices", "add", "d", "--match", "serial=S9x7", "--tag", "t")
        self.assertNotEqual(self.run_cli("devices", "add", "e", "--match", "fs_type=a", "--match", "fs_type=b")[0], 0)
        self.assertNotEqual(self.run_cli("actions", "add", "mounted", "--", "true")[0], 0)
        self.assertNotEqual(self.run_cli("actions", "add", "--device", "d", "--tag", "t", "mounted", "--", "true")[0], 0)
        self.assertNotEqual(self.run_cli("actions", "add", "--device", "d", "--allow-unnamed", "mounted", "--", "true")[0], 0)
        self.assertNotEqual(self.run_cli("actions", "add", "--tag", "t", "mounted", "--allow-unnamed", "--named-only", "--", "true")[0], 0)
        self.assertNotEqual(self.run_cli("tags", "remove", "t")[0], 0)   # implicit tag has no block
        self.assertNotEqual(self.run_cli("devices", "untag", "d", "nope")[0], 0)


class ScanPromptTests(unittest.TestCase):
    """The interactive naming flow, driven with scripted answers."""

    def test_name_several_things_with_tags(self):
        from udevmon.scan import Scanner
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.toml"
            scanner = Scanner(path, prompt=True)
            scanner.loop = mock.Mock()
            usb = Endpoint("udev", "/sys/1", {"vendor_id": "0781", "product_id": "5567",
                                              "serial": "4C530001", "vendor": "SanDisk"})
            fs = Endpoint("udisks", "/o/1", {"fs_uuid": "3F2A-91BC", "fs_label": "BK"})
            scanner.pending = {"a": usb, "b": fs}
            answers = iter(["1,2",
                "", "stick", "", "usb-storage", "",
                "", "stick-fs", "", "usb-storage,backup", ""])
            with mock.patch("builtins.input", lambda prompt="": next(answers)), \
                    contextlib.redirect_stdout(io.StringIO()):
                scanner._prompt_user()
            cfg = C.load(path)
            self.assertEqual(cfg.devices["stick"].tags, ("usb-storage",))
            self.assertEqual(cfg.devices["stick"].match["serial"], "4C530001")
            self.assertEqual(cfg.devices["stick-fs"].match, {"fs_uuid": "3F2A-91BC"})
            self.assertEqual(cfg.devices["stick-fs"].tags, ("usb-storage", "backup"))
            scanner.loop.quit.assert_not_called()


    def run_prompt(self, endpoints, answers):
        from udevmon.scan import Scanner
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = Path(d) / "config.toml"
        scanner = Scanner(path, prompt=True)
        scanner.loop = mock.Mock()
        scanner.pending = {str(i): e for i, e in enumerate(endpoints)}
        seq = iter(answers)
        prompts = []

        def fake_input(prompt=""):
            prompts.append(prompt)
            return next(seq)
        out = io.StringIO()
        with mock.patch("builtins.input", fake_input), contextlib.redirect_stdout(out):
            scanner._prompt_user()
        return path, out.getvalue(), prompts

    def test_wrong_property_asks_again_and_keeps_the_name(self):
        usb = Endpoint("udev", "/sys/1", {"vendor_id": "090c", "product_id": "1000",
                                          "serial": "1111222233334444", "vendor": "V"})
        # what happened for real: the next device's name typed at the properties prompt
        path, out, prompts = self.run_prompt([usb], ["1", "", "stick", "stick-fs", "", "usbtest", ""])
        self.assertIn("Not a property of this thing: stick-fs", out)
        cfg = C.load(path)
        self.assertEqual(cfg.devices["stick"].match["serial"], "1111222233334444")
        self.assertEqual(cfg.devices["stick"].tags, ("usbtest",))
        self.assertEqual(sum("Device name" in p for p in prompts), 1)   # not asked twice

    def test_typed_property_list_and_skip(self):
        usb = Endpoint("udev", "/sys/1", {"vendor_id": "090c", "product_id": "1000",
                                          "serial": "1111222233334444"})
        path, out, _ = self.run_prompt([usb], ["1", "", "s", "vendor_id,serial", "", ""])
        self.assertEqual(set(C.load(path).devices["s"].match), {"vendor_id", "serial"})
        path, out, _ = self.run_prompt([usb], ["1", "", "", ""])       # empty name: skip
        self.assertIn("Skipped.", out)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
