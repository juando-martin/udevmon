Integration contracts
=====================

Other programs use udevmon in three ways: as the commands its actions run,
as a configuration they contribute to, or as a CLI they call. There is no
Python plugin interface; see :doc:`api` for the status of each contract.

What an action receives
-----------------------

An action's command is an argument list and is never passed to a shell.
Placeholders (``{mountpoint}``, ``{serial}``, ``{event}``, ``{device}``,
``{tag}``, ``{var_NAME}``) are each substituted as one whole argument. An
action whose placeholder has no value for the event is skipped with an
error, not run with an empty one. The same values are in the environment:

* ``UDEVMON_EVENT``: the event kind (``connected``, ``mounted``, ...).
* ``UDEVMON_DEVICE``: the device name, when the thing is named.
* ``UDEVMON_TAG``: the tag, for tag actions.
* ``UDEVMON_<PROPERTY>``: each property, for example
  ``UDEVMON_MOUNTPOINT`` or ``UDEVMON_SERIAL``.
* ``UDEVMON_VAR_<NAME>``: each device variable.
* ``UDEVMON_STATE_DIR``: a private folder shared by the actions of one plug
  or one mount, removed when that presence ends.

Commands run as the user, from the home directory, with no standard input,
and their output is logged line by line. A non-zero exit is a failure
(retries, ``notify_on_failure``); a timeout ends the whole process group.
The desktop session variables (``DISPLAY``, ``WAYLAND_DISPLAY``,
``XAUTHORITY``, ``DBUS_SESSION_BUS_ADDRESS``) are provided even under the
service. Under systemd, stopping the service stops everything the actions
started; use ``systemd-run --user --scope`` for something that must outlive
it. ``README.md`` ("How commands are run") and ``man 5 udevmon.toml`` have
the full rules.

Contributing configuration
--------------------------

A program that needs its own devices, tags or actions owns a drop-in file,
``~/.config/udevmon/config.d/NN-NAME.toml``, and replaces or deletes it
whole without touching the user's ``config.toml``. Drop-ins are read after
the main file, in file-name order, and have the same tables except
``[daemon]``. Their entries join the others: an action in one file can use
a device or tag of another. A name defined in two files is an error naming
both. Each file must be owned by the user and not writable by group or
others. The ``add`` commands write to a drop-in with ``--file NAME``;
``check`` lists the files it read. kp-sync's actions live in
``config.d/50-kp-sync.toml``.

Between programs, set the run order of one event's actions with
``order``, not with file names: file order only breaks ties.

Calling the CLI
---------------

* Pass arguments as separate argv elements. Exit status 0 means success.
  The man page lists the others for each command.
* ``udevmon present --json`` prints the things present now, with every
  property and the devices and tags that match them. It is the
  machine-readable form; the other output is for people.
* ``udevmon trigger DEVICE [EVENT] --wait`` asks the running daemon to run
  a device's actions now and returns their exit status. It needs the
  daemon's ``XDG_RUNTIME_DIR``.
* ``udevmon reload``, ``pause [--for DURATION]`` and ``resume`` act on the
  running daemon. The pause is a flag file, so it applies even to a daemon
  started later.
* ``udevmon check`` validates a configuration (``--config PATH`` for
  another file) without running anything.

The control socket (``$XDG_RUNTIME_DIR/udevmon.sock``) and its JSON lines
are internal: use the commands above, which own the protocol. Integration
tests must use a private daemon with its own configuration and runtime
folder (``DEVELOPMENT.md``, "Live testing"), never the user's service.

Known gaps
----------

The contracts above cover today's integrations (kp-sync, rclone-config).
Things a new integration might miss, none of them planned (planned work is
in ``SPECIFICATIONS.md`` §12):

* Machine-readable output from ``explain`` and ``check``; only ``present``
  has ``--json``.
* A documented, versioned control-socket protocol for clients other than
  the CLI, such as a GUI (``SPECIFICATIONS.md`` §11).
