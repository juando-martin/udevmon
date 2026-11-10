Interfaces and stability
========================

udevmon is preliminary (0.x) and keeps no backwards compatibility yet:
formats and names change in place, without aliases or migration, and the
changelog records each change. Pin the version an integration was tested
with.

Provisional (meant for integration, may still change)
-----------------------------------------------------

* The command line and its exit statuses (:doc:`cli`, ``man 1 udevmon``).
* The configuration format and drop-in files (``man 5 udevmon.toml``).
* What an action receives: placeholders, ``UDEVMON_*`` environment
  variables and the state folder (:doc:`integration`).
* The JSON printed by ``udevmon present --json``.

Internal (no promise at all)
----------------------------

* Every Python module of the ``udevmon`` package. There is no public Python
  API: nothing is meant to be imported by another program, and appearing in
  :doc:`internals` or :doc:`modules` does not make an object public.
* The control socket protocol, the lock file, the pause flag file and the
  layout of ``$XDG_RUNTIME_DIR``.
* Log message wording, and the human-readable output of every command.

A program that needs udevmon's data model should call ``present --json``
or read the configuration with its own TOML parser, not import udevmon.
