# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""``udevmon edit``: change a configuration file in an editor, checked before it is saved.

Like ``visudo``: the editor gets a copy, the whole configuration (every file, with the
edited text in place) is checked on a second copy, and the real file is replaced only
when that check passes and nobody changed the file meanwhile (spec 8.8).
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Callable

from . import config as cfgmod
from . import configedit


def editor_command(env=None) -> list[str]:
    """The editor to run, as an argument list: ``$VISUAL``, else ``$EDITOR`` (both may
    hold options, ``code --wait``), else ``editor`` when it exists (Debian's choice),
    else ``vi``."""
    env = os.environ if env is None else env
    for name in ("VISUAL", "EDITOR"):
        value = (env.get(name) or "").strip()
        if value:
            return shlex.split(value)
    return ["editor"] if shutil.which("editor") else ["vi"]


def run_editor(path: Path) -> int:
    """Open ``path`` in :func:`editor_command`, wait for it, and return its exit status
    (127 when it cannot be started)."""
    argv = [*editor_command(), str(path)]
    try:
        return subprocess.call(argv)
    except OSError as e:
        print(f"error: cannot start the editor {argv[0]!r}: {e}", file=sys.stderr)
        return 127


def _text(path: Path) -> str | None:
    """The file's text exactly as stored, or None when it does not exist."""
    return configedit._read(path) if path.exists() else None


def check_text(main: Path, target: Path, text: str) -> tuple[list[str], list[str]]:
    """The errors and warnings of the configuration of ``main`` if ``target`` held
    ``text``, as ``check`` gives them. Works on a copy; the messages name the real
    files."""
    with tempfile.TemporaryDirectory(prefix="udevmon-edit-check-") as tmp:
        copy = Path(tmp) / main.name
        if main.exists():
            shutil.copy2(main, copy)
        for f in cfgmod.dropin_files(main):
            cfgmod.dropin_dir(copy).mkdir(mode=0o700, exist_ok=True)
            shutil.copy2(f, cfgmod.dropin_dir(copy) / f.name)
        if target == main:
            edited = copy
        else:
            cfgmod.dropin_dir(copy).mkdir(mode=0o700, exist_ok=True)
            edited = cfgmod.dropin_dir(copy) / target.name
        configedit._write(edited, text)

        def shown(message: str) -> str:
            return message.replace(str(Path(tmp)), str(main.parent))
        try:
            cfg = cfgmod.load(copy)
        except cfgmod.ConfigError as e:
            return [shown(p) for p in e.problems], []
        return [], [shown(w) for w in (*cfg.warnings, *cfgmod.command_problems(cfg))]


def edit_file(main: Path, target: Path, *, editor: Callable[[Path], int] = run_editor,
              ask: Callable[[str], str] | None = None) -> tuple[int, bool]:
    """Edit ``target`` (``main`` or one of its drop-ins, which may not exist yet).

    ``editor`` opens a file and returns its exit status; ``ask`` asks a question and
    returns the answer (None: never ask, so a file with errors is discarded).

    Returns:
        ``(exit status, saved)``. 0 when saved or unchanged; 1 when discarded, the
        editor failed, or ``target`` changed on disk during the edit (the edited text
        is then kept beside it as ``NAME.edit``, which is never read).
    """
    original = _text(target)
    with tempfile.TemporaryDirectory(prefix="udevmon-edit-") as tmp:
        work = Path(tmp) / target.name                 # same name: the editor sees .toml
        configedit._write(work, original or "")
        while True:
            status = editor(work)
            if status != 0:
                print(f"The editor exited with status {status}; nothing was saved.")
                return 1, False
            text = configedit._read(work)
            if text == (original or ""):
                print("No changes.")
                return 0, False
            errors, warnings = check_text(main, target, text)
            if not errors:
                break
            for problem in errors:
                print(f"error: {problem}", file=sys.stderr)
            answer = ""
            if ask is not None:
                try:
                    answer = ask("Edit again (e) or discard the changes (d)? [e] ")
                except (EOFError, KeyboardInterrupt):
                    print()
                    answer = "d"
                answer = answer.strip().lower() or "e"
            if not answer.startswith("e"):
                print("Discarded: nothing was saved.")
                return 1, False
        if _text(target) != original:
            kept = target.with_name(target.name + ".edit")
            configedit._write(kept, text)
            print(f"error: {target} changed while you were editing it (another program?); "
                  f"nothing was saved. Your version is in {kept}.", file=sys.stderr)
            return 1, False
    try:
        configedit.replace_file(main, target, text)
    except cfgmod.ConfigError as e:                     # changed between check and save
        for problem in e.problems:
            print(f"error: {problem}", file=sys.stderr)
        print("Nothing was saved.", file=sys.stderr)
        return 1, False
    for w in warnings:
        print(f"warning: {w}", file=sys.stderr)
    backup = f" (the old version is in {target.name}.bak)" if original is not None else ""
    print(f"Saved {target}{backup}.")
    return 0, True
