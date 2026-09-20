"""Device status (disabled / ignored), action enabled, and `actions set`."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli, explain
from udevmon import config as C
from udevmon import configedit as E
from udevmon.engine import Engine
from udevmon.events import CONNECTED, MOUNTED, SHUTDOWN, STARTUP, Endpoint, Event
from udevmon.matching import device_label, resolve


def load(text):
    return C.parse(C.tomllib.loads(text))


def write(text):
    p = Path(tempfile.mkdtemp()) / "c.toml"
    p.write_text(text)
    p.chmod(0o600)
    return p


BASE = """
[devices.stick]
match = { serial = "S9x7a" }
tags = ["usbtest"]
{stick_status}

[devices.twin]
match = { vendor_id = "1", product_id = "2" }
{twin_status}

[tags.storage]
match = { fs_type = ["ext4"] }

[[actions]]
name = "own"
device = "stick"
on = ["connected", "startup", "shutdown"]
run = ["true"]

[[actions]]
name = "by-tag"
tag = "usbtest"
on = ["connected"]
allow_unnamed = false
run = ["true"]

[[actions]]
name = "open"
tag = "storage"
on = ["mounted"]
allow_unnamed = true
run = ["true"]

[[actions]]
name = "twin-own"
device = "twin"
on = ["connected"]
run = ["true"]
"""


def cfg(stick="", twin="", extra=""):
    return load(BASE.replace("{stick_status}", stick).replace("{twin_status}", twin) + extra)


STICK = Endpoint("udev", "/sys/1", {"serial": "S9x7a", "vendor_id": "1", "product_id": "2"})
UNKNOWN_FS = Endpoint("udisks", "/o/1", {"fs_type": "ext4", "fs_uuid": "zzz", "mountpoint": "/m"})


class ParseTests(unittest.TestCase):
    def test_status_values(self):
        for text, want in (("", "active"), ('status = ""', "active"), ('status = "active"', "active"),
                           ('status = "disabled"', "disabled"), ('status = "ignored"', "ignored")):
            with self.subTest(text):
                self.assertEqual(cfg(stick=text).devices["stick"].status, want)

    def test_bad_status_is_an_error(self):
        for bad in ('status = "paused"', "status = true", "status = 3", 'status = "Ignored"'):
            with self.subTest(bad):
                with self.assertRaises(C.ConfigError) as cm:
                    cfg(stick=bad)
                self.assertIn("'status' must be", str(cm.exception))

    def test_enabled_must_be_a_boolean(self):
        good = cfg(extra='\n[[actions]]\nname = "x"\ndevice = "stick"\non = ["connected"]\n'
                         'run = ["true"]\nenabled = false\n')
        self.assertFalse([a for a in good.actions if a.name == "x"][0].enabled)
        with self.assertRaises(C.ConfigError):
            cfg(extra='\n[[actions]]\nname = "x"\ndevice = "stick"\non = ["connected"]\n'
                      'run = ["true"]\nenabled = "no"\n')

    def test_notes_list_what_is_switched_off(self):
        c = cfg(stick='status = "disabled"', twin='status = "ignored"',
                extra='\n[[actions]]\nname = "off"\ndevice = "stick"\non = ["connected"]\n'
                      'run = ["true"]\nenabled = false\n')
        joined = "\n".join(c.notes)
        self.assertIn("device 'stick' is disabled", joined)
        self.assertIn("device 'twin' is ignored", joined)
        self.assertIn("action 'off' is disabled", joined)
        self.assertIn("action 'twin-own' only targets disabled or ignored devices", joined)
        self.assertIn("action 'own' only targets disabled or ignored devices", joined)   # stick is off
        self.assertFalse([n for n in cfg().notes if "disabled" in n or "ignored" in n])


class EngineMatrixTests(unittest.TestCase):
    def names(self, config, endpoint, kind=CONNECTED):
        plan = Engine(config, runner=None).plan(Event(kind, endpoint))
        return [j.action.name for j in plan.jobs], [(s.action.name, s.reason) for s in plan.skipped]

    def test_everything_active_is_unchanged(self):
        ran, skipped = self.names(cfg(), STICK)
        self.assertEqual((ran, skipped), (["own", "by-tag", "twin-own"], []))

    def test_a_disabled_device_loses_only_its_own_actions(self):
        ran, skipped = self.names(cfg(stick='status = "disabled"'), STICK)
        self.assertEqual(ran, ["by-tag", "twin-own"])        # the tag action still runs
        self.assertEqual(skipped, [("own", "device 'stick' is disabled")])

    def test_a_disabled_device_is_still_named(self):
        """It must not fall back into 'unnamed', where allow_unnamed = false would stop it."""
        ran, _ = self.names(cfg(stick='status = "disabled"'), STICK)
        self.assertIn("by-tag", ran)
        unnamed = Endpoint("udev", "/sys/2", {"serial": "other"})       # same tag would not apply at all
        self.assertEqual(self.names(cfg(stick='status = "disabled"'), unnamed)[0], [])
        self.assertTrue(resolve(cfg(stick='status = "disabled"'), STICK.props).is_named)

    def test_an_ignored_device_silences_everything(self):
        ran, skipped = self.names(cfg(stick='status = "ignored"'), STICK)
        self.assertEqual(ran, [])                             # twin-own too: ignore wins on overlap
        for kind in (STARTUP, SHUTDOWN):
            self.assertEqual(self.names(cfg(stick='status = "ignored"'), STICK, kind)[0], [])

    def test_ignore_wins_when_two_devices_match(self):
        ran, _ = self.names(cfg(twin='status = "ignored"'), STICK)     # stick active, twin ignored
        self.assertEqual(ran, [])

    def test_disabled_never_conflicts(self):
        ran, skipped = self.names(cfg(twin='status = "disabled"'), STICK)   # stick active
        self.assertEqual(ran, ["own", "by-tag"])
        self.assertEqual(skipped, [("twin-own", "device 'twin' is disabled")])

    def test_an_ignored_device_hides_the_thing_from_unnamed_rules(self):
        ignored = cfg(extra='\n[devices.fs]\nmatch = { fs_type = "ext4" }\nstatus = "ignored"\n')
        self.assertEqual(self.names(ignored, UNKNOWN_FS, MOUNTED)[0], [])
        self.assertEqual(self.names(cfg(), UNKNOWN_FS, MOUNTED)[0], ["open"])   # without it, it runs

    def test_a_disabled_action_is_skipped_with_a_reason(self):
        off = cfg(extra='\n[[actions]]\nname = "later"\ntag = "usbtest"\non = ["connected"]\n'
                        'run = ["true"]\nenabled = false\nallow_unnamed = false\n')
        ran, skipped = self.names(off, STICK)
        self.assertNotIn("later", ran)
        self.assertIn(("later", "the action is disabled"), skipped)
        # only reported where it would have applied: not for another event or an unrelated thing
        self.assertEqual(self.names(off, STICK, MOUNTED)[1], [])
        self.assertEqual(self.names(off, Endpoint("udev", "/x", {"serial": "nope"}))[1], [])

    def test_a_disabled_device_action_that_is_also_disabled(self):
        off = cfg(extra='\n[[actions]]\nname = "d"\ndevice = "stick"\non = ["connected"]\n'
                        'run = ["true"]\nenabled = false\n')
        self.assertIn(("d", "the action is disabled"), self.names(off, STICK)[1])

    def test_handle_submits_nothing_for_an_ignored_thing(self):
        runner = mock.Mock()
        engine = Engine(cfg(stick='status = "ignored"'), runner)
        self.assertEqual(engine.handle(Event(CONNECTED, STICK)), [])
        runner.submit.assert_not_called()
        engine = Engine(cfg(stick='status = "disabled"'), runner)
        self.assertEqual([j.action.name for j in engine.handle(Event(CONNECTED, STICK))],
                         ["by-tag", "twin-own"])
        runner.submit.assert_called_once()


class ExplainTests(unittest.TestCase):
    def text(self, config, endpoint=STICK, kinds=None):
        return "\n".join(explain.explain_endpoint(config, endpoint, kinds))

    def test_labels_and_reasons(self):
        text = self.text(cfg(stick='status = "disabled"'))
        self.assertIn("device: stick [disabled], twin", text)
        self.assertIn("skips 'own': device 'stick' is disabled", text)
        self.assertIn("runs 'by-tag'", text)

    def test_ignored(self):
        text = self.text(cfg(stick='status = "ignored"'))
        self.assertIn("stick [ignored]", text)
        self.assertIn("ignored (stick): nothing runs for this thing", text)
        self.assertNotIn("runs '", text)

    def test_disabled_actions_are_shown(self):
        off = cfg(extra='\n[[actions]]\nname = "later"\ndevice = "stick"\non = ["connected"]\n'
                        'run = ["true"]\nenabled = false\n')
        self.assertIn("skips 'later': the action is disabled", self.text(off))

    def test_device_label(self):
        c = cfg(stick='status = "ignored"', twin='status = "disabled"')
        self.assertEqual(device_label(c.devices["stick"]), "stick [ignored]")
        self.assertEqual(device_label(c.devices["twin"]), "twin [disabled]")
        self.assertEqual(device_label(cfg().devices["stick"]), "stick")


class EditTests(unittest.TestCase):
    HAND = ('# my config\n[devices.stick]   # a stick\nmatch = { serial = "S9x7a" }\n'
            'status = ""\n\n[devices.card]\n[devices.card.match]\nvendor_id = "2ce3"\n\n'
            '[devices.hub]\nmatch = { vendor_id = "1d6b" }\nstatus = "ignored"   # noisy\n\n'
            '[[actions]]   # first\nname = "one"\ndevice = "stick"\non = ["connected"]\n'
            'timeout = 30   # seconds\nrun = ["true"]\n\n'
            '[[actions]]\nname = "two"\ntag = "t"\non = ["connected"]\nrun = ["true"]\n')

    def test_set_and_clear_a_status(self):
        p = write(self.HAND)
        self.assertEqual(E.set_device_status(p, ["stick", "card"], "disabled"), ["stick", "card"])
        c = C.load(p)
        self.assertEqual([c.devices[n].status for n in ("stick", "card", "hub")],
                         ["disabled", "disabled", "ignored"])
        self.assertIn('[devices.stick]   # a stick', p.read_text())       # comments kept
        self.assertIn("[devices.card.match]", p.read_text())               # sub-table layout ok
        self.assertEqual(E.set_device_status(p, ["stick"], "disabled"), [])   # already: no change
        self.assertEqual(E.set_device_status(p, ["stick", "hub"], None), ["stick", "hub"])
        c = C.load(p)
        self.assertEqual([c.devices[n].status for n in ("stick", "card", "hub")],
                         ["active", "disabled", "active"])
        self.assertNotIn("noisy", p.read_text().split("[[actions]]")[0].split("[devices.hub]")[1])
        self.assertNotIn("status", p.read_text().split("[devices.card]")[0])   # `status = ""` gone

    def test_all_or_nothing(self):
        p = write(self.HAND)
        before = p.read_bytes()
        with self.assertRaises(C.ConfigError) as cm:
            E.set_device_status(p, ["stick", "ghost", "nope"], "ignored")
        self.assertIn("'ghost', 'nope'", str(cm.exception))
        self.assertEqual(p.read_bytes(), before)

    def test_a_status_edit_that_does_nothing_is_rolled_back(self):
        p = write(self.HAND)
        before = p.read_bytes()
        with mock.patch.object(E, "_edited", return_value=self.HAND):
            with self.assertRaises(C.ConfigError):
                E.set_device_status(p, ["stick"], "ignored")
        self.assertEqual(p.read_bytes(), before)

    def test_enable_and_disable_actions(self):
        p = write(self.HAND)
        self.assertEqual(E.set_action_enabled(p, ["one"], False), ["one"])
        self.assertFalse([a for a in C.load(p).actions if a.name == "one"][0].enabled)
        self.assertEqual(E.set_action_enabled(p, ["one"], False), [])
        self.assertEqual(E.set_action_enabled(p, ["one", "two"], True), ["one"])
        self.assertNotIn("enabled", p.read_text())                     # back to absent
        with self.assertRaises(C.ConfigError):
            E.set_action_enabled(p, ["one", "ghost"], False)

    def test_actions_set(self):
        p = write(self.HAND)
        E.set_action_options(p, "one", on=["connected", "mounted"], timeout=45)
        a = [x for x in C.load(p).actions if x.name == "one"][0]
        self.assertEqual((a.on, a.timeout), (("connected", "mounted"), 45.0))
        self.assertIn("timeout = 45   # seconds", p.read_text())          # the comment survived
        E.set_action_options(p, "two", unnamed="allow")
        self.assertIs([x for x in C.load(p).actions if x.name == "two"][0].allow_unnamed, True)
        E.set_action_options(p, "two", unnamed="named-only")
        self.assertIs([x for x in C.load(p).actions if x.name == "two"][0].allow_unnamed, False)
        E.set_action_options(p, "two", unnamed="default")
        self.assertIsNone([x for x in C.load(p).actions if x.name == "two"][0].allow_unnamed)
        self.assertNotIn("allow_unnamed", p.read_text())
        E.set_action_options(p, "one", timeout=2.5)
        self.assertIn("timeout = 2.5", p.read_text())

    def test_actions_set_refuses_what_the_config_would_refuse(self):
        p = write(self.HAND)
        before = p.read_bytes()
        for kwargs in ({"unnamed": "allow"},                 # 'one' targets a device
                       {"on": ["mounted"], },                # ... fine
                       {"on": ["bogus"]}, {}):
            if kwargs == {"on": ["mounted"]}:
                continue
            with self.subTest(kwargs):
                with self.assertRaises(C.ConfigError):
                    E.set_action_options(p, "one", **kwargs)
                self.assertEqual(p.read_bytes(), before)
        with self.assertRaises(C.ConfigError):
            E.set_action_options(p, "ghost", timeout=5)

    def test_add_device_with_a_status(self):
        p = write("")
        E.add_device(p, "hub", {"vendor_id": "1d6b", "product_id": "2"}, "a hub", (), "ignored")
        self.assertEqual(C.load(p).devices["hub"].status, "ignored")
        self.assertIn('status = "ignored"', p.read_text())


class CliTests(unittest.TestCase):
    def setUp(self):
        self.p = write(EditTests.HAND)

    def run_cli(self, *args, reload_result=False):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("udevmon.daemon.reload_running", return_value=reload_result) as reload, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(["--config", str(self.p), *args])
            except SystemExit as e:
                rc = e.code
        return rc, out.getvalue(), err.getvalue(), reload

    def test_devices(self):
        rc, out, _, reload = self.run_cli("devices", "disable", "stick", "card", reload_result=True)
        self.assertEqual(rc, 0)
        self.assertIn("Disabled: stick, card.", out)
        self.assertIn("Reloaded the running daemon.", out)
        reload.assert_called_once_with(self.p)
        rc, out, _, _ = self.run_cli("devices", "disable", "stick", "hub")
        self.assertIn("Disabled: hub.", out)
        self.assertIn("Already disabled: stick.", out)
        rc, out, _, _ = self.run_cli("devices", "ignore", "stick")
        self.assertIn("Ignored: stick.", out)
        rc, out, _, _ = self.run_cli("devices", "list")
        self.assertIn("stick [ignored]", out)
        self.assertIn("hub [disabled]", out)
        rc, out, _, _ = self.run_cli("devices", "show", "stick")
        self.assertIn("status: ignored", out)
        rc, out, _, _ = self.run_cli("devices", "enable", "stick", "card", "hub")
        self.assertIn("Enabled: stick, card, hub.", out)
        self.assertEqual({d.status for d in C.load(self.p).devices.values()}, {"active"})

    def test_actions(self):
        self.assertEqual(self.run_cli("actions", "disable", "one", "two")[0], 0)
        self.assertIn("[disabled]", self.run_cli("actions", "list")[1])
        rc, out, _, _ = self.run_cli("actions", "enable", "one")
        self.assertIn("Enabled: one.", out)
        rc, out, _, _ = self.run_cli("actions", "list")
        self.assertEqual(out.count("[disabled]"), 1)

    def test_actions_set_and_its_errors(self):
        self.assertEqual(self.run_cli("actions", "set", "two", "--allow-unnamed", "--timeout", "5")[0], 0)
        a = [x for x in C.load(self.p).actions if x.name == "two"][0]
        self.assertEqual((a.allow_unnamed, a.timeout), (True, 5.0))
        self.assertEqual(self.run_cli("actions", "set", "two", "--default-unnamed", "--on", "mounted")[0], 0)
        rc, _, err, _ = self.run_cli("actions", "set", "two")
        self.assertEqual((rc, "nothing to change" in err), (1, True))
        rc, _, err, _ = self.run_cli("actions", "set", "two", "--on", "bogus")
        self.assertEqual((rc, "events must be from" in err), (1, True))
        rc, _, err, _ = self.run_cli("actions", "set", "two", "--timeout", "0")
        self.assertEqual((rc, "greater than 0" in err), (1, True))
        rc, _, err, _ = self.run_cli("actions", "set", "one", "--allow-unnamed")
        self.assertEqual((rc, "only applies to actions that use 'tag'" in err), (1, True))
        rc, _, err, _ = self.run_cli("actions", "set", "two", "--allow-unnamed", "--named-only")
        self.assertNotEqual(rc, 0)                                    # mutually exclusive

    def test_unknown_names_change_nothing(self):
        before = self.p.read_bytes()
        rc, _, err, reload = self.run_cli("devices", "ignore", "stick", "ghost")
        self.assertEqual((rc, "ghost" in err), (1, True))
        self.assertEqual(self.p.read_bytes(), before)
        reload.assert_not_called()

    def test_check_lists_the_notes(self):
        self.run_cli("devices", "ignore", "hub", "--no-reload")
        self.run_cli("actions", "disable", "two", "--no-reload")
        out = self.run_cli("check")[1]
        self.assertIn("note: device 'hub' is ignored", out)
        self.assertIn("note: action 'two' is disabled", out)


class ScanTests(unittest.TestCase):
    def prompt(self, endpoints, answers):
        from udevmon.scan import Scanner
        p = Path(tempfile.mkdtemp()) / "c.toml"
        scanner = Scanner(p, prompt=True)
        scanner.loop = mock.Mock()
        scanner.pending = {str(i): e for i, e in enumerate(endpoints)}
        seq = iter(answers)
        prompts = []

        def fake(prompt=""):
            prompts.append(prompt)
            return next(seq)
        out = io.StringIO()
        with mock.patch("builtins.input", fake), mock.patch("udevmon.daemon.reload_running",
                                                            return_value=False), \
                contextlib.redirect_stdout(out):
            scanner._prompt_user()
        return p, out.getvalue(), prompts

    HUB = Endpoint("udev", "/sys/hub", {"vendor_id": "1d6b", "product_id": "0002", "port_path": "usb1"})
    KEY = Endpoint("udev", "/sys/key", {"vendor_id": "1", "product_id": "2", "serial": "S9x7a"})

    def test_ignoring_from_scan(self):
        p, out, prompts = self.prompt([self.HUB, self.KEY], ["i1", "root-hub", "", "y", ""])
        c = C.load(p)
        self.assertEqual(c.devices["root-hub"].status, "ignored")
        self.assertEqual(c.devices["root-hub"].tags, ())
        self.assertIn("(ignored)", out)
        self.assertFalse(any("Tags" in x for x in prompts))               # no tags for an ignored one
        self.assertTrue(any("to ignore" in x for x in prompts))
        self.assertNotIn("Add an action", out)

    def test_naming_still_works_and_marks_ignored_things(self):
        p, out, prompts = self.prompt([self.KEY], ["1", "key", "", "t1"])
        self.assertEqual(C.load(p).devices["key"].status, "active")
        self.assertEqual(C.load(p).devices["key"].tags, ("t1",))


if __name__ == "__main__":
    unittest.main()
