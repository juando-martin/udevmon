Documentation and development
=============================

``DEVELOPMENT.md`` is the guide to working on udevmon: tests, the checks
that keep CLI, completion, man pages and docs in step, live testing and
releasing. This page covers the HTML manual.

Building
--------

Sphinx is optional, in the ``docs`` extra. Install it into a private
virtual environment, not the one udevmon is installed in for daily use::

   python3 -m venv --system-site-packages /path/to/private-venv
   /path/to/private-venv/bin/python -m pip install -e '.[docs]'

(``--system-site-packages`` lets it use the system's PyGObject and pyudev,
which the tests need; the docs build itself does not import them.)

Build the HTML, or build it and preview it::

   /path/to/private-venv/bin/python tools/docs.py build
   /path/to/private-venv/bin/python tools/docs.py serve --port 8000

The build is fresh (``-E -a``), nitpicky (``-n``), and fails on any warning
(``-W``). The output goes to ``docs/_build/html``, which git ignores;
``--output PATH`` writes it elsewhere. The preview listens only on
``127.0.0.1``; stop it with Ctrl-C. It does not rebuild after edits: run
it again. Once Sphinx is installed the build needs no network.

The build must stay free of side effects: importing the modules and
building the parser must not read the user's configuration, start a
source, open the control socket or run a command (other than ``git``, for
the version). ``tests/test_documentation.py`` builds the manual with those
entry points patched to fail.

Writing docstrings
------------------

Docstrings are PEP 257, rendered by autodoc. Napoleon reads Google-style
``Args:``, ``Returns:`` and ``Raises:`` sections; use them where a
function's contract is not obvious from one sentence. Say what matters to a
caller: blocking work, files written, subprocesses, locks, what happens on
failure. A docstring is reStructuredText: double backquotes for code, and
a blank line before a list.

Autodoc leaves out an undocumented member without a warning, so
``test_documentation`` requires a docstring on every public function, class
and method, and a ``#:`` comment on every public module constant and class
field (on the line above, or after the value). Private helpers (``_name``)
and the tests need none. Keep ``: `` out of a ``#:`` comment: Napoleon reads
``words: more words`` as a type and a description.

:doc:`cli` comes from ``cli.build_parser``. A new command or option needs
no change here, but it does need the other places ``DEVELOPMENT.md``
lists. A new module appears in :doc:`modules` by itself. Move it to
:doc:`internals` if it is a core part.
