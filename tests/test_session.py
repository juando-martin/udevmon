import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import session as S


def fake_proc(root: Path, pid: int, started: int, env: dict[str, str]) -> None:
    d = root / str(pid)
    d.mkdir()
    (d / "environ").write_bytes(b"\0".join(f"{k}={v}".encode() for k, v in env.items()) + b"\0")
    rest = ["S"] + ["0"] * 18 + [str(started)]          # field 22 is the start time
    (d / "stat").write_text(f"{pid} (some prog) {' '.join(rest)} 0 0\n")


class FromProcessesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_the_newest_program_with_a_display_wins(self):
        fake_proc(self.root, 100, 50, {"DISPLAY": ":0", "XAUTHORITY": "/old", "HOME": "/h"})
        fake_proc(self.root, 200, 90, {"DISPLAY": ":1", "XAUTHORITY": "/new"})
        fake_proc(self.root, 300, 99, {"PATH": "/bin"})              # newest, but no display
        self.assertEqual(S.from_processes(str(self.root)),
                         {"DISPLAY": ":1", "XAUTHORITY": "/new"})

    def test_wayland_counts_and_only_session_variables_are_taken(self):
        fake_proc(self.root, 100, 5, {"WAYLAND_DISPLAY": "wayland-0", "SECRET": "x"})
        self.assertEqual(S.from_processes(str(self.root)), {"WAYLAND_DISPLAY": "wayland-0"})

    def test_a_program_of_the_graphical_login_beats_a_newer_ssh_shell(self):
        fake_proc(self.root, 100, 5, {"DISPLAY": ":0", "XDG_SESSION_ID": "2"})
        fake_proc(self.root, 200, 9, {"DISPLAY": "localhost:10.0", "XDG_SESSION_ID": "7"})
        self.assertEqual(S.from_processes(str(self.root)), {"DISPLAY": "localhost:10.0"})
        self.assertEqual(S.from_processes(str(self.root), sessions={"2": ":0"}),
                         {"DISPLAY": ":0"})

    def test_nothing_found(self):
        (self.root / "self").mkdir()
        fake_proc(self.root, 100, 5, {"PATH": "/bin"})
        self.assertEqual(S.from_processes(str(self.root)), {})
        self.assertEqual(S.from_processes(str(self.root / "missing")), {})


class FromManagerTests(unittest.TestCase):
    def test_it_parses_and_filters_show_environment(self):
        out = "HOME=/h\nDISPLAY=:0\nXAUTHORITY=$'/a b/\\'x'\nLANG=C\n"
        done = mock.Mock(returncode=0, stdout=out)
        with mock.patch("udevmon.session.subprocess.run", return_value=done):
            self.assertEqual(S.from_manager(), {"DISPLAY": ":0", "XAUTHORITY": "/a b/'x"})

    def test_no_systemctl_or_a_failure_gives_nothing(self):
        with mock.patch("udevmon.session.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(S.from_manager(), {})
        with mock.patch("udevmon.session.subprocess.run",
                        return_value=mock.Mock(returncode=1, stdout="")):
            self.assertEqual(S.from_manager(), {})


class GraphicalSessionsTests(unittest.TestCase):
    def test_only_graphical_user_logins_count(self):
        answers = {
            "show-user": "2 3 4 5\n",
            "2": "Type=x11\nState=active\nClass=user\nDisplay=:0\n",
            "3": "Type=tty\nState=online\nClass=user\nDisplay=\n",
            "4": "Type=wayland\nState=online\nClass=user\nDisplay=\n",
            "5": "Type=x11\nState=closing\nClass=user\nDisplay=:1\n",
        }

        def run(argv, **kw):
            key = argv[1] if argv[1] == "show-user" else argv[2]
            return mock.Mock(returncode=0, stdout=answers[key])
        with mock.patch("udevmon.session.subprocess.run", side_effect=run):
            self.assertEqual(S.graphical_sessions(), {"2": ":0", "4": ""})

    def test_no_loginctl(self):
        with mock.patch("udevmon.session.subprocess.run", side_effect=FileNotFoundError):
            self.assertEqual(S.graphical_sessions(), {})


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.clock = 0.0

        def sleep(s):
            self.clock += s
        patches = [mock.patch.object(S, "_sleep", sleep),
                   mock.patch.object(S, "_monotonic", lambda: self.clock),
                   mock.patch.object(S, "_last_note", None)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def run_update(self, env, manager=None, procs=None, sessions=None):
        manager = manager if callable(manager) else (lambda m=manager or {}: m)
        procs_f = procs if callable(procs) else (lambda sessions=(), p=procs or {}: p)
        with mock.patch.object(S, "from_manager", side_effect=manager) as m, \
                mock.patch.object(S, "from_processes", side_effect=procs_f) as p, \
                mock.patch.object(S, "graphical_sessions", return_value=sessions or {}), \
                self.assertLogs("udevmon.session", "INFO") as logs:
            S.update(env)
        return m, p, logs

    def test_a_terminal_with_a_display_is_left_alone(self):
        env = {"DISPLAY": ":5"}
        with mock.patch.object(S, "from_manager") as m, mock.patch.object(S, "from_processes"):
            S.update(env)
        m.assert_not_called()
        self.assertEqual(env, {"DISPLAY": ":5"})

    def test_a_service_started_before_the_session_gets_the_managers_variables(self):
        env = {"INVOCATION_ID": "x", "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/user/1/bus"}
        _, p, logs = self.run_update(env, manager={"DISPLAY": ":0", "XAUTHORITY": "/xa"})
        p.assert_not_called()
        self.assertEqual(env["DISPLAY"], ":0")
        self.assertEqual(env["XAUTHORITY"], "/xa")
        self.assertIn("from the systemd user manager", logs.output[0])

    def test_under_the_service_the_managers_current_display_replaces_a_stale_one(self):
        env = {"INVOCATION_ID": "x", "DISPLAY": ":0"}
        self.run_update(env, manager={"WAYLAND_DISPLAY": "wayland-0"})
        self.assertNotIn("DISPLAY", env)
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")

    def test_a_desktop_that_never_imports_falls_back_to_a_running_program(self):
        env = {"INVOCATION_ID": "x"}
        _, p, logs = self.run_update(env, manager={"DBUS_SESSION_BUS_ADDRESS": "m"},
                                     procs={"DISPLAY": ":1", "XAUTHORITY": "/p"})
        p.assert_called_once()
        self.assertEqual((env["DISPLAY"], env["XAUTHORITY"]), (":1", "/p"))
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "m")
        self.assertIn("from a running program of yours", logs.output[0])

    def test_no_session_at_all_warns_once_and_does_not_wait(self):
        env = {"INVOCATION_ID": "x"}
        _, _, logs = self.run_update(env)
        self.assertNotIn("DISPLAY", env)
        self.assertEqual(self.clock, 0)
        self.assertIn("no graphical session found", logs.output[0])
        with mock.patch.object(S, "from_manager", return_value={}), \
                mock.patch.object(S, "from_processes", return_value={}), \
                mock.patch.object(S, "graphical_sessions", return_value={}), \
                mock.patch.object(S.log, "log") as said:
            S.update({"INVOCATION_ID": "x"})
        said.assert_not_called()

    def test_at_login_it_waits_for_the_desktop_to_get_its_display(self):
        # the service starts with the user manager, before the desktop has a display
        def manager():
            return {"DISPLAY": ":0", "XAUTHORITY": "/xa"} if self.clock >= 2 else {}
        env = {"INVOCATION_ID": "x"}
        _, _, logs = self.run_update(env, manager=manager, sessions={"2": ":0"})
        self.assertEqual(env["DISPLAY"], ":0")
        self.assertEqual(self.clock, 2)
        self.assertIn("waiting up to 30s", logs.output[0])
        self.assertIn("from the systemd user manager", logs.output[1])

    def test_the_wait_asks_for_a_program_of_that_login(self):
        seen = []

        def procs(sessions=()):
            seen.append(dict(sessions))
            return {"DISPLAY": ":0"} if sessions else {}
        env = {"INVOCATION_ID": "x"}
        self.run_update(env, procs=procs, sessions={"2": ":0"})
        self.assertEqual(seen, [{}, {"2": ":0"}])
        self.assertEqual(self.clock, 0)

    def test_after_the_wait_the_logind_display_is_used(self):
        env = {"INVOCATION_ID": "x"}
        _, _, logs = self.run_update(env, sessions={"2": ":0"})
        self.assertEqual(self.clock, S.WAIT)
        self.assertEqual(env["DISPLAY"], ":0")
        self.assertIn("(from logind)", logs.output[-1])

    def test_a_wayland_login_without_any_display_gives_up_after_the_wait(self):
        env = {"INVOCATION_ID": "x"}
        _, _, logs = self.run_update(env, sessions={"4": ""})
        self.assertEqual(self.clock, S.WAIT)
        self.assertNotIn("DISPLAY", env)
        self.assertIn("no graphical session found", logs.output[-1])


if __name__ == "__main__":
    unittest.main()
