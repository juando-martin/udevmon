# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ["README.md", "SPECIFICATIONS.md", "CHANGELOG.md", "DEVELOPMENT.md"]
CODE_LIMIT = 76          # widest code line that still fits an 80-column glow


def lines_of(name):
    return (ROOT / name).read_text().split("\n")


class DocTests(unittest.TestCase):
    """The docs are read in a terminal with glow; keep them renderable at 80 columns."""

    def test_code_fences_are_balanced_and_never_doubled(self):
        for name in DOCS:
            fences = [n for n, l in enumerate(lines_of(name), 1) if l.startswith("```")]
            self.assertEqual(len(fences) % 2, 0, f"{name}: unbalanced code fences")
            doubled = [a for a, b in zip(fences, fences[1:]) if b == a + 1]
            self.assertEqual(doubled, [], f"{name}: an empty code block (doubled fence) at {doubled}")

    def test_code_lines_fit_an_80_column_glow(self):
        for name in DOCS:
            inside = False
            for n, line in enumerate(lines_of(name), 1):
                if line.startswith("```"):
                    inside = not inside
                elif inside and len(line) > CODE_LIMIT:
                    self.fail(f"{name}:{n}: code line is {len(line)} characters "
                              f"(limit {CODE_LIMIT}): {line[:50]}...")


if __name__ == "__main__":
    unittest.main()
