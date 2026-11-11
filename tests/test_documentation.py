# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""The HTML manual: tools/docs.py (strict build, loopback-only preview) and an offline build.

The build test needs Sphinx (the ``docs`` extra) and is skipped without it.
"""

import contextlib
import functools
import importlib.util
import io
import os
import socket
import subprocess
import tempfile
import unittest
import warnings as pywarnings
from pathlib import Path
from unittest.mock import Mock, patch
from pkgutil import resolve_name

from tools import docs

ROOT = Path(__file__).resolve().parents[1]


class DocsToolTests(unittest.TestCase):
    def test_build_is_fresh_nitpicky_and_fails_on_warnings(self):
        output = ROOT / "docs" / "_build" / "test-html"
        with patch("tools.docs.subprocess.run") as run:
            docs.build(output)
        argv = run.call_args.args[0]
        for flag in ("-E", "-a", "-n", "-W", "--keep-going"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[-2:], [str(ROOT / "docs"), str(output)])
        self.assertTrue(run.call_args.kwargs["check"])

    def test_preview_binds_only_loopback_and_closes_on_ctrl_c(self):
        server = Mock(server_port=8123)
        server.serve_forever.side_effect = KeyboardInterrupt
        manager = Mock()
        manager.__enter__ = Mock(return_value=server)
        manager.__exit__ = Mock(return_value=False)
        with patch("tools.docs.build") as build, \
                patch("tools.docs.ThreadingHTTPServer", return_value=manager) as factory, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(docs.main(["serve", "--port", "8123"]), 0)
        build.assert_called_once()
        self.assertEqual(factory.call_args.args[0], ("127.0.0.1", 8123))
        manager.__exit__.assert_called_once()

    def test_relative_output_is_resolved_before_sphinx_changes_folder(self):
        here = os.getcwd()
        with tempfile.TemporaryDirectory() as temp, \
                patch("tools.docs.subprocess.run") as run:
            os.chdir(temp)
            try:
                docs.build(Path("html"))
            finally:
                os.chdir(here)
            self.assertEqual(run.call_args.args[0][-1],
                             str(Path(temp).resolve() / "html"))
            self.assertEqual(run.call_args.kwargs["cwd"], ROOT)

    def test_output_option_is_passed_to_the_build(self):
        with patch("tools.docs.build") as build:
            self.assertEqual(docs.main(["build", "--output", "/x/html"]), 0)
        build.assert_called_once_with(Path("/x/html"))

    def test_failed_build_is_neither_served_nor_reported_as_success(self):
        with patch("tools.docs.build",
                   side_effect=subprocess.CalledProcessError(1, ["sphinx"])), \
                patch("tools.docs.ThreadingHTTPServer") as serve, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(docs.main(["serve"]), 1)
        serve.assert_not_called()
        self.assertIn("ERROR:", errors.getvalue())

    def test_bad_port_is_refused(self):
        with patch("tools.docs.build") as build, \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit):
            docs.main(["serve", "--port", "70000"])
        build.assert_not_called()


def _undocumented(path: Path) -> list[str]:
    """What autodoc would leave out of the manual in one module: public functions,
    classes and methods without a docstring, and public module constants and class
    fields without a ``#:`` comment (autodoc skips an undocumented member silently).
    A ``#:`` comment has no ": ", which napoleon would turn into a "Type" field."""
    import ast
    lines = path.read_text().splitlines()
    tree = ast.parse(path.read_text())
    missing = [] if ast.get_docstring(tree) else ["(module docstring)"]

    def commented(node, prefix) -> bool:
        line, above = lines[node.lineno - 1], lines[node.lineno - 2].strip()
        text = line.partition("#:")[2] if "#:" in line else above[2:] if above.startswith("#:") \
            else None
        if text is not None and ": " in text:   # napoleon would read "X: Y" as "type: text"
            missing.append(f"{prefix}{names(node)[0]} (no ': ' in a #: comment)")
        return text is not None

    def names(node) -> list[str]:
        target = node.targets[0] if isinstance(node, ast.Assign) else node.target
        found = target.elts if isinstance(target, ast.Tuple) else [target]
        return [n.id for n in found if isinstance(n, ast.Name)]

    def walk(body, prefix, in_class):
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if not node.name.startswith("_"):
                    if not ast.get_docstring(node):
                        missing.append(prefix + node.name)
                    if isinstance(node, ast.ClassDef):
                        walk(node.body, f"{prefix}{node.name}.", True)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                public = [n for n in names(node) if not n.startswith("_") and n != "log"]
                if public and len(names(node)) > 1:
                    missing.append(f"{prefix}{', '.join(public)} (one per line, to document)")
                elif public and not commented(node, prefix):
                    missing.append(prefix + public[0])
    walk(tree.body, "", False)
    return missing


class DocstringCoverageTests(unittest.TestCase):
    """Every module is in the manual through autodoc (:doc:`internals`, :doc:`modules`)."""

    def test_every_public_object_is_documented(self):
        package = ROOT / "src" / "udevmon"
        gaps = {}
        for path in sorted(package.rglob("*.py")):
            if path.name in ("__main__.py", "_version.py"):
                continue
            missing = _undocumented(path)
            if missing:
                gaps[str(path.relative_to(package))] = missing
        self.assertEqual(gaps, {}, "add a docstring, or a #: comment for a constant or field")


def _failing(original, error):
    """A stand-in for ``original`` that raises ``error``. It keeps its name, module and
    docstring, so autodoc still documents it (a Mock would vanish from the manual)."""
    @functools.wraps(original)
    def fail(*_args, **_kwargs):
        raise error
    return fail


@unittest.skipUnless(importlib.util.find_spec("sphinx"), "the docs extra (Sphinx) is not installed")
class OfflineBuildTests(unittest.TestCase):
    """Build the manual in-process with every way out of the sandbox patched to fail."""

    def test_build_reads_no_config_and_starts_nothing(self):
        from sphinx.application import Sphinx

        forbidden = AssertionError("building the docs must have no side effects")
        real_popen = subprocess.Popen

        def only_git(args, *a, **kw):       # the version comes from git (udevmon._scm)
            argv = [args] if isinstance(args, (str, bytes)) else list(args)
            if Path(os.fsdecode(argv[0])).name != "git":
                raise forbidden
            return real_popen(args, *a, **kw)

        guarded = [
            "udevmon.config.load", "udevmon.config.read_files",
            "udevmon.runtime.runtime_dir",
            "udevmon.daemon.Daemon.__init__", "udevmon.daemon.running_daemon",
            "udevmon.sources.udev.UdevSource.start", "udevmon.sources.udev.UdevSource.snapshot",
            "udevmon.sources.udisks.UdisksSource.start",
            "udevmon.sources.udisks.UdisksSource.snapshot",
            "udevmon.control.ControlServer.start", "udevmon.control.call",
            "udevmon.control.Connection.__init__",
            "udevmon.notify.send", "udevmon.session.update",
            "udevmon.service.install", "udevmon.completion.default_install_path",
        ]
        with tempfile.TemporaryDirectory() as temp, contextlib.ExitStack() as stack:
            out = Path(temp)
            for target in guarded:
                stack.enter_context(patch(target, _failing(resolve_name(target), forbidden)))
            stack.enter_context(patch("subprocess.Popen", side_effect=only_git))
            stack.enter_context(patch.object(socket.socket, "connect", side_effect=forbidden))
            # Sphinx's own napoleon uses an API Sphinx itself deprecates: not ours to fix.
            stack.enter_context(pywarnings.catch_warnings())
            pywarnings.filterwarnings("ignore", message="The mapping interface for autodoc")
            warnings = io.StringIO()
            app = Sphinx(srcdir=str(ROOT / "docs"), confdir=str(ROOT / "docs"),
                         outdir=str(out / "html"), doctreedir=str(out / "doctrees"),
                         buildername="html", status=io.StringIO(), warning=warnings,
                         freshenv=True, warningiserror=True, confoverrides={"nitpicky": True})
            app.build(force_all=True)
            self.assertEqual(app.statuscode, 0, warnings.getvalue())
            self.assertEqual(warnings.getvalue(), "")

            html = out / "html"
            cli = (html / "cli.html").read_text()
            for text in ("udevmon actions set", "--from-mountpoint", "udevmon service install",
                         "udevmon completion uninstall"):
                self.assertIn(text, cli)
            self.assertNotIn("_complete", cli)
            internals = (html / "internals.html").read_text()
            self.assertIn("Engine", internals)
            self.assertIn("_static/layout.css", internals)
            self.assertEqual((html / "_static" / "layout.css").read_bytes(),
                             (ROOT / "docs" / "_static" / "layout.css").read_bytes())
            modules = (html / "modules.html").read_text()
            self.assertIn("udevmon.runtime", modules)
            self.assertNotIn('id="module-udevmon.engine"', modules)     # it is in internals
            self.assertNotIn("udevmon.__main__", modules)


if __name__ == "__main__":
    unittest.main()
