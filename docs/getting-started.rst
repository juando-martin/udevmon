Getting started
===============

udevmon needs Linux with udev and udisks2, Python 3.10 or later, and the
``pyudev``, ``PyGObject`` and ``tomlkit`` packages. It runs as you, usually
as a systemd user service. ``README.md`` has the installation steps; the
short path is::

   udevmon scan                  # plug things in, name them
   udevmon actions add --device backup-disk mounted -- \
       rsync -a ~/Documents/ "{mountpoint}/docs/"
   udevmon check                 # validate
   udevmon explain               # what would run, without plugging in
   udevmon service install --now

Where things are
----------------

* ``~/.config/udevmon/config.toml``: the configuration, plus drop-in files
  in ``config.d/*.toml`` (``man 5 udevmon.toml``).
* ``$XDG_RUNTIME_DIR``: the daemon's lock file, control socket, pause flag
  and per-instance state folders. All of it disappears at logout or reboot.
* ``man 1 udevmon``: every command, with exit statuses. :doc:`cli` is the
  same command set, generated from the argument parser.

Checking without side effects
-----------------------------

``check``, ``explain``, ``present``, ``devices list`` and the other ``list``
and ``show`` commands only read. ``run --dry-run`` watches real events but
logs the actions instead of running them. ``trigger`` runs a present
device's actions for real. The editing commands (``devices add``,
``actions set``, ...) rewrite the config and tell a running daemon to reload
unless given ``--no-reload``.
