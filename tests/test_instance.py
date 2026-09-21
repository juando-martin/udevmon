"""`instance` (one value per plug or mount) and the per-instance state folder."""

import os
import stat
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.test_sources import BLK, FS, P, managed
from udevmon import actions as A
from udevmon import config as C
from udevmon import explain, state
from udevmon.engine import Engine, Job, presence_ends
from udevmon.events import (CONNECTED, DISCONNECTED, MOUNTED, REMOVED, SHUTDOWN, STARTUP,
                            UNMOUNTED, VOLATILE_KEYS, Endpoint, Event)
from udevmon.sources import udev as U
from udevmon.sources import udisks as D

MP = [list(b"/run/media/u/BACKUP\0")]


def usb_props(**extra):
    return {"syspath": "/sys/devices/usb3/3-4", "subsystem": "usb", "devtype": "usb_device",
            "vendor_id": "1050", "product_id": "0407", **extra}


class UdevInstanceTests(unittest.TestCase):
    def test_it_is_built_on_usec_initialized(self):
        src = U.UdevSource()
        event = src.handle("add", usb_props(udev_usec_initialized="9878235508"))
        self.assertEqual(event.props["instance"], "usb-9878235508")

    def test_the_disconnect_carries_the_instance_seen_at_connect(self):
        src = U.UdevSource()
        c = src.handle("add", usb_props(udev_usec_initialized="111"))
        d = src.handle("remove", usb_props(udev_usec_initialized="111"))
        self.assertEqual(d.props["instance"], c.props["instance"])
        self.assertEqual(d.kind, DISCONNECTED)

    def test_even_when_the_remove_event_has_other_values(self):
        src = U.UdevSource()
        c = src.handle("add", usb_props())                          # no usec on the add event
        d = src.handle("remove", usb_props(udev_usec_initialized="999"))
        self.assertEqual(d.props["instance"], c.props["instance"])

    def test_without_usec_it_is_a_hash_and_a_counter_so_each_plug_differs(self):
        src = U.UdevSource()
        first = src.handle("add", usb_props()).props["instance"]
        src.handle("remove", usb_props())
        second = src.handle("add", usb_props()).props["instance"]
        self.assertRegex(first, r"^usb-[0-9a-f]{8}-\d+$")
        self.assertNotEqual(first, second)

    def test_a_removal_of_a_device_never_seen_still_gets_one(self):
        d = U.UdevSource().handle("remove", usb_props(udev_usec_initialized="5"))
        self.assertEqual(d.props["instance"], "usb-5")

    def test_things_present_at_startup_keep_theirs_until_they_go(self):
        src = U.UdevSource()
        present = Endpoint("udev", usb_props()["syspath"],
                           src._with_instance(usb_props(udev_usec_initialized="42")))
        src._cache[present.key] = present                           # what start() primes
        self.assertEqual(src.present()[0].props["instance"], "usb-42")
        self.assertEqual(src.handle("remove", usb_props()).props["instance"], "usb-42")

    def test_a_snapshot_carries_it_too(self):
        """What start() primes the cache with, and what `present` and `scan` list."""
        dev = mock.Mock(sys_path="/sys/devices/usb3/3-4", subsystem="usb",
                        device_type="usb_device", device_node="/dev/bus/usb/003/006",
                        sys_name="3-4",
                        properties={"USEC_INITIALIZED": "9878235508", "ID_VENDOR_ID": "1050",
                                    "ID_MODEL_ID": "0407"})
        ctx = mock.Mock()
        ctx.list_devices.return_value = [dev]
        src = U.UdevSource()
        with mock.patch.object(src, "_context", return_value=(None, ctx)):
            endpoints = src.snapshot()
        self.assertEqual(endpoints[0].props["instance"], "usb-9878235508")
        self.assertEqual(endpoints[0].props["vendor_id"], "1050")

    def test_the_subsystem_is_the_prefix(self):
        props = {"syspath": "/sys/x", "subsystem": "input", "udev_usec_initialized": "7"}
        self.assertEqual(U.instance_id(props, iter([1])), "input-7")


class UdisksInstanceTests(unittest.TestCase):
    def mounted(self):
        t = D.UdisksTracker()
        t.load(managed())
        return t, t.properties_changed(P, FS, {"MountPoints": MP})

    def test_a_mount_unmount_has_one_instance(self):
        t, mounted = self.mounted()
        unmounted = t.properties_changed(P, FS, {"MountPoints": []})
        self.assertRegex(mounted[0].props["instance"], r"^fs-\d+-\d+$")
        self.assertEqual(unmounted[0].props["instance"], mounted[0].props["instance"])

    def test_the_next_mount_is_a_new_instance(self):
        t, first = self.mounted()
        t.properties_changed(P, FS, {"MountPoints": []})
        second = t.properties_changed(P, FS, {"MountPoints": MP})
        self.assertNotEqual(second[0].props["instance"], first[0].props["instance"])

    def test_a_pull_says_the_same_on_unmounted_and_removed(self):
        t, mounted = self.mounted()
        events = t.interfaces_removed(P, [FS, BLK, D.I_PART])
        self.assertEqual({e.props["instance"] for e in events},
                         {mounted[0].props["instance"]})

    def test_present_at_startup_the_listing_shows_the_mounts_instance(self):
        t = D.UdisksTracker()
        t.load(managed(mounted=True))
        shown = t.filesystems(include_system=True)[0].props["instance"]
        gone = t.interfaces_removed(P, [FS, BLK])
        self.assertEqual(gone[0].props["instance"], shown)

    def test_a_filesystem_listed_unmounted_gets_a_new_instance_when_it_is_mounted(self):
        t = D.UdisksTracker()
        t.load(managed())
        listed = t.filesystems(include_system=True)[0].props["instance"]
        mounted = t.properties_changed(P, FS, {"MountPoints": MP})
        self.assertNotEqual(mounted[0].props["instance"], listed)

    def test_a_filesystem_that_is_not_mounted_still_has_one(self):
        t = D.UdisksTracker()
        t.load(managed())
        self.assertRegex(t.filesystems(include_system=True)[0].props["instance"], r"^fs-")


class ScanTests(unittest.TestCase):
    def test_the_instance_stays_out_of_the_compact_line_and_the_offered_keys(self):
        from udevmon.scan import format_all_props, format_props
        e = Endpoint("udev", "k", {"vendor_id": "1", "instance": "usb-1"})
        self.assertNotIn("instance", format_props(e))
        self.assertIn("instance = usb-1   [changes per plug]", format_all_props(e))


class PropertyTests(unittest.TestCase):
    def test_it_cannot_be_used_in_a_rule(self):
        self.assertIn("instance", VOLATILE_KEYS)
        with self.assertRaises(C.ConfigError):
            C.parse({"tags": {"t": {"match": {"instance": "usb-1"}}}})

    def test_placeholders_are_accepted_for_every_event(self):
        for kind in ("connected", "disconnected", "mounted", "unmounted", "removed",
                     "startup", "shutdown"):
            with self.subTest(kind):
                C.parse({"devices": {"d": {"match": {"serial": "S"}}},
                         "actions": [{"name": "a", "device": "d", "on": [kind],
                                      "run": ["echo", "{instance}", "{state_dir}"]}]})

    def test_they_are_not_hardware_values_for_the_unnamed_warning(self):
        c = C.parse({"tags": {"t": {"match": {"fs_type": "ext4"}}},
                     "actions": [{"name": "a", "tag": "t", "on": ["mounted"],
                                  "allow_unnamed": True,
                                  "run": ["echo", "{instance}", "{state_dir}"]}]})
        self.assertFalse(any("hardware-supplied" in w for w in c.warnings))

    def test_explain_fills_them_in_for_a_simulated_thing(self):
        c = C.parse({"devices": {"d": {"match": {"serial": "S"}}},
                     "actions": [{"name": "a", "device": "d", "on": ["connected"],
                                  "run": ["echo", "{instance}", "{state_dir}"]}]})
        text = "\n".join(explain.explain_endpoint(c, Endpoint("udev", "k", {"serial": "S"}),
                                                  [CONNECTED]))
        self.assertIn("echo '<instance>' '<state folder>'", text)


class StateFolderTests(unittest.TestCase):
    def test_created_private_and_removed(self):
        p = state.ensure("usb-123")
        self.addCleanup(state.wipe)
        self.assertTrue(p.is_dir())
        self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o700)
        self.assertEqual(p, state.root() / "usb-123")
        (p / "serial").write_text("7544954")
        state.remove("usb-123")
        self.assertFalse(p.exists())
        state.remove("usb-123")                                     # safe to repeat

    def test_an_instance_can_never_escape_the_folder(self):
        for bad in ("../../etc", "a/b", "..", ".hidden", "x y", ""):
            with self.subTest(bad):
                p = state.path_for(bad)
                self.assertEqual(p.parent, state.root())
                self.assertNotIn("/", p.name)
                self.assertFalse(p.name.startswith("."))

    def test_wipe_removes_everything(self):
        state.ensure("a"), state.ensure("b")
        state.wipe()
        self.assertFalse(state.root().exists())
        self.assertFalse(state.root().parent.exists())              # no empty folder left behind
        state.wipe()                                                # nothing there: fine

    def test_wipe_leaves_the_folder_above_alone_when_it_holds_something_else(self):
        state.ensure("a")
        other = state.root().parent / "other"
        other.write_text("x")
        state.wipe()
        self.assertFalse(state.root().exists())
        self.assertTrue(other.exists())
        other.unlink()
        state.wipe()


class EngineLifecycleTests(unittest.TestCase):
    def ev(self, kind, **props):
        return Event(kind, Endpoint("udev", "k", {"instance": "i-1", **props}))

    def test_which_events_end_a_presence(self):
        self.assertEqual(presence_ends(self.ev(DISCONNECTED)), "i-1")
        self.assertEqual(presence_ends(self.ev(REMOVED, surprise="1")), "i-1")
        self.assertEqual(presence_ends(self.ev(UNMOUNTED, surprise="0")), "i-1")
        self.assertIsNone(presence_ends(self.ev(UNMOUNTED, surprise="1")))   # `removed` follows
        for kind in (CONNECTED, MOUNTED, STARTUP, SHUTDOWN):
            self.assertIsNone(presence_ends(self.ev(kind)), kind)
        self.assertIsNone(presence_ends(Event(DISCONNECTED, Endpoint("udev", "k", {}))))

    def engine(self, actions=(), **kw):
        cfg = C.parse({"devices": {"d": {"match": {"serial": "S"}}}, "actions": list(actions)})
        self.runner = mock.Mock()
        return Engine(cfg, self.runner, **kw)

    def test_the_folder_is_tidied_even_when_no_action_runs(self):
        engine = self.engine()
        engine.handle(self.ev(DISCONNECTED, serial="S"))
        self.runner.submit.assert_called_once()
        self.assertEqual(self.runner.submit.call_args.args[2], "i-1")

    def test_also_for_an_unknown_thing_and_while_paused(self):
        engine = self.engine(paused=lambda: True)
        engine.handle(self.ev(DISCONNECTED, serial="S"))
        self.assertEqual(self.runner.submit.call_args.args[2], "i-1")
        engine = self.engine()
        engine.handle(self.ev(DISCONNECTED, serial="stranger"))
        self.assertEqual(self.runner.submit.call_args.args[2], "i-1")

    def test_also_when_the_event_is_debounced(self):
        action = {"name": "a", "device": "d", "on": ["disconnected"], "run": ["true"]}
        engine = self.engine([action], clock=lambda: 100.0)
        engine.handle(self.ev(DISCONNECTED, serial="S"))
        engine.handle(self.ev(DISCONNECTED, serial="S"))            # within the debounce time
        self.assertEqual(self.runner.submit.call_count, 2)
        self.assertEqual(self.runner.submit.call_args.args[2], "i-1")

    def test_the_jobs_and_the_end_go_in_one_submit(self):
        action = {"name": "a", "device": "d", "on": ["disconnected"], "run": ["true"]}
        engine = self.engine([action])
        jobs = engine.handle(self.ev(DISCONNECTED, serial="S"))
        self.assertEqual(len(jobs), 1)
        self.runner.submit.assert_called_once()
        self.assertEqual(self.runner.submit.call_args.args[1], jobs)

    def test_other_events_with_no_action_submit_nothing(self):
        engine = self.engine()
        engine.handle(self.ev(CONNECTED, serial="S"))
        self.runner.submit.assert_not_called()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(state.wipe)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def script(self, name, body):
        p = self.dir / name
        p.write_text("#!/bin/bash\n" + body)
        p.chmod(0o755)
        return str(p)

    def event(self, kind, instance="usb-9"):
        return Event(kind, Endpoint("udev", "k", {"instance": instance, "serial": "S"}))

    def job(self, name, script, *args):
        return Job(C.Action(name, ("d",), (), ("connected", "disconnected"),
                            (script, *args), 10), "d", None)

    def test_a_connect_script_can_leave_a_note_for_the_disconnect_script(self):
        """The whole point: what `ykman` reported at plug time is there at removal."""
        out = self.dir / "out"
        connect = self.script("c.sh", '[ -d "$UDEVMON_STATE_DIR" ] || exit 9\n'
                                      'echo 7544954 > "$UDEVMON_STATE_DIR/serial"\n')
        disconnect = self.script("d.sh", f'cat "$UDEVMON_STATE_DIR/serial" > "{out}"\n'
                                         f'echo "$UDEVMON_INSTANCE" >> "{out}"\n')
        runner = A.ActionRunner(2)
        with self.assertLogs("udevmon.actions", "INFO"):
            runner.submit(self.event(CONNECTED), [self.job("c", connect)]).result(10)
            self.assertTrue((state.root() / "usb-9" / "serial").exists())     # while present
            runner.submit(self.event(DISCONNECTED), [self.job("d", disconnect)],
                          "usb-9").result(10)
        self.assertEqual(out.read_text().split(), ["7544954", "usb-9"])
        self.assertFalse((state.root() / "usb-9").exists())                   # gone afterwards
        runner.shutdown(wait=True)

    def test_the_placeholder_and_the_variable_agree(self):
        out = self.dir / "out"
        script = self.script("p.sh", f'echo "$1|$UDEVMON_STATE_DIR" > "{out}"\n')
        runner = A.ActionRunner(1)
        job = Job(C.Action("p", ("d",), (), ("connected",), (script, "{state_dir}"), 10),
                  "d", None)
        with self.assertLogs("udevmon.actions", "INFO"):
            runner.submit(self.event(CONNECTED), [job]).result(10)
        shown, env = out.read_text().strip().split("|")
        self.assertEqual(shown, env)
        self.assertEqual(env, str(state.root() / "usb-9"))
        runner.shutdown(wait=True)

    def test_the_folder_is_removed_even_when_there_is_nothing_to_run(self):
        state.ensure("usb-9")
        runner = A.ActionRunner(1)
        runner.submit(self.event(DISCONNECTED), [], "usb-9").result(10)
        self.assertFalse((state.root() / "usb-9").exists())
        runner.shutdown(wait=True)

    def test_two_things_get_separate_folders(self):
        seen = self.dir / "seen"
        script = self.script("s.sh", f'echo "$UDEVMON_STATE_DIR" >> "{seen}"\n')
        runner = A.ActionRunner(2)
        with self.assertLogs("udevmon.actions", "INFO"):
            for inst in ("usb-1", "usb-2"):
                runner.submit(self.event(CONNECTED, inst), [self.job("s", script)]).result(10)
        self.assertEqual(sorted(seen.read_text().split()),
                         [str(state.root() / "usb-1"), str(state.root() / "usb-2")])
        runner.shutdown(wait=True)

    def test_no_instance_no_folder_no_variable(self):
        out = self.dir / "out"
        script = self.script("n.sh", f'echo "[$UDEVMON_STATE_DIR]" > "{out}"\n')
        runner = A.ActionRunner(1)
        event = Event(CONNECTED, Endpoint("udev", "k", {"serial": "S"}))
        with self.assertLogs("udevmon.actions", "INFO"):
            runner.submit(event, [self.job("n", script)]).result(10)
        self.assertEqual(out.read_text().strip(), "[]")
        self.assertFalse(state.root().exists())
        runner.shutdown(wait=True)

    def test_a_dry_run_creates_nothing(self):
        runner = A.ActionRunner(1, dry_run=True)
        with self.assertLogs("udevmon.actions", "INFO"):
            runner.submit(self.event(CONNECTED), [self.job("x", "true")]).result(10)
        self.assertFalse(state.root().exists())
        runner.shutdown(wait=True)

    def test_a_crash_in_an_action_still_removes_the_folder_at_the_end(self):
        runner = A.ActionRunner(1)
        with mock.patch.object(A, "run_action", side_effect=RuntimeError("boom")), \
                self.assertLogs("udevmon.actions", "ERROR"):
            state.ensure("usb-9")
            runner.submit(self.event(DISCONNECTED), [self.job("x", "true")], "usb-9").result(10)
        self.assertFalse((state.root() / "usb-9").exists())
        runner.shutdown(wait=True)


class DaemonTests(unittest.TestCase):
    def test_the_daemon_wipes_at_start_and_stop(self):
        from udevmon import daemon
        src = Path(daemon.__file__).read_text()
        self.assertEqual(src.count("state.wipe()"), 2)
        self.assertLess(src.index("state.wipe()"), src.index("UdevSource(self.cfg"))


if __name__ == "__main__":
    unittest.main()
