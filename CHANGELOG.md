# Changelog

All notable changes to udevmon are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the version numbers follow
[Semantic Versioning](https://semver.org/) (while below 1.0, a new minor
version may change behaviour).

How to keep it: add a line under **Unreleased** in the same commit as any
change a user would notice (new or changed commands, options, config keys,
dependencies, behaviour, fixes). When a release is tagged, rename that heading
to the version and date, and start a new empty **Unreleased** above it.

## [Unreleased]

### Added

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
