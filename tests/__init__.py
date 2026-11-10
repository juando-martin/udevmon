# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Test isolation.

* Every temporary file or folder the tests create lives in one per-run folder that is
  deleted when the run ends, so the suite never leaves anything behind in /tmp.
* Tests never touch a real udevmon daemon: they get their own runtime directory.
* Tests never touch the real graphical session: they get a fake DISPLAY.

(Both take effect when the `tests` package is imported, which `python -m unittest
discover -s tests -t .` and `python -m unittest tests.test_x` do; running a test file
directly bypasses it.)
"""

import atexit
import os
import tempfile

_scratch = tempfile.TemporaryDirectory(prefix="udevmon-tests-tmp-")
tempfile.tempdir = _scratch.name          # mkdtemp(), TemporaryDirectory() ... in this process
os.environ["TMPDIR"] = _scratch.name      # ... and in any subprocess the tests start
atexit.register(_scratch.cleanup)

_runtime = tempfile.TemporaryDirectory(prefix="udevmon-tests-")     # inside the folder above
os.environ["XDG_RUNTIME_DIR"] = _runtime.name
atexit.register(_runtime.cleanup)

# Actions look up the graphical session (udevmon.session) unless they already have a
# display and are not under systemd. A fake one keeps that lookup out of the suite (and
# anything graphical a test might start off the real screen); tests/test_session.py
# exercises the lookup itself against a fake /proc and manager.
os.environ.pop("INVOCATION_ID", None)
os.environ["DISPLAY"] = ":udevmon-tests"
os.environ.pop("WAYLAND_DISPLAY", None)

# No test may show a real desktop notification (notify_on_failure): no session bus. The
# runtime folder above has no `bus` socket either; tests/test_notify.py fakes the sending.
os.environ.pop("DBUS_SESSION_BUS_ADDRESS", None)
