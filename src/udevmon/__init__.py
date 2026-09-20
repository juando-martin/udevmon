"""udevmon: user-level device and mount event automation."""

from pathlib import Path


def __getattr__(name):
    """``__version__``: written at build time, else asked from git (a source checkout)."""
    if name == "__version__":
        try:
            from ._version import version
        except ImportError:
            from ._scm import get_version
            version = get_version(Path(__file__).resolve().parents[2])
        globals()["__version__"] = version
        return version
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
