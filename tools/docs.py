#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Build the reference HTML, or build it and preview it on the loopback interface.

Run it with the interpreter that has the ``docs`` extra (Sphinx) installed.
"""

import argparse
import subprocess
import sys
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(output: Path) -> None:
    """Build fresh HTML into ``output``; any warning is an error."""
    subprocess.run([sys.executable, "-m", "sphinx", "-E", "-a", "-n", "-W", "--keep-going",
                    "-b", "html", str(ROOT / "docs"), str(output.resolve())],
                   cwd=ROOT, check=True)


def main(argv=None) -> int:
    """Build the HTML and, for ``serve``, serve it on 127.0.0.1 until Ctrl-C."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("build", "serve"))
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "_build" / "html")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    try:
        build(args.output)
        if args.command == "serve":
            handler = partial(SimpleHTTPRequestHandler, directory=str(args.output.resolve()))
            with ThreadingHTTPServer(("127.0.0.1", args.port), handler) as server:
                print(f"Documentation: http://127.0.0.1:{server.server_port}", flush=True)
                try:
                    server.serve_forever()
                except KeyboardInterrupt:
                    pass
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: documentation {args.command} failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
