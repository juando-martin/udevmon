# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Sphinx configuration. Offline: importing it reads no user config and starts nothing."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "docs" / "_ext"))

from udevmon import _scm  # noqa: E402  (standard library only)

project = "udevmon"
copyright = "2026, Juan Domingo Martín"
release = _scm.get_version(ROOT)       # from git; in an sdist, from PKG-INFO
extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.napoleon",
    "sphinx.ext.viewcode",
    "cli_help",
    "module_reference",
]
html_theme = "classic"
html_static_path = ["_static"]
html_css_files = ["layout.css"]
exclude_patterns = ["_build"]
autodoc_member_order = "bysource"
autodoc_typehints = "none"
napoleon_google_docstring = True
napoleon_numpy_docstring = False
