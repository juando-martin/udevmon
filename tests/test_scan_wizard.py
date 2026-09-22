"""The scan-mode registration wizard: choosing device vs. category, the confirm-and-edit
screen (rename, change match/rule, change tags, cancel), and the small helpers it is built
from."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from udevmon import config as C
from udevmon.events import Endpoint
from udevmon.scan import Scanner

YUBIKEY = Endpoint("udev", "/sys/yk", {"vendor_id": "1050", "product_id": "0407",
                                       "port_path": "3-4", "vendor": "Yubico",
                                       "model": "Yubikey 4"})
STICK = Endpoint("udev", "/sys/stick", {"vendor_id": "0781", "product_id": "55ae",
                                        "serial": "323434393551343035323530", "vendor": "SanDisk",
                                        "model": "Extreme"})
FS = Endpoint("udisks", "/org/x", {"fs_uuid": "U1", "fs_label": "BACKUP"})


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.p = Path(tmp.name) / "c.toml"
        self.scanner = Scanner(self.p, prompt=True)

    def run_with(self, answers, endpoint=YUBIKEY, ignore=False):
        seq, prompts = iter(answers), []

        def fake(prompt=""):
            prompts.append(prompt)
            return next(seq)
        out = io.StringIO()
        with mock.patch("builtins.input", fake), \
                mock.patch("udevmon.daemon.reload_running", return_value=False), \
                contextlib.redirect_stdout(out):
            result = self.scanner._register_endpoint(endpoint, ignore=ignore)
        return result, out.getvalue(), prompts


class ChooseKindTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.scanner = Scanner(Path(tmp.name) / "c.toml", prompt=True)

    def choose(self, endpoint, answer):
        with mock.patch("builtins.input", return_value=answer), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            return self.scanner._choose_kind(endpoint), out.getvalue()

    def test_defaults(self):
        kind, out = self.choose(YUBIKEY, "")
        self.assertEqual(kind, "category")
        self.assertIn("no serial number", out)
        self.assertEqual(self.choose(STICK, "")[0], "device")

    def test_explicit_letters_and_words(self):
        for answer in ("d", "device"):
            self.assertEqual(self.choose(YUBIKEY, answer)[0], "device")
        for answer in ("c", "category"):
            self.assertEqual(self.choose(STICK, answer)[0], "category")

    def test_q_skips(self):
        self.assertIsNone(self.choose(YUBIKEY, "q")[0])

    def test_unknown_answer_reprompts(self):
        with mock.patch("builtins.input", side_effect=["nonsense", "d"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            kind = self.scanner._choose_kind(YUBIKEY)
        self.assertEqual(kind, "device")
        self.assertIn("Not a valid choice.", out.getvalue())


class AskNameTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.scanner = Scanner(Path(tmp.name) / "c.toml", prompt=True)

    def test_blank_with_no_default_means_skip(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertIsNone(self.scanner._ask_name("prompt: "))

    def test_blank_with_a_default_keeps_it(self):
        with mock.patch("builtins.input", return_value=""):
            self.assertEqual(self.scanner._ask_name("prompt: ", default="old"), "old")

    def test_a_typed_name_overrides_the_default(self):
        with mock.patch("builtins.input", return_value="new-name"):
            self.assertEqual(self.scanner._ask_name("prompt: ", default="old"), "new-name")

    def test_an_invalid_name_reprompts(self):
        with mock.patch("builtins.input", side_effect=["bad name!", "ok-name"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(self.scanner._ask_name("prompt: "), "ok-name")
        self.assertIn("letters, digits, _ . -", out.getvalue())


class AskTagsTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.scanner = Scanner(Path(tmp.name) / "c.toml", prompt=True)

    def ask(self, answer, current=None):
        with mock.patch("builtins.input", return_value=answer), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            return self.scanner._ask_tags(current=current), out.getvalue()

    def test_blank_with_no_current_means_no_tags(self):
        self.assertEqual(self.ask("")[0], [])

    def test_blank_with_current_keeps_it(self):
        self.assertEqual(self.ask("", current=["a", "b"])[0], ["a", "b"])

    def test_typing_replaces_it_entirely(self):
        self.assertEqual(self.ask("x, y", current=["a", "b"])[0], ["x", "y"])

    def test_dash_clears_it(self):
        self.assertEqual(self.ask("-", current=["a", "b"])[0], [])

    def test_an_invalid_tag_name_reprompts(self):
        with mock.patch("builtins.input", side_effect=["bad tag!", "ok"]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            tags = self.scanner._ask_tags()
        self.assertEqual(tags, ["ok"])
        self.assertIn("letters, digits, _ . -", out.getvalue())


class DeviceConfirmTests(Base):
    def match(self, name="yk"):
        return dict(C.load(self.p).devices[name].match)

    def test_strong_match_saves_on_blank(self):
        # kind(d) name match(blank->serial default) tags(blank) confirm(blank->save, strong)
        ok, out, _ = self.run_with(["d", "s1", "", "", ""], endpoint=STICK)
        self.assertTrue(ok)
        self.assertEqual(self.match("s1")["serial"], STICK.props["serial"])
        self.assertIn("device 's1'", out)

    def test_weak_match_rejects_blank_and_needs_y(self):
        # kind(d) name match(2: any port, weak) tags(blank) confirm(blank -> reminder) confirm(y)
        ok, out, _ = self.run_with(["d", "yk", "2", "", "", "y"])
        self.assertTrue(ok)
        self.assertIn("This match is weak", out)
        self.assertEqual(self.match(), {"vendor_id": "1050", "product_id": "0407"})

    def test_rename_at_confirm_keeps_the_rest(self):
        ok, out, _ = self.run_with(["d", "yk", "1", "storage", "r", "the-key", "", "y"])
        self.assertTrue(ok)
        self.assertNotIn("yk", C.load(self.p).devices)
        d = C.load(self.p).devices["the-key"]
        self.assertEqual(d.tags, ("storage",))

    def test_rename_blank_keeps_the_current_name(self):
        ok, _, _ = self.run_with(["d", "yk", "1", "", "r", "", "y"])
        self.assertTrue(ok)
        self.assertIn("yk", C.load(self.p).devices)

    def test_change_match_at_confirm(self):
        # start with choice 1 (port), then at confirm pick 'm' and switch to choice 2 (any port)
        ok, out, _ = self.run_with(["d", "yk", "1", "", "m", "2", "y"])
        self.assertTrue(ok)
        self.assertEqual(self.match(), {"vendor_id": "1050", "product_id": "0407"})

    def test_skipping_the_match_edit_keeps_the_previous_one(self):
        ok, out, _ = self.run_with(["d", "yk", "1", "", "m", "", "y"])
        self.assertTrue(ok)
        self.assertIn("Kept the previous match.", out)
        self.assertEqual(self.match(), {"vendor_id": "1050", "product_id": "0407",
                                        "port_path": "3-4"})

    def test_change_tags_at_confirm(self):
        ok, _, _ = self.run_with(["d", "yk", "1", "old", "t", "new,tags", "y"])
        self.assertTrue(ok)
        self.assertEqual(C.load(self.p).devices["yk"].tags, ("new", "tags"))

    def test_blank_at_the_tags_edit_keeps_the_current_tags(self):
        ok, _, _ = self.run_with(["d", "yk", "1", "old", "t", "", "y"])
        self.assertTrue(ok)
        self.assertEqual(C.load(self.p).devices["yk"].tags, ("old",))

    def test_cancel_at_confirm_saves_nothing(self):
        for word in ("c", "cancel", "q", "n", "no"):
            with self.subTest(word):
                p = Path(tempfile.mkdtemp()) / "c.toml"
                scanner = Scanner(p, prompt=True)
                seq = iter(["d", "yk", "1", "", word])

                def fake(prompt="", seq=seq):
                    return next(seq)
                with mock.patch("builtins.input", fake), contextlib.redirect_stdout(io.StringIO()):
                    ok = scanner._register_endpoint(YUBIKEY)
                self.assertFalse(ok)
                self.assertFalse(p.exists())

    def test_a_duplicate_name_lets_the_user_rename_and_retry(self):
        C.tomllib
        self.p.write_text('[devices.taken]\nmatch = { serial = "X" }\n')
        self.p.chmod(0o600)
        ok, out, _ = self.run_with(["d", "taken", "1", "", "y", "r", "free", "", "y"])
        self.assertTrue(ok)
        self.assertIn("Could not save", out)
        self.assertIn("free", C.load(self.p).devices)
        self.assertNotIn("free", C.load(self.p).devices["taken"].match)  # 'taken' untouched
        self.assertEqual(C.load(self.p).devices["taken"].match, {"serial": "X"})

    def test_unknown_answer_at_confirm_reprompts(self):
        ok, out, _ = self.run_with(["d", "yk", "1", "", "zzz", "y"])
        self.assertTrue(ok)
        self.assertIn("Not a valid choice.", out)


class IgnoreConfirmTests(Base):
    def test_ignoring_has_no_tags_option_and_sets_status(self):
        ok, out, prompts = self.run_with(["root-hub", "1", "y"], ignore=True)
        self.assertTrue(ok)
        d = C.load(self.p).devices["root-hub"]
        self.assertEqual((d.status, d.tags), ("ignored", ()))
        self.assertFalse(any(p.startswith("[Enter] save") and "tags" in p for p in prompts))
        self.assertIn("[ignored]", out)
        self.assertNotIn("Add an action", out)

    def test_rename_and_change_match_still_work_while_ignoring(self):
        ok, _, _ = self.run_with(["root-hub", "1", "r", "the-hub", "m", "2", "y"], ignore=True)
        self.assertTrue(ok)
        self.assertIn("the-hub", C.load(self.p).devices)
        self.assertEqual(C.load(self.p).devices["the-hub"].match,
                         {"vendor_id": "1050", "product_id": "0407"})


class CategoryConfirmTests(Base):
    def rule(self, name="yk"):
        return {k: v for k, v in C.load(self.p).tags[name].match.items()}

    def test_saves_on_blank_even_for_a_weak_rule(self):
        ok, out, _ = self.run_with(["c", "yk", "2", ""])
        self.assertTrue(ok)
        self.assertIn("category (tag) 'yk'", out)
        self.assertEqual(self.rule(), {"vendor_id": ("1050",), "product_id": ("0407",)})

    def test_rename_at_confirm(self):
        ok, _, _ = self.run_with(["c", "yk", "1", "r", "any-key", ""])
        self.assertTrue(ok)
        self.assertIn("any-key", C.load(self.p).tags)
        self.assertNotIn("yk", C.load(self.p).tags)

    def test_change_rule_at_confirm(self):
        ok, _, _ = self.run_with(["c", "yk", "1", "m", "2", ""])
        self.assertTrue(ok)
        self.assertEqual(self.rule(), {"vendor_id": ("1050",), "product_id": ("0407",)})

    def test_skipping_the_rule_edit_keeps_the_previous_one(self):
        ok, out, _ = self.run_with(["c", "yk", "1", "m", "", ""])
        self.assertTrue(ok)
        self.assertIn("Kept the previous rule.", out)
        self.assertEqual(self.rule(), {"vendor_id": ("1050",), "product_id": ("0407",),
                                       "port_path": ("3-4",)})

    def test_cancel_at_confirm(self):
        ok, _, _ = self.run_with(["c", "yk", "1", "c"])
        self.assertFalse(ok)
        self.assertFalse(self.p.exists())

    def test_a_duplicate_tag_name_lets_the_user_rename_and_retry(self):
        self.p.write_text('[tags.taken]\nmatch = { vendor_id = "9999" }\n')
        self.p.chmod(0o600)
        ok, out, _ = self.run_with(["c", "taken", "1", "", "r", "free", ""])
        self.assertTrue(ok)
        self.assertIn("Could not save", out)
        self.assertIn("free", C.load(self.p).tags)
        self.assertEqual(dict(C.load(self.p).tags["taken"].match), {"vendor_id": ("9999",)})

    def test_after_save_hints_allow_unnamed_and_tag_action(self):
        ok, out, _ = self.run_with(["c", "yk", "1", ""])
        self.assertTrue(ok)
        self.assertIn("udevmon actions add --tag yk connected --allow-unnamed -- "
                      "notify-send 'hello'", out)

    def test_mounted_event_hint_for_a_filesystem(self):
        # FS has an fs_uuid, so choose_keys uses the ordinary "accept the default" prompt.
        ok, out, _ = self.run_with(["c", "storage", "", ""], endpoint=FS)
        self.assertTrue(ok)
        self.assertIn("udevmon actions add --tag storage mounted --allow-unnamed", out)


class MultiSelectMixedKindTests(unittest.TestCase):
    def test_one_endpoint_becomes_a_device_and_the_other_a_category(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = Path(tmp.name) / "c.toml"
        scanner = Scanner(p, prompt=True)
        scanner.loop = mock.Mock()
        scanner.pending = {"a": STICK, "b": YUBIKEY}
        answers = iter([
            "1,2",
            "d", "stick", "", "", "",          # STICK: device, strong, no tags, confirm blank
            "c", "keys", "1", "",              # YUBIKEY: category, port rule, confirm blank
        ])
        with mock.patch("builtins.input", lambda prompt="": next(answers)), \
                mock.patch("udevmon.daemon.reload_running", return_value=False), \
                contextlib.redirect_stdout(io.StringIO()):
            scanner._prompt_user()
        cfg = C.load(p)
        self.assertIn("stick", cfg.devices)
        self.assertIn("keys", cfg.tags)
        scanner.loop.quit.assert_not_called()


class SaveMessageTests(Base):
    def test_device_hint_uses_device_and_connected(self):
        ok, out, _ = self.run_with(["d", "yk", "1", "", "y"])
        self.assertTrue(ok)
        self.assertIn("udevmon actions add --device yk connected -- notify-send 'hello'", out)

    def test_reload_and_pause_notices(self):
        with mock.patch("udevmon.daemon.reload_running", return_value=True):
            seq = iter(["d", "yk", "1", "", "y"])
            with mock.patch("builtins.input", lambda p="": next(seq)), \
                    mock.patch("udevmon.pause.status", return_value=(True, None)), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                ok = self.scanner._register_endpoint(YUBIKEY)
        self.assertTrue(ok)
        self.assertIn("Reloaded the running daemon.", out.getvalue())
        self.assertIn("automation is paused", out.getvalue())


if __name__ == "__main__":
    unittest.main()
