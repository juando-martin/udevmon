# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
import os
import tempfile
import unittest

from tests import _scratch


class IsolationTests(unittest.TestCase):
    """The suite must never leave files in the system temp directory (see tests/__init__)."""

    def test_temporary_files_go_to_the_per_run_folder(self):
        self.assertEqual(tempfile.gettempdir(), _scratch.name)
        self.assertEqual(os.environ["TMPDIR"], _scratch.name)
        made = tempfile.mkdtemp()
        self.addCleanup(lambda: None)
        self.assertTrue(made.startswith(_scratch.name + os.sep))

    def test_the_runtime_directory_is_private_to_the_run(self):
        self.assertTrue(os.environ["XDG_RUNTIME_DIR"].startswith(_scratch.name + os.sep))


if __name__ == "__main__":
    unittest.main()
