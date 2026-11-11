Interfaces and stability
========================

udevmon is preliminary (0.x) and keeps no backwards compatibility yet:
formats and names change in place, without aliases or migration, and the
changelog records each change. A change to a provisional interface that can
break a caller is listed under **Changed (incompatible)**: pin a range such
as ``udevmon>=0.9,<0.10``, and read that section before raising it.

Provisional (meant for integration, may still change)
-----------------------------------------------------

* The command line and its exit statuses (:doc:`cli`, ``man 1 udevmon``).
* The configuration format and drop-in files (``man 5 udevmon.toml``).
* What an action receives: placeholders, ``UDEVMON_*`` environment
  variables and the state folder (:doc:`integration`).
* The JSON printed by ``--json`` (``present``, ``devices list|show|identify``,
  ``tags list|show``, ``actions list``, ``check``, ``explain``,
  ``trigger``, ``status``, ``version``), and the feature names in
  ``version --json``.
* The input of ``udevmon apply``: a JSON array of editing commands.
* The output of ``udevmon --version``: the version alone, on one line.

Internal (no promise at all)
----------------------------

* Every Python module of the ``udevmon`` package. There is no public Python
  API: nothing is meant to be imported by another program, and appearing in
  :doc:`internals` or :doc:`modules` does not make an object public.
* The control socket protocol (a draft, ``SPECIFICATIONS.md`` §8.9), the
  lock file, the pause flag file and the layout of ``$XDG_RUNTIME_DIR``.
* Log message wording, and the human-readable output of every command.

A program that needs udevmon's data model should call the ``--json``
commands, not import udevmon or parse its TOML itself.
