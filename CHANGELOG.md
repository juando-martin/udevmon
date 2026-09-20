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

- **Device status: `disabled` and `ignored`.** A `status` key on a device (absent
  or empty means active). A disabled device's own actions don't run, but actions
  reached through its tags still do; an ignored device runs nothing at all, and
  ignore wins if a thing matches several devices. Both stay "named", so they never
  fall into the unnamed category. New commands: `devices disable`, `devices ignore`
  and `devices enable`, which take several names and change nothing unless all exist.
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
- **`scan` can ignore things:** answer `i3` (or `i1,3`) instead of a number to save
  them as ignored devices, without asking for tags.
- `devices list`, `devices show`, `actions list`, `present`, `scan` and `explain`
  mark what is switched off; `explain` says why an action was skipped (disabled
  action, disabled device) and `check` notes it.

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
