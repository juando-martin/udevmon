# Working on udevmon

How the project is worked on: tests, the checks that keep the docs in step with
the code, live testing, and releasing. `SPECIFICATIONS.md` describes the design;
`README.md` is for users.

## Tests

```
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

Running from the source tree needs the dependencies installed (`pyudev`,
`PyGObject`, `tomlkit`). Run one module with `python3 -m unittest tests.test_x`,
not `python3 tests/test_x.py`: importing the `tests` package is what isolates
the suite (`tests/__init__.py`):

- every temporary file goes in one per-run folder, deleted at the end, so
  nothing is left in `/tmp`;
- the tests get their own `XDG_RUNTIME_DIR`, so they never reach a real daemon,
  its pause flag or its control socket;
- a fake `DISPLAY`, and no `DBUS_SESSION_BUS_ADDRESS`, so nothing reaches the
  real desktop or sends a notification.

A new test that needs a daemon, a session bus or the desktop must fake it the
same way; `tests/test_notify.py` and `tests/test_trigger.py` show how.

After writing a test for a rule, break the rule in the code on purpose and check
that the test fails (a quick mutation check). It catches a test that passes for
the wrong reason.

## Checks that keep things in step

These tests fail when a change is only half done. Fix them by finishing the
change, not by loosening the test.

| Test | Fails when |
|---|---|
| `test_completion` | a command, positional argument or value option of the CLI is missing from the completion tables in `completion.py` |
| `test_manpages` | a command, option, config key, event or property is not described in `man/`, the example config there is invalid, or `groff` warns |
| `test_documentation` | the HTML manual has a warning (broken reference, bad docstring markup), or building it reads the config, starts a source, opens a socket or runs a command other than `git`; skipped without Sphinx |
| `test_docs` | a code block in `README.md`, `SPECIFICATIONS.md`, `CHANGELOG.md` or this file is unbalanced, empty, or has a line wider than 76 characters (it must fit an 80-column `glow`) |
| `test_license` | a Python file lacks the two-line SPDX header (after any `#!` line), or `LICENSE` or the license metadata of udevmon or `contrib/rclone-config` is missing |

So a new option normally touches: the parser in `cli.py`, `completion.py`, both
man pages where relevant, README, SPECIFICATIONS, and the changelog.

## Docs

- `CHANGELOG.md`: add a line under **Unreleased** for any change a user would
  notice, in the section it belongs to (Added, Changed, Fixed, ...).
- Man pages: when a page's content changes, set the date in its `.TH` line to
  the day of the change (`man/udevmon.1`, `man/udevmon.toml.5`).
- Prose is plain and short; examples are real commands that work.
- Every Python file starts with the license header (after a `#!` line):

  ```
  # SPDX-License-Identifier: GPL-3.0-or-later
  # Copyright (C) 2026 Juan Domingo Martín
  ```
- No personal data in tracked files: examples use neutral drive labels
  (`KEYDRIVE`), serials (`1111222233334444`), UUIDs (`AB12-CD34`) and host
  names (`alpha`, `beta`).
- No backwards compatibility while udevmon is preliminary: a format change is
  made in place, and existing configs are fixed by hand, not by migration code.

### The HTML manual

`docs/` is a Sphinx manual for integrators and maintainers: integration
contracts, what is provisional and what internal, the command reference
(generated from the parser) and the module reference (from docstrings).
Sphinx is optional (the `docs` extra); install it in a private virtual
environment, never in `~/.venvs/tools`:

```
python3 -m venv --system-site-packages /path/to/venv
/path/to/venv/bin/python -m pip install -e '.[docs]'
/path/to/venv/bin/python tools/docs.py build
/path/to/venv/bin/python tools/docs.py serve --port 8000
```

The build is fresh, nitpicky and fails on any warning; the output, in
`docs/_build/html` (ignored by git) or `--output PATH`, is never committed.
The preview listens on 127.0.0.1 only, does not rebuild after edits, and
stops with Ctrl-C. Run the test suite with that venv's Python too, so that
`test_documentation` builds the manual instead of being skipped.

Docstrings are PEP 257 and reStructuredText (double backquotes for code);
Napoleon reads Google-style `Args:`, `Returns:` and `Raises:` sections. Give
the contract a caller needs (blocking work, files written, subprocesses,
failure), not boilerplate for trivial helpers. A new module shows up in the
manual by itself, under "Other modules"; list a core one in
`docs/internals.rst`. `docs/contributing.rst` has the details.

## Live testing

The unit tests use synthetic events. For the real thing:

- `tools/usb-test.py` walks through plugging and pulling a USB stick (see
  README, "Real-hardware test").
- A private daemon, so the real service is not disturbed: give it its own
  config and runtime folder, and stop it at the end.

  ```
  rt=$(mktemp -d /tmp/udm-XXXX)    # short: a socket path is limited
  trap 'rm -rf "$rt"' EXIT
  XDG_RUNTIME_DIR=$rt udevmon --config test.toml run --dry-run
  ```

  Commands that talk to it (`trigger`, `pause`, `reload`) need the same
  `XDG_RUNTIME_DIR`.

While testing: leave nothing behind in `/tmp` (a `trap` as above), keep the
desktop from opening a file-manager window for each mounted stick
(`usb-test.py` turns that setting off and back on), and avoid anything that asks
for a password, such as `udisksctl` without `--no-user-interaction`.

## Branches

Work goes on a branch off `main` (`feature/...`, `fix/...`, `docs/...`), one
commit per step that was tested. It is merged into `main` as one clean commit,
squashed if the branch has a long trial-and-error history.

## Releasing

The version comes from git tags (`src/udevmon/_scm.py`, SPECIFICATIONS §13.1);
nothing in the source holds it.

1. Choose the number. Before 1.0: a new feature or a change in behaviour bumps
   the minor version (0.7.0 to 0.8.0); fixes only bump the patch (0.4.0 to
   0.4.1).
2. In `CHANGELOG.md`, add `## [X.Y.Z] - YYYY-MM-DD` under `## [Unreleased]`,
   which stays, empty, above it.
3. Check the man page dates (see Docs) and run the tests.
4. Commit as `Release X.Y.Z`, then tag: `git tag -a vX.Y.Z -m "udevmon X.Y.Z"`.
5. Push `main` and the tag. On GitHub the tag starts the release workflow
   (`.github/workflows/release.yml`): it checks the tag against the version
   from git, builds, and publishes to PyPI. Never re-upload a version.
6. Reinstall where it is used, and restart the service if the daemon changed.

## Installing a development build

udevmon is installed in a virtual environment made with
`include-system-site-packages = true`, so it uses the system's PyGObject (`gi`),
`pyudev` and `tomlkit`, with the commands linked into `~/.local/bin`. With `uv`:

```
uv pip install --python ~/.venvs/tools/bin/python --no-deps .
udevmon completion install
```

`--no-deps` is needed because `uv` does not count the system packages as
installed and would fetch and build PyGObject from PyPI. The man
pages are installed into the environment's `share/man`; `man` finds them only
if they are linked from `~/.local/share/man/man1` and `man5`. The links
survive reinstalls.
