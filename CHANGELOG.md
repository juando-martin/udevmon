# Changelog

All notable changes to udevmon are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the version numbers follow
[Semantic Versioning](https://semver.org/) (while below 1.0, a new minor
version may change behaviour).

How to keep it: add a line under **Unreleased** in the same commit as any
change a user would notice (new or changed commands, options, config keys,
dependencies, behaviour, fixes). When a release is tagged, rename that heading
to the version and date, and start a new empty **Unreleased** above it.

A change to the interface other programs use (the CLI with its exit statuses
and `--json` output, the configuration format, what actions receive; see
README, "Using udevmon from other programs") that can break them goes under
**Changed (incompatible)**, so callers can read that section before raising
their pinned version.

## [Unreleased]

## [0.10.0] - 2026-11-12

### Added

- **`check --json` says where each finding comes from**: a `findings` list
  beside the unchanged `errors`, `warnings` and `notes`, one object per
  finding with `level`, `message` and `subjects`, the entries it concerns
  (`device`, `tag`, `action`, `daemon` or `file`, with `name`, `file` and
  `dropin`). A finding about two entries names both; a load error that
  knows only its file names the file. Feature name `check-json-findings`.

## [0.9.0] - 2026-11-11

### Added

- **`--json` for scripts** on `devices list`, `devices show`, `tags list`,
  `tags show`, `actions list`, `check` and `explain`. Every entry says which
  `file` defines it and, for a drop-in, its `dropin` name (what `--file`
  takes), so a program can find its own entries without reading the TOML.
  `actions list --json` gives every field with defaults filled in; `check
  --json` gives the errors, warnings, notes and files; `explain --json` the
  expanded command of each action.
- **`udevmon version [--json]`**: the version and a list of feature names
  (`identify`, `edit-dry-run`, ...) that scripts can test for instead of
  comparing versions. `udevmon --version` still prints the version alone on
  one line, now stated as part of the interface.
- **`udevmon devices identify MOUNTPOINT [--json]`**: the `fs_uuid` and
  `drive_serial` of the drive mounted there, exactly as actions get them,
  the match `--from-mountpoint` would write, and the devices and tags it has.
- **`--dry-run` on every editing command** (`devices`, `tags`, `actions`):
  makes the change on a copy, prints it as a unified diff of each file, and
  writes nothing.
- **`trigger --json`**: the result as one object, failures included, with an
  error code and, with `--wait`, each action's exit status.
- **`$UDEVMON_SCRIPT_DIR` and `$UDEVMON_STATE_ROOT`** for actions: the
  folder of the program run (when `run[0]` is a path or was found in
  `script_dir`), and the folder that holds every state folder. Feature
  names `env-script-dir` and `env-state-root`.
- **New `check` notes**: two devices one of whose `match` contains all of
  the other's, so a thing always matches both and a tag action's `{device}`
  is the one read first (across files, the drop-in file names decide it);
  a `lock` name only one action uses, with the closest other name in case
  of a typo; an `exclude_tags` entry whose rule can never match the device.
- **TAB completion for `pause --for`** offers `15m 30m 1h 2h 1d`.
- **`udevmon edit [FILE]`**, like `visudo`: edit the main file or a drop-in
  in `$VISUAL`/`$EDITOR` on a copy; the whole configuration is checked when
  the editor exits, with the choice to edit again or discard on errors;
  then the file is replaced (keeping `.bak`) and the daemon reloads. A file
  changed by another program meanwhile is not overwritten (the edit is kept
  as `NAME.edit`). Feature name `edit`.
- **`scan` through the running daemon**: when a daemon with the same
  configuration runs, `scan` shows the events it receives instead of
  watching by itself, so only one process watches.
- **`udevmon pause` and `resume` tell the running daemon**, which logs the
  change at once (the pause is still the flag file, and works with no
  daemon).
- **Why an action failed, in `trigger --wait`**: each action's result has
  `failure` (timed out, could not start, ...) and `output`, the last 10
  lines of what it printed (each cut to 300 characters); the text output
  shows them under a failed action. Feature name `trigger-output`.
- **`disconnected` says whether the pull was a surprise**: `surprise` is
  `1` when a filesystem on that USB device was still mounted (or vanished
  while mounted within 5 s before), else `0`. udevmon links each mounted
  filesystem to its USB device through sysfs, so a device without a serial
  number works too. Feature name `disconnect-surprise`.
- **An action's own `env` and `cwd`**: environment variables for the
  command and the folder it starts in (default: home), both with
  placeholders; `actions add|set --env NAME=VALUE --cwd DIR`, and on `set`
  `--unset-env NAME` and `--no-cwd`. Feature name `action-env-cwd`.
- **Default variables on a tag**: `[tags.NAME] vars = {...}` (and `tags set
  NAME --var N=V --unset-var N`), which a device's own override. A tag
  action gets the defaults of its tag, a device action those of the
  device's tags in the order listed; `check` notes two tags that disagree.
- **zsh and fish completion**: `udevmon completion zsh|fish` prints the
  script, and `completion install|uninstall --shell zsh|fish` puts it where
  the shell loads it (zsh needs that folder in `fpath`; the command says so).
- **A `check` note for devices that match on different keys** but could
  select one thing (one on `fs_uuid`, another on `drive_serial`). Keys of a
  USB device and of a filesystem never meet, so those pairs stay quiet.
- **`udevmon status [--json]`**: whether the daemon is running, its version,
  configuration and pause, and each action's last result since it started
  (time, event, device, exit status or why there is none, the last output
  lines of a failure). Exit 3 when no daemon runs. Feature name `status`.
- **`udevmon apply [FILE] [--dry-run]`**: several editing commands as one
  change. The input is a JSON array of commands (each an array of words);
  they run in order on a copy, and only when all succeed are the files
  written, with one reload. `--dry-run` prints the whole change as one diff.
  Feature name `apply`.
- **A drop-in's own `script_dir`**: a drop-in's `[daemon]` may now have
  `script_dir` (and nothing else). Its folders are searched first, for that
  drop-in's actions only, so a tool's drop-in can name its programs by bare
  name without editing `config.toml`, and cannot change what a bare name
  means elsewhere. Feature name `dropin-script-dir`.
- **`actions list --json` says which program runs**: `program`, the file
  the daemon will start (or null), with `program_lookup` (`path`,
  `script_dir`, `PATH`, `placeholder`) and the action's own `script_dir`.
  Feature name `action-program`.
- **`retry_backoff`** for actions (and `--retry-backoff` on `actions add` and
  `actions set`): each wait between attempts is that many times the one
  before (`retry_delay = 2`, `retry_backoff = 2`: 2, 4, 8 ... seconds).
  Feature name `retry-backoff`.

### Changed (incompatible)

- **`udevmon trigger` exit statuses tell the failures apart**: 3 the daemon
  is not running, 4 the device is not in its configuration, 5 `--config`
  names another file, 6 another refusal (not present, ignored, the event
  does not fit, paused), 7 an unreadable answer. 1 now means only that an
  action failed (with `--wait`). Before, all of these gave 1.
- **`max_workers` counts commands, not events**: every event's actions run in
  a thread of their own, and `max_workers` is how many commands may run at
  once. An action waiting for its `lock`, or between two attempts, no longer
  holds up other events. Feature name `max-workers-commands`.
- **Retries of `shutdown` actions go on while the daemon stops** (within the
  service's 120 seconds); before, stopping ended them like any other.
  Feature name `shutdown-retries`.
- **`udevmon reload` reports the result and fails on a bad configuration**:
  it prints what the daemon loaded and its warnings; when the configuration
  has errors it prints them and exits 1 (so does `systemctl --user reload
  udevmon`), the daemon keeping the previous one. The editing commands, too,
  now return only once the daemon has reloaded, so a `udevmon trigger` right
  after an edit finds the new device. Before, `reload` sent `SIGHUP` and
  always exited 0 with a daemon running. Feature name `reload-result`.
- **The control socket speaks JSON-RPC 2.0** (one object per line,
  persistent connections, methods `hello`, `status`, `present`, `results`,
  `trigger`, `pause`, `resume`, `reload`, `subscribe`; SPECIFICATIONS §8.9,
  a draft). Params are strict (an unknown or mistyped one is refused);
  replies may be up to 64 MiB; a request that times out says whether it ran
  (`not-run`) or may still take effect (`still-running`); events are
  numbered, and `subscribe` returns what is present at that moment and
  takes filters (kinds, devices, tags) and what to be told: the raw
  `event`, what the engine did with it (`handled`) and each action's
  `result`. The versioning rule is written down.
  After upgrading, restart the daemon: against one still running the old
  version, `trigger` exits 7 and says so, while `reload`, `pause` and
  `resume` fall back to `SIGHUP` and the flag file.

### Documentation

- The interface for other programs is described in full (README, "Using
  udevmon from other programs"; SPECIFICATIONS §8.7, §11): the compatibility
  policy above, the `--json` fields, feature names, and how a program's tests
  run a private udevmon with `XDG_CONFIG_HOME` and `XDG_RUNTIME_DIR`. The
  control socket protocol is described in §8.9, as a draft.
- The module reference in the HTML manual is complete: the command
  handlers, module constants (event kinds, `trigger` exit statuses,
  `FEATURES`, ...) and the fields of the data classes are documented, and
  `test_documentation` fails when a public object has no docstring or
  `#:` comment.

## [0.8.1] - 2026-11-11

The first public release, on GitHub and PyPI. udevmon behaves as in 0.8.0:
this release adds the license, the packaging and the HTML manual.

### Added

- **An HTML reference manual** (Sphinx, optional `docs` extra):
  `python tools/docs.py build` or `serve`. It has the integration contracts,
  which interfaces are provisional and which internal, the command reference
  generated from the parser, and the module reference from the docstrings.
  The core modules gained docstrings for it. The sdist now includes the docs,
  man pages, tests and tools.
- **Licensed under GPL-3.0-or-later**: `LICENSE`, an SPDX header in every
  Python file (a test checks them), and the license in the package metadata.
  The same for `contrib/rclone-config`.
- **Ready for PyPI, GitHub and Read the Docs**: project metadata (readme,
  authors, URLs, classifiers), install instructions with `pipx`, CI and a
  release workflow that publishes a `vX.Y.Z` tag to PyPI, and a Read the
  Docs configuration. The README says which interfaces other programs can
  use (the CLI, the config format, what actions receive, `present --json`)
  and that the Python modules are internal.

### Fixed

- An editable install (`pip install -e .`) failed while writing
  `_version.py`; it now skips that file and asks git, like a source checkout.
- `tests/test_manpages.py` failed to import on Python 3.10 (`tomllib`).

### Changed

- The examples and tests use neutral drive labels, serials and host names.

## [0.8.0] - 2026-11-10

### Changed

- **`scan` now confirms before saving, and lets you go back.** Before anything is
  written it shows what would be saved (name, match or rule with its strength, and
  tags for a device) and asks you to confirm; `r`, `m` and `t` redo the name, the
  match/rule or the tags without losing the rest, and `c` (also `n`, `q`) cancels,
  leaving the thing in the list to pick again. A weak or medium match now needs an
  explicit `y` at that screen instead of a separate "Save anyway?" prompt. A name
  already in use is reported there, so you can rename and try again instead of
  losing what you typed.
- **`scan` can now declare a category instead of a device.** For each thing you
  pick to name, it first asks whether to register it as a **device** (one
  particular, trusted thing) or a **category** (a declared tag with a rule,
  matching every thing like it); a thing with no serial number, such as a YubiKey,
  defaults to category, since no device match can single out one such unit anyway.
  A category has no tags of its own and no strength gate, and the "add an action"
  hint printed afterwards includes `--allow-unnamed`, since a category is unnamed
  by default until it also matches a device you named. `iN` (ignore) is unchanged:
  it is device-only and skips the kind choice.

## [0.7.0] - 2026-11-05

### Added

- **Drop-in files: `config.d/*.toml`.** Read after `config.toml`, in file-name
  order, with the same tables except `[daemon]`. Their devices, tags and
  actions join the main file's, so a program can install its own in a file it
  owns and replaces or deletes whole. With the same `order` (below), actions
  run in the order the files are read, main file first. A name
  defined in two files is an error naming both. `check` lists the files read;
  `list`, `show` and `explain` mark entries from a drop-in. The `add` commands
  take `--file NAME` to write to `config.d/NAME.toml`; the other editing
  commands change an entry where it is defined, and a rename fixes references
  in every file. With only `config.toml` nothing changes.
- **`udevmon trigger DEVICE [EVENT]`** asks the running daemon to run a present
  device's actions now, as for a real `startup` (default), `shutdown`,
  `connected`, `mounted` or `unmounted`: for a drive named while plugged in, or
  to test an action without unplugging. It follows pause, `disabled` and
  `ignored`, and is logged like a real event. `--wait` waits for the actions
  and shows their exit status. The daemon now listens on
  `$XDG_RUNTIME_DIR/udevmon.sock` for this (only you can connect).
- **`udevmon devices add NAME --from-mountpoint PATH`** names the plugged-in
  drive mounted at `PATH` without typing its identity: the match is its
  filesystem's `fs_uuid` and its drive's `drive_serial`. It refuses (and writes
  nothing) when nothing is mounted there, the drive has no usable serial number,
  or a device already matches that filesystem.
- **`notify_on_failure = true` on an action** (`actions add|set
  --notify-on-failure`): a desktop notification when it exits non-zero, times
  out or cannot start, after its last attempt with `retries`. Sent on the
  desktop's D-Bus; without a session bus nothing is shown. Off by default.
- **`order` on an action** (a whole number, default 50): the actions of one
  event run by `order`, lower first, then in config order. This nests one
  tool's actions around another's across files, which one file order cannot
  (credentials up first on plugging, down last on unplugging).
  `actions add|set --order N` sets it; `actions list` shows the actions in the
  order they run, and `explain` shows a non-default order. Without `order`
  nothing changes.
- **`udevmon present --json`**: what is plugged in, for scripts, with every
  property, the devices and tags each thing is, and the suggested match.

### Changed

- **A missing optional `script_dir` folder is a note, not a warning.** When
  another folder of the list exists, one that does not (an example's
  `~/.local/udev/scripts` on a machine without it) is a note in `udevmon check`
  and an ordinary line in the log at start and reload. A path that exists but is
  not a folder, or a list where no folder exists, is still a warning.
- **`contrib/rclone-config`: the example uses `order`** (10 for `up`, 90 for
  `down`) and goes in `config.d/rclone-config.toml`, so it wraps the actions
  that use the credentials whatever file they are in.

## [0.6.0] - 2026-11-03

### Added

- **`contrib/rclone-config`: rclone credentials carried on a drive.** A
  separate helper package (not part of udevmon) whose `rclone-config`
  command udevmon runs as an action: while a drive tagged `rclone` is present, its
  `config/rclone/rclone.conf` is published to
  `$XDG_RUNTIME_DIR/rclone/rclone.conf` for any rclone user; it is switched to
  another tagged drive's config, or deleted, as soon as that drive is gone or
  udevmon stops. See its README for installing and the two actions.

### Fixed

- **Graphical programs started by actions work under the systemd service.**
  The service got its environment once, when it started; if that was before the
  desktop gave systemd its `DISPLAY` (or on a desktop that never does), every
  action ran without a display. Now the session's `DISPLAY`, `WAYLAND_DISPLAY`,
  `XAUTHORITY`, `DBUS_SESSION_BUS_ADDRESS` and `XDG_*` desktop variables are
  looked up for each action: from systemd's environment for your user as it is
  now, or else from one of your own running programs that has a display
  (preferring those of your desktop login over, say, an `ssh -X` shell). At
  the very beginning of a login, before the desktop has a display, an action
  waits for it, up to 30 s. The log says which display is used, and warns when
  there is none. Run from a terminal with a display, nothing changes.
- **Debounce no longer drops events of a different thing, or a real second
  event.** It was keyed on the device name, so two things matching one device
  (two YubiKeys without a serial in the match) within a second counted as one:
  at startup only one got its `startup` action. And a `mounted`, `unmounted`,
  `mounted` within a second lost the second `mounted`. Now a repeat is the same
  kind of event for the same thing with nothing else in between, and a dropped
  repeat is logged at the normal level.
- **Works with tomlkit 0.15.** Removing the last action left `actions = []` at
  the top of the file (tomlkit 0.15 writes an emptied `[[actions]]` list that
  way), which also broke the file's first line. The key is now removed with its
  last entry.

### Dependencies

- tomlkit **0.11.7** or newer (was 0.11.4), **except 0.15.0**: 0.11.4 to 0.11.6
  fail two of the tests on variables, and 0.15.0 writes invalid TOML when a
  variable in an inline table is changed (the edit is refused and rolled back;
  fixed in tomlkit 0.15.1). Tested with 0.11.7, 0.12, 0.13, 0.14, 0.15.1 and
  0.15.2.

### Documentation

- README: installing into a virtual environment without the system packages
  (PyGObject's build needs, and `PyGObject<3.51` on Ubuntu 22.04), and the
  `UDISKS_AUTO=0` udev rule as the clean way to keep a partition from being
  automounted, rather than an action that unmounts it.

## [0.5.0] - 2026-09-21

### Added

- **`instance` and a state folder, to match an unplug with its plug.** Every plug of a
  USB device and every mount of a filesystem has one `instance` value (`{instance}`,
  `$UDEVMON_INSTANCE`), identical in all its events and new for the next plug or mount,
  and a private folder (`{state_dir}`, `$UDEVMON_STATE_DIR`,
  `$XDG_RUNTIME_DIR/udevmon/state/<instance>`) that exists from the first action to the
  end of its last event, so a connect script can leave what it found out (the serial that
  `ykman` reported) for the disconnect script. Wiped when the daemon starts and stops.
  `instance` shows in `--all-props` and cannot be used in tag rules. See "Matching an
  unplug with its plug" in the README.
- **`retries` and `retry_delay`** on an action (`actions add --retries N --retry-delay S`,
  `actions set` the same): a command that exits with a non-zero status is run again after
  the delay, up to `retries` more times, for things like `udisksctl unmount` right after a
  desktop mounted the drive. Never after a timeout, when the program cannot start, or when
  the action was skipped; each attempt has its own timeout; retrying stops when automation
  is paused or the daemon is stopping; a `lock` is held across the attempts. Not with
  `detach`. `actions list` and `explain` show it.
- **`lock = "name"`** on an action (`actions add --lock NAME`, `actions set --lock NAME`
  / `--no-lock`): actions that share a lock name never run at the same time, and run in
  the order their events arrived, so the actions of a plug are never overtaken by those of
  the unplug that follows. Not affected: actions without a lock, other lock names, and the
  file order within one event. Released when the command exits (or, for `detach`, has
  started), also after a crash. `actions list` and `explain` show it, and the log says
  when an action had to wait. See "Running actions one at a time" in the README.
- **`detach = true`** on an action (`actions add --detach`, `actions set --detach` /
  `--no-detach`): the command is started in its own session and not waited for, for a
  command that is itself a long-running program such as `keepassxc`. No timeout (setting
  one is an error), the actions after it and `shutdown` are not held up, its output is
  logged and its exit status is logged when it ends. `actions list` and `explain` show
  it. See "Programs that keep running" in the README.
- **`[daemon] script_dir`**, a folder or a list of folders for your own scripts. A
  command given by bare name is looked for in them, in order, and then only in the
  standard system paths (`/usr/local/sbin`, `/usr/local/bin`, `/usr/sbin`, `/usr/bin`,
  `/sbin`, `/bin`), never in the daemon's own `PATH`, so it is found the same way
  under systemd and in a terminal. Resolved at every event (a script added later
  needs no reload), shown in full by `explain`, and checked by `check`. Without it
  nothing changes.
- **`scan --all-props` and `present --all-props`** print every property of each thing,
  one per line, including the raw `udev_*` ones that the normal output hides, and mark
  those that change on every plug (they cannot be used in tag rules). It shows the
  names to use in a rule or as `{udev_NAME}` in a command.

### Fixed

- **A script that leaves a program running in the background** (`keepassxc &`, then
  exit) made the action look stuck until its `timeout`, log "timed out" and be
  reported as failed: the program kept the output pipe open and udevmon waited for the
  pipe, not for the script. udevmon now waits for the command to exit; the program
  keeps running, and whatever it writes is still logged. Output is also logged line by
  line as it arrives instead of at the end.
- **`{udev_NAME}` placeholders** (for example `{udev_id_model}`) in a command made the
  configuration fail to load, although the documentation said raw udev properties can
  be used. They are accepted for every event; a thing that lacks the property makes the
  action be skipped, with an error in the log, as for any unavailable placeholder.

## [0.4.1] - 2026-09-21

### Changed

- **Devices without a serial number are no longer given a suggested match.** `scan`
  used to pre-select `vendor_id`, `product_id` and `port_path`, which ties the rule to
  a USB port (it silently stops matching in another port or behind a hub, and matches
  any unit of the model in that port). It now says nothing identifies the unit and asks
  you to choose between "this model, in this port only" and "this model, in any port",
  or type a list.
- `present` and the scan list mark such a device `[medium: no serial, matches the
  port]`, and `check` notes any device or tag rule that uses `port_path`.

## [0.4.0] - 2026-09-21

### Added

- **Wildcards in tag rules.** `*` (any run of characters) and `?` (one) in a rule
  value, for example `fs_label = "BACKUP*"`. The whole value must match, ignoring
  capitals; nothing else is special. A rule with a pattern is rated weak. In a
  device's own `match` they stay ordinary characters, and `check` warns.
- **"All of" for tags.** An action with several tags can require every one of them:
  `tag_mode = "all"` (default `"any"`), `actions add --all-tags`, and
  `actions set --all-tags` / `--any-tags`. `actions list` and `explain` show it.
- **Opting a device out of a rule tag.** `exclude_tags` on a device, and
  `devices exclude` / `devices include` (also `devices add --exclude-tag`). The
  device stays named and keeps its own actions; explicit tags are never affected.
  `explain`, `scan` and `present` show `excluded from: TAG`, and renaming a tag
  updates `exclude_tags`.
- **Per-device variables.** `vars = { dest = "..." }` on a device, used as
  `{var_dest}` in commands and as `UDEVMON_VAR_DEST` in the environment, so one
  action can serve several devices differently. `devices add --var NAME=VALUE`,
  `devices set --var NAME=VALUE --unset-var NAME`, listed by `devices show`. A
  variable no device defines is an error; `check` warns about devices that lack
  one, and about `allow_unnamed` actions that use one (unnamed things have none).

- **`actions set NAME -- COMMAND...`** replaces an action's command, as `actions add`
  takes it. A placeholder the action's events don't provide is refused, and a
  comment after the old `run` line is kept.
- **`tags set NAME`** changes a declared tag: `--match` replaces its rule,
  `--no-rule` removes it, `--description` changes the text. Comments and layout
  are kept, and a rule with a property that changes from plug to plug is refused.

## [0.3.0] - 2026-09-21

### Added

- **Device status: `disabled` and `ignored`.** A `status` key on a device (absent
  or empty means active). A disabled device's own actions don't run, but actions
  reached through its tags still do; an ignored device runs nothing at all, and
  ignore wins if a thing matches several devices. Both stay "named", so they never
  fall into the unnamed category. New commands: `devices disable`, `devices ignore`
  and `devices enable`, which take several names and change nothing unless all exist.
- **Renaming.** `devices rename`, `tags rename` and `actions rename` change a name
  everywhere it is used (headers and sub-tables, every action's `device` or `tag`,
  the `tags` of devices), keep comments, layout and order, and refuse (writing
  nothing) unless the result is exactly the old config with that one name changed.
  They report what was updated, made-up action names that changed, and the comments,
  descriptions and commands that still mention the old name (left alone).
- **`devices set NAME`** replaces a device's match (`--match`) and/or its
  `--description`, keeping a comment after the old value.
- **Global pause.** `udevmon pause` stops every action without touching the config,
  `udevmon pause --for 30m` (`90s`, `2h`, `1d`, `1h30m`) for a while, and
  `udevmon resume` lets them run again. Nothing runs while paused, not even
  `startup` and `shutdown` actions, and events during a pause are not replayed. The
  state is a file in the runtime directory, so it survives a daemon restart but
  not a logout or reboot. `explain` and the editing commands say when automation
  is paused, and the daemon's start-up line shows `[PAUSED]`.
- **Surprise removal.** A mounted filesystem that vanishes without being unmounted
  (a pulled stick) now fires a new **`removed`** event after `unmounted`, so an
  action can warn about it (`on = ["removed"]`). The new `surprise` value (`{surprise}`,
  `$UDEVMON_SURPRISE`) is `1` for a pulled stick and `0` after a clean unmount, on
  `unmounted` and `removed`. Existing `on = ["unmounted"]` actions still fire.
- **`enabled = false` on actions**, with `actions disable` and `actions enable`.
- **`actions set NAME`** changes `allow_unnamed` (`--allow-unnamed`, `--named-only`,
  `--default-unnamed` to clear it), the events (`--on`) or the `--timeout` of an
  action without removing and re-adding it.
- **Retargeting actions:** `actions set NAME` also takes `--add-device`,
  `--remove-device`, `--add-tag` and `--remove-tag` (repeatable) to change which
  devices or tags an action applies to. Comments and layout are kept; removing a
  target the action lacks, or its last target, is refused.
- **`scan` can ignore things:** answer `i3` (or `i1,3`) instead of a number to save
  them as ignored devices, without asking for tags.
- `devices list`, `devices show`, `actions list`, `present`, `scan` and `explain`
  mark what is switched off; `explain` says why an action was skipped (disabled
  action, disabled device) and `check` notes it.
- **Manual pages:** `udevmon(1)` for every command and option, and
  `udevmon.toml(5)` for the configuration file, the events, the properties and
  how commands are run. They are installed by `pip` (under `share/man`) and can
  be read from a checkout with `man -l man/udevmon.1`. A test fails when a
  command, option, config key, event or property is missing from them.

### Fixed

- **Names containing a dot** (`usb.1`) were accepted but written as a nested header, so
  the file was rejected. They are now written quoted (`[devices."usb.1"]`).
- **Clearer reasons** when an edit cannot proceed: the message names the entry it could
  not find, and says how to give an unnamed action a name so it can be changed.
- **Documentation:** the README described property matching as exact; values are
  compared without regard to capitals (`fs_label = "backup"` matches `BACKUP`).

### Internal

- **`tools/usb-test.py`**, a guided real-hardware test: it asks you to plug a
  stick in, pull it out and plug it in again, and checks events, status, pause,
  the unnamed gate and surprise removal (16 checks) on a throwaway config. The
  `removed` event and `surprise` value were confirmed on a real stick with it.
- The test suite no longer leaves temporary folders behind: it works in one
  per-run folder that is removed on exit, and a test guards this.

## [0.2.0] - 2026-09-20

### Added

- **Bash tab completion.** `udevmon completion install` (or `completion bash`
  to print the script) completes commands, sub-commands and options, and reads
  your config for live names: devices, tags, actions, event lists and
  `--match` keys. `udevmon completion uninstall` removes it.
- **`udevmon explain`** shows what would run for everything plugged in now, or
  for a simulated thing (`--device NAME`, `--prop KEY=VALUE`). It applies the
  same rules as the daemon (named or unnamed, tags, `allow_unnamed`), shows
  each command with its values filled in, and says why an action is skipped.
- **`udevmon service uninstall`** stops and disables the service, removes the
  unit file and reloads systemd. Files that are not udevmon's own are left
  alone.
- **Automatic reload after edits.** The commands that add, remove, tag or
  untag devices, tags and actions, and `scan` when it saves a device, reload a
  running daemon that uses the same config file. `--no-reload` skips it.
- **Warning for programs that cannot run.** `check` warns when an action's
  program is not found in `PATH` or is not executable. The daemon logs the same
  warning at start and on every reload, using its own `PATH`.

### Changed

- **Editing existing entries uses `tomlkit`.** `devices remove|tag|untag`,
  `tags remove` and `actions remove` keep comments and layout however the file
  was written by hand, and keep CRLF line endings (the old editor turned them
  into LF). Adding new entries still appends text as before.
- **New dependency: `tomlkit` 0.11.4 or newer**, installed automatically by
  `pip`. Only the commands above need it; adding entries, `check`, `explain`
  and the daemon do not, and a missing `tomlkit` produces a message saying how
  to install it. The system package is recent enough on Debian 13 and Ubuntu
  24.04; Ubuntu 22.04's is too old, so `pip` fetches a current one there.
- **The daemon's lock file** (`$XDG_RUNTIME_DIR/udevmon.lock`) now holds the PID
  on the first line and the resolved config path on the second.

### Fixed

- **Commented table headers.** A comment after a header (`[devices.x]  # note`,
  `[[actions]]  # note`) or after an action's `name = "x"` made `remove` and
  `tag` fail with "could not locate block" or "has no explicit name".
- **Hand-formatted files.** Editing failed on a device written as a sub-table
  (`[devices.x]` plus `[devices.x.match]`), on a block whose multi-line string
  has a line starting with `[`, and on a multi-line `tags` array.
- **Documentation.** A doubled code fence in the README and SPECIFICATIONS
  rendered the command list as an empty box followed by plain text.

### Internal

- The test suite grew from 94 to 194 tests. It gets its own runtime directory,
  so it can never signal a real daemon. New tests keep the docs renderable
  (balanced code fences, code lines that fit an 80-column terminal) and fail
  when the CLI gains a command, positional argument or value option that shell
  completion does not cover.

### Upgrading

- If you run the service, restart it after upgrading
  (`systemctl --user restart udevmon`). An old `udevmon reload` cannot read the
  new two-line lock file and would report that no daemon is running. A new CLI
  works with an old daemon.
- Editing commands now reload the daemon by themselves; add `--no-reload` to
  get the previous behaviour.

## [0.1.0] - 2026-09-20

First version.

### Added

- **Automation without root.** A daemon watches USB devices (udev) and
  filesystem mounts (udisks2 over D-Bus) and runs your commands on `connected`,
  `disconnected`, `mounted`, `unmounted`, and, for things already present,
  `startup` and `shutdown`.
- **Named devices** recognised by serial number, filesystem UUID or partition
  UUID, with a strength rating (strong, medium, weak) and warnings for weak
  matches. **Tags**, set on devices or selected by rules on properties, and
  actions that target one device, several devices or a tag.
- **Safe autorun.** Things you have not named run only actions that set
  `allow_unnamed = true`; `check` explains the choice.
- **Configuration** in a TOML file (`~/.config/udevmon/config.toml`), with
  placeholders (`{mountpoint}`, `{device}`, `{tag}`, ...) and `UDEVMON_*`
  environment variables. Commands are never run through a shell, each action has
  a timeout, and a config that is not yours or is writable by others is refused.
- **Commands:** `scan` (watch events and name devices, also for things already
  present with `--present`), `present`, `devices`, `tags`, `actions`, `check`,
  `run` (with `--dry-run`), `reload` and `service install`.
- **Version taken from git**, in the format setuptools-scm uses.
- **Python 3.10 or newer** (with `tomli` as the reader on 3.10), a README and a
  detailed SPECIFICATIONS, and 94 tests.
