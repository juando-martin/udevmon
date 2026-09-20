import os
import re
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from udevmon import _scm

HAVE_GIT = shutil.which("git") is not None
ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null",
       "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
TODAY = lambda: datetime.now(timezone.utc).strftime("%Y%m%d")   # noqa: E731


class Repo:
    def __init__(self, path):
        self.path = Path(path)
        self.git("init", "-q", "-b", "main")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.path), *args], check=True, env=ENV,
                              capture_output=True, text=True).stdout.strip()

    def commit(self, n=1):
        for _ in range(n):
            f = self.path / "f.txt"
            f.write_text((f.read_text() if f.exists() else "") + "x")
            self.git("add", "f.txt")
            self.git("commit", "-qm", "c")

    def tag(self, name):
        self.git("tag", name)

    def dirty(self):
        (self.path / "f.txt").write_text("changed")

    def node(self):
        return "g" + self.git("rev-parse", "HEAD")[:9]


@unittest.skipUnless(HAVE_GIT, "git is not installed")
class GitVersionTests(unittest.TestCase):
    """The strings below are what setuptools-scm's defaults produce (checked against it)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Repo(self.tmp.name)

    def version(self):
        return _scm.git_version(self.repo.path)

    def test_no_tag_counts_commits(self):
        self.repo.commit(3)
        self.assertEqual(self.version(), f"0.1.dev3+{self.repo.node()}")

    def test_exact_tag_is_just_the_tag(self):
        self.repo.commit(2)
        self.repo.tag("v0.1.0")
        self.assertEqual(self.version(), "0.1.0")

    def test_commits_after_a_tag_bump_the_last_number(self):
        self.repo.commit()
        self.repo.tag("v0.1.0")
        self.repo.commit(2)
        self.assertEqual(self.version(), f"0.1.1.dev2+{self.repo.node()}")

    def test_uncommitted_changes_add_the_build_date(self):
        self.repo.commit(2)
        self.repo.dirty()
        self.assertEqual(self.version(), f"0.1.dev2+{self.repo.node()}.d{TODAY()}")

    def test_dirty_at_a_tag_is_not_the_release(self):
        self.repo.commit()
        self.repo.tag("v1.2.3")
        self.repo.dirty()
        self.assertEqual(self.version(), f"1.2.4.dev0+{self.repo.node()}.d{TODAY()}")

    def test_untracked_files_do_not_count(self):
        self.repo.commit()
        self.repo.tag("v1.0")
        (self.repo.path / "new.txt").write_text("u")
        self.assertEqual(self.version(), "1.0")

    def test_tag_forms(self):
        cases = [("v2.0", "2.1"), ("7", "8"), ("1.0rc1", "1.0rc2"), ("release-3.4.5", "3.4.6"),
                 ("0.9.9", "0.9.10")]
        for tag, bumped in cases:
            with self.subTest(tag=tag):
                with tempfile.TemporaryDirectory() as d:
                    repo = Repo(d)
                    repo.commit()
                    repo.tag(tag)
                    repo.commit()
                    self.assertEqual(_scm.git_version(repo.path), f"{bumped}.dev1+{repo.node()}")

    def test_latest_tag_wins_and_other_tags_are_ignored(self):
        self.repo.commit()
        self.repo.tag("v0.1.0")
        self.repo.commit()
        self.repo.tag("v0.2.0")
        self.repo.tag("latest")
        self.repo.commit(2)
        self.assertEqual(self.version(), f"0.2.1.dev2+{self.repo.node()}")

    def test_only_the_top_of_a_checkout_counts(self):
        self.repo.commit()
        sub = self.repo.path / "sub"
        sub.mkdir()
        self.assertIsNone(_scm.git_version(sub))

    def test_not_a_repository_or_no_commits(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(_scm.git_version(Path(d)))
        with tempfile.TemporaryDirectory() as d:
            Repo(d)
            self.assertIsNone(_scm.git_version(Path(d)))


class FallbackTests(unittest.TestCase):
    def test_pkg_info_then_unknown(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(_scm.get_version(Path(d)), _scm.FALLBACK)
            (Path(d) / "PKG-INFO").write_text("Metadata-Version: 2.1\nName: x\nVersion: 3.4.5.dev2+gabc\n")
            self.assertEqual(_scm.get_version(Path(d)), "3.4.5.dev2+gabc")

    def test_missing_git_never_raises(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.dict(os.environ, {"PATH": ""}):
                self.assertEqual(_scm.get_version(Path(d)), _scm.FALLBACK)

    def test_version_tuple_and_file(self):
        self.assertEqual(_scm.version_tuple("0.1.1.dev5+gabc123.d20260920"),
                         (0, 1, 1, "dev5", "gabc123.d20260920"))
        self.assertEqual(_scm.version_tuple("1.2.3"), (1, 2, 3))
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "_version.py"
            _scm.write_version_file(path, "0.1.dev2+gabc")
            ns = {}
            exec(path.read_text(), ns)
            self.assertEqual((ns["__version__"], ns["version"], ns["version_tuple"]),
                             ("0.1.dev2+gabc", "0.1.dev2+gabc", (0, 1, "dev2", "gabc")))


class PackageVersionTests(unittest.TestCase):
    def test_version_is_a_pep440_string(self):
        import udevmon
        self.assertRegex(udevmon.__version__, r"^\d+(\.\d+)*([.\w+-]*)$")
        with self.assertRaises(AttributeError):
            udevmon.nope


if __name__ == "__main__":
    unittest.main()
