# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Every Python file carries the license header; GPLv3+ metadata."""

import unittest
from pathlib import Path

try:
    import tomllib
except ImportError:                               # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
HEADER = (
    "# SPDX-License-Identifier: GPL-3.0-or-later\n"
    "# Copyright (C) 2026 Juan Domingo Martín\n"
)
SKIP = {"_version.py"}                            # written by the build (setup.py)


def sources():
    yield ROOT / "setup.py"
    for directory in ("src", "tests", "tools", "docs", "contrib"):
        for path in sorted((ROOT / directory).rglob("*.py")):
            if ("__pycache__" in path.parts or "_build" in path.parts
                    or path.name in SKIP):
                continue
            yield path


class LicenseTest(unittest.TestCase):
    def test_every_python_file_starts_with_the_header(self):
        missing = []
        for path in sources():
            text = path.read_text(encoding="utf-8")
            if text.startswith("#!"):
                text = text.partition("\n")[2]
            if not text.startswith(HEADER):
                missing.append(str(path.relative_to(ROOT)))
        self.assertEqual(missing, [], "add the two-line SPDX header (DEVELOPMENT.md)")

    def test_license_text_and_metadata(self):
        for folder in (ROOT, ROOT / "contrib" / "rclone-config"):
            with self.subTest(folder=folder.name):
                text = (folder / "LICENSE").read_text(encoding="utf-8")
                self.assertIn("GNU GENERAL PUBLIC LICENSE", text[:200])
                self.assertIn("Version 3, 29 June 2007", text[:200])
                self.assertIn("END OF TERMS AND CONDITIONS", text)
                project = tomllib.loads((folder / "pyproject.toml").read_text())["project"]
                self.assertEqual(project["license"], "GPL-3.0-or-later")
                self.assertEqual(project["license-files"], ["LICENSE"])


if __name__ == "__main__":
    unittest.main()
