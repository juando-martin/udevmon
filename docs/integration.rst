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
* ``UDEVMON_VAR_<NAME>``: each variable, the device's own over its tags'
  defaults.
* ``UDEVMON_STATE_DIR``: a private folder shared by the actions of one plug
  or one mount, removed when that presence ends.
* ``UDEVMON_STATE_ROOT``: the folder that holds every state folder, for
  notes shared by all instances; it lasts as long as the daemon.
* ``UDEVMON_SCRIPT_DIR``: the folder of the program run, when ``run[0]`` is
  a path or was found in ``script_dir`` (not for a bare name left to
  ``PATH``), so a script finds the files next to it.
* The action's own ``env`` entries, set last (``UDEVMON_*`` names cannot
  be among them).

Commands run as the user, from the action's ``cwd`` or else the home
directory, with no standard input, and their output is logged line by line
(the last lines are kept for ``trigger --wait`` and ``status``). A non-zero exit is a failure
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
the main file, in file-name order, and have the same tables; their
``[daemon]`` may only have ``script_dir``, which applies to that drop-in's
own actions alone (searched first), so a program can name its executables
by bare name without editing the user's file. Their entries join the others: an action in one file can use
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
* ``--json`` is the machine-readable form; the other output is for people.
  ``present --json`` prints the things present now; ``devices list``,
  ``tags list`` and ``actions list --json`` the configuration, each entry
  with the ``file`` that defines it and its ``dropin`` name (what
  ``--file`` takes), so a program finds its own entries without reading
  the TOML; ``check --json`` the problems; ``explain --json`` the
  expanded commands. ``actions list --json`` also gives each action's
  ``program``, the file the daemon will start (null when only the daemon's
  ``PATH`` decides, as ``program_lookup`` says), so a program can check
  that the daemon runs the install it expects. ``man 1 udevmon`` lists the
  fields.
* ``udevmon devices identify MOUNTPOINT --json`` gives the ``fs_uuid`` and
  ``drive_serial`` of the drive mounted there, exactly as its actions get
  them, and the devices that match it. Take a drive's identity from here
  rather than working it out with ``lsblk`` or sysfs.
* ``udevmon version --json`` lists feature names; test for one (for
  example ``identify``) instead of comparing versions. ``udevmon
  --version`` prints the version alone on one line.
* Every editing command takes ``--dry-run``: it prints the change as a
  unified diff and writes nothing. ``udevmon apply`` takes a JSON list of
  editing commands and makes them one change: all or nothing, with one
  diff (``--dry-run``) and one reload.
* ``udevmon trigger DEVICE [EVENT] --wait`` asks the running daemon to run
  a device's actions now and returns their exit status. Its other exit
  statuses tell the failures apart (3 not running, 4 unknown device, 5
  another configuration, 6 refused, 7 no usable answer); ``--json`` adds
  an ``error`` code and each action's status, ``failure`` and last
  ``output`` lines, so a program can say why an action failed. It needs
  the daemon's ``XDG_RUNTIME_DIR``.
* ``udevmon status --json`` says whether the daemon runs, with which
  configuration, and each action's last result since it started (exit 3
  when none runs).
* ``udevmon reload``, ``pause [--for DURATION]`` and ``resume`` act on the
  running daemon. ``reload`` exits 1 when the new configuration has errors
  (printed; the daemon keeps the previous one) or no daemon runs. The pause is
  a flag file, so it applies even to a daemon started later.
* ``udevmon check`` validates a configuration (``--config PATH`` for
  another file) without running anything.

The control socket (``$XDG_RUNTIME_DIR/udevmon.sock``) speaks JSON-RPC 2.0;
its protocol (``SPECIFICATIONS.md`` §8.9) is a draft and still internal: use
the commands above, which own it. Integration
tests must use a private daemon with its own configuration and runtime
folder (``DEVELOPMENT.md``, "Live testing"), never the user's service: set
``XDG_CONFIG_HOME`` and ``XDG_RUNTIME_DIR`` to private folders for both the
daemon and the commands that talk to it, and nothing reaches the user's.

Known gaps
----------

The contracts above cover today's integrations (kp-sync, rclone-config).
Things a new integration might miss, none of them planned (planned work is
in ``SPECIFICATIONS.md`` §12):

* A public control-socket protocol for clients other than the CLI, such as
  a GUI: ``SPECIFICATIONS.md`` §8.9 describes a versioned one (``hello``,
  ``status``, ``present``, ``subscribe`` ...), but it is a draft until a real
  client has used it.
