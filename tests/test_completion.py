import argparse
import contextlib
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import cli, completion
from udevmon.completion import DIRS, FILES, NOSPACE, WORDS

CONFIG = """
[devices.stick]
match = { serial = "S9x7a" }
tags = ["usbtest", "backup"]
exclude_tags = ["storage"]
vars = { dest = "/srv/x", keep = "7" }

[devices.disk]
match = { fs_uuid = "U1" }
tags = ["backup"]

[tags.storage]
match = { fs_type = ["ext4", "exfat"] }

[[actions]]
name = "log"
tag = "usbtest"
on = ["connected"]
run = ["true"]

[[actions]]
name = "note"
device = "disk"
on = ["mounted"]
run = ["true"]
"""


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "c.toml"
        self.cfg.write_text(CONFIG)
        self.cfg.chmod(0o600)
        self.parser = cli.build_parser()

    def comp(self, line, config=True):
        """Complete the last word of ``line`` (end it with a space to complete a new word)."""
        words = line.split(" ")
        if config:
            words[1:1] = ["--config", str(self.cfg)]
        return completion.complete(self.parser, words, len(words) - 1)

    def words(self, line, **kw):
        mode, found = self.comp(line, **kw)
        self.assertEqual(mode, WORDS, line)
        return found

    def test_subcommands(self):
        found = self.words("udevmon ")
        for name in ("run", "scan", "present", "devices", "tags", "actions", "check",
                     "reload", "service", "completion"):
            self.assertIn(name, found)
        self.assertNotIn("_complete", found)
        self.assertEqual(self.words("udevmon d", config=False), ["devices"])
        self.assertEqual(self.words("udevmon -v ", config=False), self.words("udevmon ", config=False))

    def test_options(self):
        self.assertEqual(set(self.words("udevmon -", config=False)),
                         {"-h", "--help", "--version", "--config", "-v", "--verbose"})
        self.assertIn("--dry-run", self.words("udevmon run --"))
        self.assertEqual(self.words("udevmon actions add --allow"), ["--allow-unnamed"])
        self.assertEqual(set(self.words("udevmon actions add --")) & {"--device", "--tag", "--named-only",
                         "--name", "--timeout"}, {"--device", "--tag", "--named-only", "--name", "--timeout"})

    def test_groups_and_subcommands(self):
        self.assertEqual(self.words("udevmon devices "),
                         ["add", "disable", "enable", "exclude", "ignore", "include", "list",
                          "remove", "rename", "set", "show", "tag", "untag"])
        self.assertEqual(self.words("udevmon actions "),
                         ["add", "disable", "enable", "list", "remove", "rename", "set"])
        self.assertEqual(self.words("udevmon tags "), ["add", "list", "remove", "rename", "set", "show"])
        self.assertEqual(self.words("udevmon service "), ["install", "uninstall"])
        self.assertEqual(self.words("udevmon completion "), ["bash", "install", "uninstall"])
        self.assertEqual(self.words("udevmon devices sh"), ["show"])
        self.assertEqual(self.words("udevmon devices s"), ["set", "show"])

    def test_names_come_from_the_config_named_on_the_command_line(self):
        self.assertEqual(self.words("udevmon devices show "), ["stick", "disk"])
        self.assertEqual(self.words("udevmon devices remove s"), ["stick"])
        self.assertEqual(self.words("udevmon actions remove "), ["log", "note"])
        self.assertEqual(self.words("udevmon tags show "), ["backup", "storage", "usbtest"])
        # tags remove: only declared [tags.X] blocks
        self.assertEqual(self.words("udevmon tags remove "), ["storage"])

    def test_default_config_is_used_without_the_option(self):
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name}):
            (Path(self.tmp.name) / "udevmon").mkdir()
            shutil.copy(self.cfg, Path(self.tmp.name) / "udevmon" / "config.toml")
            os.chmod(Path(self.tmp.name) / "udevmon" / "config.toml", 0o600)
            self.assertEqual(self.words("udevmon devices show ", config=False), ["stick", "disk"])

    def test_tag_arguments(self):
        self.assertEqual(self.words("udevmon devices tag disk "), ["backup", "storage", "usbtest"])
        self.assertEqual(self.words("udevmon devices tag disk u"), ["usbtest"])
        self.assertEqual(self.words("udevmon devices untag stick "), ["usbtest", "backup"])
        self.assertEqual(self.words("udevmon devices untag stick u"), ["usbtest"])
        self.assertEqual(self.words("udevmon devices untag ghost "), [])
        self.assertEqual(self.words("udevmon devices tag "), ["stick", "disk"])

    def test_option_values(self):
        self.assertEqual(self.words("udevmon actions add --device "), ["stick", "disk"])
        self.assertEqual(self.words("udevmon actions add --tag u"), ["usbtest"])
        self.assertEqual(self.words("udevmon devices add x --match s --tag b"), ["backup"])
        self.assertEqual(self.words("udevmon actions add --name "), [])
        self.assertEqual(self.words("udevmon actions add --timeout "), [])

    def test_match_keys(self):
        mode, found = self.comp("udevmon devices add x --match ser")
        self.assertEqual((mode, found), (NOSPACE, ["serial="]))
        mode, found = self.comp("udevmon devices add x --match ")
        self.assertIn("mountpoint=", found)                 # allowed in a device match
        mode, found = self.comp("udevmon tags add t --match ")
        self.assertEqual(mode, NOSPACE)
        self.assertIn("fs_type=", found)
        self.assertNotIn("mountpoint=", found)              # volatile: rejected in a rule
        self.assertNotIn("devnode=", found)
        # after "key=" (bash splits it into key, =, value) there is nothing to offer
        self.assertEqual(self.comp("udevmon tags add t --match fs_type = "), (WORDS, []))
        self.assertEqual(self.comp("udevmon tags add t --match fs_type ="), (WORDS, []))

    def test_events(self):
        self.assertEqual(self.words("udevmon actions add --tag t "),
                         ["connected", "disconnected", "mounted", "removed", "shutdown", "startup",
                          "unmounted"])
        self.assertEqual(self.words("udevmon actions add --tag t mo"), ["mounted"])
        self.assertEqual(self.words("udevmon actions add --tag t connected,"),
                         ["connected,disconnected", "connected,mounted", "connected,removed",
                          "connected,shutdown", "connected,startup", "connected,unmounted"])
        self.assertEqual(self.words("udevmon actions add --tag t connected,dis"),
                         ["connected,disconnected"])
        self.assertNotIn("mounted,mounted", self.words("udevmon actions add --tag t mounted,"))

    def test_explain(self):
        self.assertIn("explain", self.words("udevmon "))
        self.assertEqual(self.words("udevmon explain mo"), ["mounted"])
        self.assertIn("connected", self.words("udevmon explain "))
        self.assertEqual(self.words("udevmon explain --device s"), ["stick"])
        self.assertEqual(self.comp("udevmon explain --prop fs_t"), (NOSPACE, ["fs_type="]))
        self.assertIn("mountpoint=", self.comp("udevmon explain --prop ")[1])   # any property
        self.assertEqual(set(self.words("udevmon explain --")),
                         {"--device", "--prop", "--all", "--help"})

    def test_status_and_action_commands(self):
        self.assertEqual(self.words("udevmon devices disable "), ["stick", "disk"])
        self.assertEqual(self.words("udevmon devices ignore stick "), ["stick", "disk"])   # repeats
        self.assertEqual(self.words("udevmon devices enable d"), ["disk"])
        self.assertEqual(self.words("udevmon actions disable "), ["log", "note"])
        self.assertEqual(self.words("udevmon actions enable n"), ["note"])
        self.assertEqual(self.words("udevmon actions set "), ["log", "note"])
        self.assertEqual(self.words("udevmon actions set log --on mo"), ["mounted"])
        self.assertEqual(self.words("udevmon actions set log --on connected,dis"),
                         ["connected,disconnected"])
        self.assertEqual(set(self.words("udevmon actions set log --")),
                         {"--allow-unnamed", "--named-only", "--default-unnamed", "--on",
                          "--timeout", "--no-reload", "--help", "--add-device",
                          "--remove-device", "--add-tag", "--remove-tag", "--all-tags", "--any-tags"})
        self.assertEqual(self.words("udevmon actions set log --timeout "), [])   # free text
        self.assertIn("--no-reload", self.words("udevmon devices ignore --"))

    def test_pause_and_resume(self):
        self.assertEqual(self.words("udevmon pa", config=False), ["pause"])
        self.assertEqual(self.words("udevmon res", config=False), ["resume"])
        self.assertEqual(set(self.words("udevmon pause --", config=False)), {"--for", "--help"})
        self.assertEqual(self.comp("udevmon pause --for ", config=False), (WORDS, []))   # free text
        self.assertEqual(self.words("udevmon resume ", config=False), [])

    def test_no_reload_flag(self):
        self.assertIn("--no-reload", self.words("udevmon devices add --"))
        self.assertIn("--no-reload", self.words("udevmon actions remove --"))
        self.assertNotIn("--no-reload", self.words("udevmon devices list --"))

    def test_bash_splits_at_equals_signs(self):
        """`--name a=b` reaches us as --name, a, =, b; the "=" must not count as an argument."""
        words = ["udevmon", "--config", str(self.cfg), "actions", "add", "--tag", "t",
                 "--name", "a", "=", "b", "mo"]
        self.assertEqual(completion.complete(self.parser, words, len(words) - 1), (WORDS, ["mounted"]))
        words = ["udevmon", "--config", str(self.cfg), "actions", "add", "--timeout", "5",
                 "--tag", "u"]
        self.assertEqual(completion.complete(self.parser, words, len(words) - 1), (WORDS, ["usbtest"]))

    def test_join_equals(self):
        j = completion._join_equals
        self.assertEqual(j(["--name", "a", "=", "b", "x"]), ["--name", "a=b", "x"])
        self.assertEqual(j(["a", "=", "b", "=", "c"]), ["a=b=c"])
        self.assertEqual(j(["--config", "=", "/p"]), ["--config=/p"])
        self.assertEqual(j(["a", "="]), ["a="])
        self.assertEqual(j(["x", "y"]), ["x", "y"])

    def test_paths_and_the_command_tail(self):
        self.assertEqual(self.comp("udevmon --config ", config=False), (FILES, []))
        self.assertEqual(self.comp("udevmon service install --dest "), (DIRS, []))
        self.assertEqual(self.comp("udevmon completion install --dest "), (FILES, []))
        self.assertEqual(self.comp("udevmon service uninstall --dest "), (DIRS, []))
        self.assertEqual(self.comp("udevmon completion uninstall --dest "), (FILES, []))
        self.assertEqual(self.comp("udevmon actions add --tag t connected -- no"), (FILES, []))
        self.assertEqual(self.comp("udevmon actions add --tag t connected -- "), (FILES, []))

    def test_nothing_more_to_offer(self):
        self.assertEqual(self.words("udevmon devices show stick "), [])      # only one argument
        self.assertEqual(self.words("udevmon check "), [])
        self.assertEqual(self.words("udevmon devices list "), [])

    def test_a_broken_or_missing_config_gives_no_names_and_no_error(self):
        self.cfg.write_text("[devices.x\n")
        self.assertEqual(self.words("udevmon devices show "), [])
        self.cfg.unlink()
        self.assertEqual(self.words("udevmon devices show "), [])
        self.cfg.write_text(CONFIG)
        self.cfg.chmod(0o666)                                # unsafe permissions: refused
        self.assertEqual(self.words("udevmon devices show "), [])

    def test_the_hidden_helper_prints_mode_then_candidates(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli.main(["_complete", "5", "udevmon", "--config", str(self.cfg), "devices", "show", ""])
        self.assertEqual(out.getvalue().split("\n")[:3], ["words", "stick", "disk"])
        with contextlib.redirect_stdout(io.StringIO()) as junk:
            self.assertEqual(cli.main(["_complete", "notanumber"]), 0)     # never fails loudly
        self.assertEqual(junk.getvalue(), "")


def _commands(parser, path=()):
    """Every (path, parser) below ``parser``, groups included."""
    sub = completion._subparsers(parser)
    if sub is None:
        return
    for name, child in sub.choices.items():
        yield path + (name,), child
        yield from _commands(child, path + (name,))


def _positionals(node):
    return [a for a in node._actions
            if not a.option_strings and not isinstance(a, argparse._SubParsersAction)]


def _value_options(node):
    return [a for a in node._actions if a.option_strings and completion._takes_value(a)
            and a.dest not in ("help", "version")]


def drift_problems(parser, cfg_path):
    """What completion does not cover (or covers wrongly) in ``parser``; [] when in step."""
    problems = []
    seen = set()
    fill = {"device": "stick", "tag": "usbtest", "declared-tag": "storage", "action": "log",
            "device-tag": "usbtest", "events": "connected", "event": "connected",
            "rule-tag": "storage", "device-exclusion": "storage"}
    for path, node in [((), parser), *_commands(parser)]:      # the top level has options too
        seen.add(path)
        positionals = _positionals(node)
        slots = completion.POSITIONALS.get(path)
        if positionals and slots is None and path not in completion.NO_COMPLETION:
            problems.append(f"{' '.join(path)}: positional argument(s) "
                            f"{[a.dest for a in positionals]} are not in POSITIONALS or NO_COMPLETION")
        if slots is not None:
            if len(slots) != len(positionals):
                problems.append(f"{' '.join(path)}: POSITIONALS has {len(slots)} slot(s), "
                                f"the parser {len(positionals)}")
            else:
                for i, (slot, action) in enumerate(zip(slots, positionals)):
                    repeats = action.nargs in ("+", "*")
                    if slot.endswith("*") != repeats:
                        problems.append(f"{' '.join(path)}: slot {slot!r} and nargs "
                                        f"{action.nargs!r} disagree on repetition")
                    if slot.rstrip("*") not in completion.SLOT_KINDS:
                        problems.append(f"{' '.join(path)}: unknown slot kind {slot!r}")
                        continue
                    if slot.rstrip("*") in completion.FREE_SLOT_KINDS:
                        continue                       # a new name: nothing to offer
                    before = [fill[k.rstrip("*")] for k in slots[:i]]
                    words = ["udevmon", "--config", str(cfg_path), *path, *before, ""]
                    if not completion.complete(parser, words, len(words) - 1)[1]:
                        problems.append(f"{' '.join(path)}: slot {slot!r} offers nothing")
        for action in _value_options(node):
            dest = action.dest
            words = ["udevmon", "--config", str(cfg_path), *path, action.option_strings[0], ""]
            result = completion.complete(parser, words, len(words) - 1)
            if dest in completion.FREE_TEXT_DESTS:
                if result != (WORDS, []):
                    problems.append(f"{' '.join(path)} {action.option_strings[0]}: free text "
                                    f"should offer nothing, got {result}")
            elif dest in completion.OPTION_VALUE_DESTS:
                if result == (WORDS, []):
                    problems.append(f"{' '.join(path)} {action.option_strings[0]}: offers nothing")
            else:
                problems.append(f"{' '.join(path)} {action.option_strings[0]}: option value "
                                f"{dest!r} is in neither OPTION_VALUE_DESTS nor FREE_TEXT_DESTS")
    for name in (*completion.POSITIONALS, *completion.NO_COMPLETION):
        if name not in seen:
            problems.append(f"{' '.join(name)}: listed in the completion tables but not in the CLI")
    both = set(completion.POSITIONALS) & completion.NO_COMPLETION
    problems += [f"{' '.join(n)}: in both POSITIONALS and NO_COMPLETION" for n in sorted(both)]
    return problems


class CliDriftTests(unittest.TestCase):
    """Fail when the CLI grows something completion does not know about."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = Path(self.tmp.name) / "c.toml"
        self.cfg.write_text(CONFIG)
        self.cfg.chmod(0o600)

    def test_completion_covers_the_whole_cli(self):
        self.assertEqual(drift_problems(cli.build_parser(), self.cfg), [])

    def test_a_new_command_with_a_positional_is_caught(self):
        parser = cli.build_parser()
        sub = completion._subparsers(parser).choices["tags"]
        group = completion._subparsers(sub)
        group.add_parser("frobnicate").add_argument("name")
        problems = drift_problems(parser, self.cfg)
        self.assertEqual(len(problems), 1)
        self.assertIn("tags frobnicate", problems[0])

    def test_a_new_value_option_is_caught(self):
        parser = cli.build_parser()
        completion._subparsers(parser).choices["scan"].add_argument("--format")
        problems = drift_problems(parser, self.cfg)
        self.assertEqual(len(problems), 1)
        self.assertIn("--format", problems[0])

    def test_stale_and_inconsistent_tables_are_caught(self):
        parser = cli.build_parser()
        with mock.patch.dict(completion.POSITIONALS, {("devices", "gone"): ["device"]}):
            self.assertTrue(any("not in the CLI" in p for p in drift_problems(parser, self.cfg)))
        with mock.patch.dict(completion.POSITIONALS, {("devices", "tag"): ["device"]}):
            self.assertTrue(any("slot(s)" in p for p in drift_problems(parser, self.cfg)))
        with mock.patch.dict(completion.POSITIONALS, {("devices", "show"): ["device*"]}):
            self.assertTrue(any("repetition" in p for p in drift_problems(parser, self.cfg)))
        with mock.patch.dict(completion.POSITIONALS, {("devices", "show"): ["nonsense"]}):
            self.assertTrue(any("unknown slot kind" in p for p in drift_problems(parser, self.cfg)))
        with mock.patch.object(completion, "NO_COMPLETION", completion.NO_COMPLETION | {("devices", "show")}):
            self.assertTrue(any("both" in p for p in drift_problems(parser, self.cfg)))

    def test_a_dropped_entry_or_handler_is_caught(self):
        parser = cli.build_parser()
        tables = {k: v for k, v in completion.POSITIONALS.items() if k != ("actions", "remove")}
        with mock.patch.object(completion, "POSITIONALS", tables):
            self.assertTrue(any("actions remove" in p for p in drift_problems(parser, self.cfg)))
        with mock.patch.object(completion, "OPTION_VALUE_DESTS", completion.OPTION_VALUE_DESTS - {"match"}):
            self.assertTrue(any("--match" in p for p in drift_problems(parser, self.cfg)))
        with mock.patch.object(completion, "FREE_TEXT_DESTS", completion.FREE_TEXT_DESTS - {"name"}):
            self.assertTrue(any("--name" in p for p in drift_problems(parser, self.cfg)))

class InstallTests(unittest.TestCase):
    def run_cli(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = cli.main(list(args))
        return rc, out.getvalue()

    def test_prints_the_script(self):
        rc, out = self.run_cli("completion", "bash")
        self.assertEqual(rc, 0)
        self.assertIn("complete -F _udevmon_complete udevmon", out)
        self.assertEqual(out, completion.SCRIPT)

    def test_install_writes_to_the_users_completion_dir(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.dict(os.environ, {"XDG_DATA_HOME": d}):
            rc, out = self.run_cli("completion", "install")
            target = Path(d) / "bash-completion" / "completions" / "udevmon"
            self.assertEqual((rc, target.read_text()), (0, completion.SCRIPT))
            self.assertIn(str(target), out)
            rc, out = self.run_cli("completion", "install", "--dest", str(Path(d) / "x" / "y"))
            self.assertEqual((Path(d) / "x" / "y").read_text(), completion.SCRIPT)


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class BashFunctionTests(unittest.TestCase):
    """Run the real shell function with COMP_WORDS set up the way bash does."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = Path(self.tmp.name)
        self.cfg = d / "c.toml"
        self.cfg.write_text(CONFIG)
        self.cfg.chmod(0o600)
        self.bin = d / "bin"
        self.bin.mkdir()
        wrapper = self.bin / "udevmon"
        wrapper.write_text('#!/bin/sh\nexec python3 -m udevmon "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
        (d / "script.bash").write_text(completion.SCRIPT)

    def reply(self, *words):
        """COMPREPLY (one per line) when completing the last of ``words``."""
        src = str(Path(__file__).resolve().parents[1] / "src")
        env = {**os.environ, "PATH": f"{self.bin}:{os.environ['PATH']}", "PYTHONPATH": src}
        quoted = " ".join("'" + w + "'" for w in words)
        script = (f"source {self.tmp.name}/script.bash; COMP_WORDS=({quoted}); "
                  f"COMP_CWORD=$(( ${{#COMP_WORDS[@]}} - 1 )); _udevmon_complete; "
                  'printf "%s\\n" "${COMPREPLY[@]}"')
        done = subprocess.run(["bash", "--norc", "--noprofile", "-c", script], env=env,
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 0, done.stderr)
        return [line for line in done.stdout.split("\n") if line]

    def test_subcommands_and_names(self):
        self.assertEqual(self.reply("udevmon", "dev"), ["devices"])
        self.assertEqual(self.reply("udevmon", "--config", str(self.cfg), "devices", "show", "s"),
                         ["stick"])
        self.assertEqual(self.reply("udevmon", "--config", str(self.cfg), "actions", "remove", ""),
                         ["log", "note"])

    def test_files_and_key_names(self):
        (Path(self.tmp.name) / "afile.txt").write_text("x")
        self.assertEqual(self.reply("udevmon", "--config", f"{self.tmp.name}/afi"),
                         [f"{self.tmp.name}/afile.txt"])
        self.assertEqual(self.reply("udevmon", "devices", "add", "x", "--match", "seri"), ["serial="])

    def test_a_broken_helper_leaves_no_output(self):
        self.assertEqual(self.reply("udevmon", "--config", "/nonexistent/x", "devices", "show", ""), [])


if __name__ == "__main__":
    unittest.main()
