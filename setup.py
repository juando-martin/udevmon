"""The version comes from git (see src/udevmon/_scm.py); everything else is in pyproject.toml."""

import sys
from pathlib import Path

from setuptools import setup
from setuptools.command.build_py import build_py

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
from udevmon import _scm  # noqa: E402  (standard library only)

VERSION = _scm.get_version(ROOT)


class build_py_with_version(build_py):
    """Also write udevmon/_version.py into the built package."""

    def run(self):
        super().run()
        _scm.write_version_file(Path(self.build_lib) / "udevmon" / "_version.py", VERSION)


setup(
    version=VERSION,
    cmdclass={"build_py": build_py_with_version},
    # a user install puts these under ~/.local/share/man, where `man` looks
    data_files=[("share/man/man1", ["man/udevmon.1"]),
                ("share/man/man5", ["man/udevmon.toml.5"])],
)
