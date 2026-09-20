"""Tests never touch a real udevmon daemon: they get their own runtime directory."""

import atexit
import os
import tempfile

_runtime = tempfile.TemporaryDirectory(prefix="udevmon-tests-")
os.environ["XDG_RUNTIME_DIR"] = _runtime.name
atexit.register(_runtime.cleanup)
