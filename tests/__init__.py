"""Test isolation.

* Every temporary file or folder the tests create lives in one per-run folder that is
  deleted when the run ends, so the suite never leaves anything behind in /tmp.
* Tests never touch a real udevmon daemon: they get their own runtime directory.

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
