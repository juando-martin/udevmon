# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""The manual pages must stay in step with the CLI and the config parser."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

from udevmon import cli, config as C
from udevmon.config import tomllib  # tomli on Python 3.10
from udevmon.events import VALID_KINDS

from .test_completion import _commands

MAN = Path(__file__).resolve().parent.parent / "man"
CMD_PAGE = MAN / "udevmon.1"
CFG_PAGE = MAN / "udevmon.toml.5"


def plain(page: Path) -> str:
    """The page's source with roff escapes and font changes removed."""
    text = page.read_text()
    text = text.replace("\\-", "-").replace("\\e", "\\")
    text = re.sub(r"\\f[BIRP]", "", text)
    text = re.sub(r"^\.[A-Za-z]+ ", "", text, flags=re.M)
    return text


def _options(parser):
    for path, node in [((), parser), *_commands(parser)]:
        for action in node._actions:
            for opt in action.option_strings:
                if opt.startswith("--"):
                    yield path, opt


class ManualPageTests(unittest.TestCase):
    def test_pages_exist_and_have_the_standard_sections(self):
        for page, sections in ((CMD_PAGE, ["NAME", "SYNOPSIS", "DESCRIPTION", "FILES",
                                           "EXIT STATUS", "EXAMPLES", "SEE ALSO"]),
                               (CFG_PAGE, ["NAME", "DESCRIPTION", "EXAMPLE", "FILES",
                                           "SEE ALSO"])):
            text = page.read_text()
            for s in sections:
                self.assertIn(f".SH {s}", text, f"{page.name} lacks section {s}")

    @unittest.skipUnless(shutil.which("groff"), "groff is not installed")
    def test_pages_have_no_roff_warnings(self):
        for page in (CMD_PAGE, CFG_PAGE):
            p = subprocess.run(["groff", "-man", "-ww", "-z", str(page)],
                               capture_output=True, text=True)
            self.assertEqual(p.stderr, "", f"{page.name}: {p.stderr}")

    def test_every_command_is_described(self):
        text = plain(CMD_PAGE)
        missing = ["udevmon " + " ".join(path) for path, _ in _commands(cli.build_parser())
                   if not re.search(r"udevmon " + r" ".join(map(re.escape, path)) + r"\b", text)]
        self.assertEqual(missing, [], "commands missing from man/udevmon.1")

    def test_every_option_is_described(self):
        text = plain(CMD_PAGE)
        missing = sorted({f"{' '.join(path) or 'udevmon'} {opt}" for path, opt in
                          _options(cli.build_parser())
                          if opt != "--help" and opt not in text})
        self.assertEqual(missing, [], "options missing from man/udevmon.1")

    def test_every_config_key_and_event_is_described(self):
        text = plain(CFG_PAGE)
        keys = C._DAEMON | C._DEVICE | C._TAG | C._ACTION | set(VALID_KINDS)
        missing = sorted(k for k in keys if not re.search(rf"\b{re.escape(k)}\b", text))
        self.assertEqual(missing, [], "keys missing from man/udevmon.toml.5")

    def test_every_property_is_described(self):
        from udevmon.events import KNOWN_KEYS
        text = plain(CFG_PAGE)
        props = set().union(*KNOWN_KEYS.values()) - {"event"}
        missing = sorted(p for p in props if not re.search(rf"\b{re.escape(p)}\b", text))
        self.assertEqual(missing, [], "properties missing from man/udevmon.toml.5")

    def test_the_example_config_is_valid(self):
        text = CFG_PAGE.read_text()
        block = text[text.index(".SH EXAMPLE"):text.index(".SH FILES")]
        block = block.split(".nf\n", 1)[1].rsplit(".fi", 1)[0].replace("\\-", "-")
        cfg = C.parse(tomllib.loads(block))
        self.assertEqual(cfg.warnings, [])
        self.assertTrue(cfg.devices and cfg.actions and cfg.tags)

    def test_the_pages_refer_to_each_other(self):
        self.assertIn("udevmon.toml (5)", CMD_PAGE.read_text().replace(".BR udevmon.toml (5)",
                                                                       "udevmon.toml (5)"))
        self.assertIn("udevmon (1)", CFG_PAGE.read_text().replace(".BR udevmon (1)",
                                                                  "udevmon (1)"))


if __name__ == "__main__":
    unittest.main()
