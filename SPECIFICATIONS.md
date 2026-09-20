# udevmon — Specifications

Run user-defined actions when USB devices are connected/disconnected or when
removable filesystems are mounted/unmounted, configured entirely by an
unprivileged user.

## 1. Goals

- Detect USB connect/disconnect and filesystem mount/unmount **without root**.
- Let the user give a USB device or filesystem a **name** (a *device*) by running
  the tool in a *scan mode*, then attach *actions* to that name.
- Let one action apply to **several devices**: by listing them, by **tags** set
  on devices, or by **tag rules** that select things by their properties.
- Match devices on **stable identifiers** (serial number, filesystem UUID,
  partition UUID) whenever available; clearly flag weaker matches.
- Keep autorun safe: actions run for hardware the user has not named only when
  the action explicitly allows it (§5.5).
- Run continuously with negligible resource use (event-driven, no polling).
- Configuration is a hand-editable **TOML** file, plus a **CLI** for common edits.
- Leave room to extend to other event sources and device types, and to add a
  TUI/GUI later.

## 2. Non-goals (v1)

- No TUI/GUI (the config format and CLI are designed so one can be added).
- No root component, no udev rules installed (optional later, see §12).
- No control socket between CLI and daemon (reload is by `SIGHUP`).
- No Bluetooth / network / display sources yet (extension points exist, see §11).
- No backwards compatibility with earlier config spellings while the tool is
  preliminary; format changes are made in place.
- Tags have "any of" semantics only, and a named device cannot be excluded from
  a rule tag (see §12).

## 3. Platform and dependencies

- Linux, Python >= 3.10. TOML is read with the standard library's `tomllib`
  (3.11+) or, on 3.10, the `tomli` package (same API).
- `tomlkit` >= 0.11.4, only for the commands that change an existing device, tag or
  action (`devices remove|tag|untag`, `tags remove`, `actions remove`). It reads
  and writes TOML keeping comments and layout. It is imported when such a command
  runs, so the daemon, `check`, `explain` and the `add` commands do not need it; a
  missing `tomlkit` gives a message saying to install it. Older releases (0.9.2,
  the one in Ubuntu 22.04's apt) leave artifacts (a stray `[devices]` header, a
  repeated comment), and the whole test suite passes from 0.11.4 up.
- `pyudev` — udev netlink monitor (readable by unprivileged users).
- PyGObject (`gi`: `GLib`, `Gio`) — main loop and D-Bus client for
  `org.freedesktop.UDisks2` on the system bus. No extra D-Bus library.
- `udisks2` running (already true on automounting desktops).
- Tests use only `unittest`.

## 4. Architecture

```
 ┌────────────┐
 │ UdevSource │──┐
 └────────────┘  │    ┌───────────────┐    ┌──────────────┐
                 ├───▶│ Engine        │───▶│ ActionRunner │
 ┌────────────┐  │    │ resolve, gate │    │ threads,     │
 │UdisksSource│──┘    │ debounce      │    │ subprocess   │
 └────────────┘       └───────────────┘    └──────────────┘
                              ▲
                       Config (TOML)
```

Sources emit `Event(kind, endpoint)`; the engine resolves, gates and debounces
them, and the runner executes the selected actions.

- One GLib main loop drives every source. Nothing polls.
- A **source** turns raw system notifications into normalized `Event`s.
- The **engine** resolves each event's endpoint to the named devices and tags it
  belongs to (§5.5), selects the actions that apply, debounces, and hands them to
  the runner.
- The same sources feed `scan`, `present` and the daemon.

### 4.1 Data model

`Endpoint`: a USB device or filesystem as observed by a source (not to be
confused with a user-named *device* in the config): `source`
(`"udev"`/`"udisks"`), `key` (stable id while present: sysfs path or udisks
object path), `props` (`dict[str, str]`).

`Event`: `kind`, `endpoint`, `time`.

Event kinds (extensible registry):

| kind           | source | meaning                                           |
|----------------|--------|---------------------------------------------------|
| `connected`    | udev   | a matching udev device was added                  |
| `disconnected` | udev   | it was removed (props come from a cache)          |
| `mounted`      | udisks | a filesystem gained a mount point                 |
| `unmounted`    | udisks | it lost its mount point, or its device vanished   |
| `startup`      | both   | the thing was already present when the daemon started |
| `shutdown`     | both   | the thing is still present when the daemon exits  |

Removal events carry the properties cached when the thing appeared, so
serial/UUID matching works on unplug.

`startup` and `shutdown` are the "already there" counterparts: once the sources
are attached, the daemon emits one `startup` event per present USB device and
filesystem (mounted or not, system ones included); on `SIGTERM`/`SIGINT` it emits
one `shutdown` event per thing still present, then waits for those actions to
finish before exiting. They are ordinary kinds, so `on = ["startup", "connected"]`
means "whenever this thing becomes known to me". Placeholders for these kinds are
the union of USB and filesystem keys; a missing one (e.g. `{mountpoint}` for an
unmounted filesystem) skips that action. `shutdown` actions do **not** run if the
daemon is killed (`SIGKILL`), crashes, or the machine loses power.

### 4.2 Normalized properties

Match keys and placeholders use these names.

USB devices (udev, `usb/usb_device`):

- `vendor_id`, `product_id` — `ID_VENDOR_ID`, `ID_MODEL_ID` (lowercase hex)
- `serial` — `ID_SERIAL_SHORT`
- `vendor`, `model` — `ID_VENDOR_FROM_DATABASE`/`ID_VENDOR`, `ID_MODEL_FROM_DATABASE`/`ID_MODEL`
- `port_path` — sysfs device name, e.g. `2-1.3` (physical port)
- `devnode`, `syspath`, `subsystem`, `devtype` — as reported
- `udev_<prop>` — every raw udev property, lowercased (e.g. `udev_id_input_keyboard`) — an escape hatch for device classes with no normalized keys

Filesystems (udisks):

- `fs_uuid`, `fs_label`, `fs_type` — `Block.IdUUID/IdLabel/IdType`
- `part_uuid` — `Partition.UUID` (GPT)
- `devnode` — `Block.Device`
- `mountpoint` — first entry of `Filesystem.MountPoints` (mount/unmount events)
- `drive_vendor`, `drive_model`, `drive_serial` — `Drive` of the block device

Added by the engine for actions (not present on the endpoint, so not matchable):

- `event` — the event kind
- `device` — name of the named device that applies; **absent** when the thing is unnamed
- `tag` — for tag-targeted actions, the first of the action's tags the thing has

**Volatile keys** change from plug to plug or event to event and are rejected in
tag rules (§5.4): `mountpoint`, `devnode`, `syspath`, `event`, `device`, `tag`,
and the raw properties `udev_devname`, `udev_devpath`, `udev_busnum`,
`udev_devnum`, `udev_seqnum`, `udev_usec_initialized`.

## 5. Devices, tags and matching

Three concepts, plus the actions that use them:

- **device** — a name plus a `match` table identifying one thing you know
  (a USB device or a filesystem). Naming a thing means the user vouches for it.
- **tag** — a label for a set of things: those whose devices carry it explicitly
  (`tags = [...]` on the device) and those selected by a **rule** (`[tags.X]`
  with a `match`).
- **action** — targets devices (`device`) or tags (`tag`).

### 5.1 Device matching

A device's `match` is a table of `key = "value"` pairs. An endpoint matches the
device when **every** pair equals the corresponding endpoint property
(case-insensitive). A missing property never matches. Device matches are exact
values only (no lists or wildcards): a device identifies one thing.

### 5.2 Match strength

Reported by `scan`, `present`, `devices list` and warned about on creation:

- **strong** — includes an identity-grade key: `serial`, `fs_uuid`,
  `part_uuid`, `drive_serial`, and the serial is not suspicious.
- **medium** — model plus physical location: `vendor_id` + `product_id` +
  `port_path`. Matches any such device in that port.
- **weak** — anything else (e.g. only `vendor_id`+`product_id`, or a label).
  Matches every device of that model / any filesystem with that label.

A serial is *suspicious* (downgraded to weak) when it is empty, all one
repeated character, or a well-known placeholder (`0123456789ABCDEF`,
`1234567890`, …). Cheap sticks often share such serials.

### 5.3 Default keys chosen by scan

- USB: `vendor_id`, `product_id`, `serial` if a good serial exists; otherwise
  `vendor_id`, `product_id`, `port_path` (medium, flagged).
- Filesystem: `fs_uuid`; else `part_uuid`; else `drive_serial`+`fs_label`;
  else `fs_label` (weak, flagged).

The user can override the keys interactively.

### 5.4 Tags

**Explicit tags** are listed on a device: `tags = ["backup", "usb-storage"]`. They
need no declaration.

**Rule tags** are declared as `[tags.NAME]` with a `match` table. A value is a
string (equals, case-insensitive) or a non-empty list of strings (equals any).
All pairs must match. Rules are evaluated against the endpoint's properties at
event time, so a rule can use any stable key, including ones the device's own
`match` does not mention. Volatile keys (§4.2) are rejected. A `[tags.NAME]`
without `match` only carries a `description`.

A rule's strength is computed as in §5.2, except that a key with a list value never
counts as identity-grade or as location (a list can match many things).

Tag names use the device-name character set and live in their own namespace
(a tag and a device may share a name).

### 5.5 Resolution and trust

For an event on endpoint *E* the engine computes:

- **named devices** `N(E)`: every configured device whose `match` matches *E*,
  in file order;
- **tags** `T(E)`: the explicit tags of each device in `N(E)`, then every rule tag
  whose rule matches *E*, without duplicates (union).

*E* is **named** when `N(E)` is not empty, otherwise **unnamed**.

- **named, tag set explicitly**: tags: explicit tags (+ any matching rule tags). Runs: every action targeting the device or the tag.
- **named, and a rule also matches**: tags: union. Runs: same as above.
- **unnamed, a rule matches**: tags: the rule tags. Runs: only tag actions that **allow unnamed**.
- **unnamed, no rule matches**: tags: none. Runs: none.

Naming is what grants trust, so a named device is never restricted by the
allow-unnamed setting; that setting only *widens* who a tag action may run for.

Trust belongs to each endpoint, not to the physical stick: a stick shows up as a
USB device and as one or more filesystems, and naming one of those does not make
the others named. `scan` makes it easy to name several at once (§8.1).

### 5.6 `allow_unnamed`

A tag action may set `allow_unnamed`:

- `true` — may also run for unnamed things matching the tag.
- `false` — runs for named devices only (stated explicitly).
- absent — behaves as `false`; `check` prints a note for actions on rule tags
  (§6.2) so the choice is made consciously.

It is an error on actions that target `device` (only tag actions can see unnamed
things). An action allowed to run for unnamed things runs on hardware the user
has not vetted whose properties (labels, model strings, serials) are chosen by
whoever built it; such actions should be limited to harmless commands
(notifications, logging), never read or execute the thing's contents, and treat
the `UDEVMON_*` environment as untrusted. `startup` and `shutdown` follow the same
rules, so an allowed action on a broad rule fires once for every present matching
thing, including system filesystems.

### 5.7 Device status and disabled actions

A device has a **status**: `active` (the default), `disabled` or `ignored`. In the
file it is the `status` key of the device: absent, `""` or `"active"` mean active;
any other value than `"disabled"` and `"ignored"` is an error. An action has
`enabled` (a boolean, default `true`).

- A thing that matches a device is *named* whatever that device's status, so a
  disabled or ignored device never makes it unnamed (which would let it run
  `allow_unnamed` actions, §5.6).
- **disabled:** the actions that list this device in `device` do not run for it.
  Actions that reach it through a tag (explicit or by rule) run as for any named
  device.
- **ignored:** nothing runs. If a thing matches several devices and *any* is ignored,
  nothing runs for it (an ignore cannot be overridden by another definition).
  `disabled` never conflicts across devices: it only turns off that device's own
  actions, and another matching device's actions carry on.
- **`enabled = false`:** the action never runs.
- `startup` and `shutdown` follow the same rules.

`check` lists what is switched off (notes, §6.2). `explain` marks `[disabled]` /
`[ignored]` in the device list, prints `ignored (NAME): nothing runs for this thing`,
and reports an action that would have applied as skipped, with the reason
(`the action is disabled`, `device 'X' is disabled`); actions that would not have
applied anyway are not mentioned.

## 6. Configuration

Path: `$XDG_CONFIG_HOME/udevmon/config.toml` (default `~/.config/udevmon/`),
overridable with `--config`. Unknown keys are errors (catches typos).

```toml
[daemon]                      # all optional
debounce = 1.0                # ignore repeats within N seconds
udev_filters = ["usb/usb_device"]   # "subsystem" or "subsystem/devtype"
max_workers = 4

[devices.backup-disk]
description = "Weekly backup drive"
match = { fs_uuid = "3F2A-91BC" }
tags = ["backup", "storage"]           # explicit tags (optional)
# status = "disabled"   # or "ignored"; absent = active (§5.7)

[devices.yubikey]
match = { vendor_id = "1050", product_id = "0407", serial = "A1B2C3D4E5" }

[devices.card-reader]
match = { vendor_id = "2ce3", product_id = "9563", port_path = "3-3" }

[tags.storage]                         # rule tag: selected by properties
description = "Filesystems I care about"
match = { fs_type = ["ext4", "exfat"] }    # list = any of

[[actions]]                            # one device
name = "run-backup"                    # optional; generated if missing
device = "backup-disk"                 # a name, or a list of names
on = ["mounted"]                       # string or list
run = ["~/bin/backup.sh", "{mountpoint}"]   # argv list or shlex string
timeout = 300                          # seconds, default 60

[[actions]]                            # several devices
device = ["yubikey", "card-reader"]
on = ["connected", "disconnected"]
run = ["notify-send", "{device} {event}"]

[[actions]]                            # a tag, including unnamed things
tag = "storage"                        # a name, or a list (any of)
on = ["mounted"]
allow_unnamed = true
run = ["notify-send", "Drive mounted ({tag})"]

[[actions]]                            # a tag, named devices only
tag = "backup"
on = ["mounted"]
allow_unnamed = false
run = ["~/bin/backup.sh", "{mountpoint}", "{device}"]
```

Rules:

- Names (devices, tags, actions) match `[A-Za-z0-9_.-]+`; device, tag and action
  names are each unique within their kind.
- An action has **exactly one** of `device` or `tag`, each a name or a non-empty
  list of names. `device` values must be existing devices. `on` values must be
  known kinds.
- `run` is **never passed through a shell**. Use `["sh", "-c", "…"]` explicitly
  if a shell is wanted. A leading `~` in each argument is expanded.
- Placeholders are `{lower_snake_name}`; `{{`/`}}` produce literal braces. Any other
  brace use is left untouched, so shell text such as `${1:-x}`, `${HOME}` or
  `awk '{print $1}'` needs no escaping. At run time, an action whose placeholder
  is missing is **skipped with an error**, never run with an empty value
  (protects against e.g. `rm {mountpoint}/x`).
- `{device}` is the named device that applies (§7); it does not exist for
  unnamed things, so such an action run for one is skipped. `{tag}` exists only for
  tag actions.
- Actions run with environment `UDEVMON_EVENT`, `UDEVMON_DEVICE` (when named),
  `UDEVMON_TAG` (tag actions), and `UDEVMON_<KEY>` for every property,
  cwd = `$HOME`, stdin = `/dev/null`.
- Because actions execute commands, the daemon **refuses to load** a config
  that is not owned by the user or is group/world-writable.
- The CLI edits the file so that comments and formatting elsewhere are preserved,
  however it was written by hand. New entries (`devices add`, `tags add`,
  `actions add`) are appended as text in the CLI's own style. Changes to existing
  entries (remove, tag, untag) are made with `tomlkit`, which finds a table by its
  parsed name, so it does not matter how the header is spelled (indented, spaces
  inside the brackets, quoted key, trailing comment), whether a device is an inline
  table or a sub-table (`[devices.x.match]`), or whether an array or string spans
  several lines. An array is edited in place, keeping its layout; when no tags are
  left the `tags` key is removed (never an emptied array).
- Every edit is written only if the result re-parses with the strict reader and
  does what was asked; otherwise the file is left byte for byte as it was. A
  `config.toml.bak` is written before each edit. Line endings (LF or CRLF) and a
  missing final newline are preserved. A comment above a removed block stays.

### 6.1 Errors

`check` reports these and exits 1; the daemon refuses to start with them, and on
reload keeps the previous configuration:

- unknown keys; wrong types; invalid names; duplicate names;
- an action with both or neither of `device`/`tag`; a `device` that does not exist;
- `allow_unnamed` on an action that targets `device`;
- a tag rule with an empty list, a non-string value, or a volatile key;
- a placeholder unavailable for the action's event kinds (`{mountpoint}` on
  `connected`; `{tag}` on a `device` action);
- an invalid `timeout`;
- a device `status` other than `"disabled"`, `"ignored"`, `"active"` or `""`, or a
  non-boolean `enabled` (§5.7).

### 6.2 Warnings and notes

Warnings are printed by `check` and logged at daemon start and reload; notes are
printed by `check` only. Neither changes the exit status.

- warning: a device with a weak match (as today);
- warning: an action's tag that no device carries and that has no rule (never runs);
- warning: an `allow_unnamed = true` action that uses `{device}` (skipped for
  unnamed things);
- warning: an `allow_unnamed = true` action that uses placeholders derived from
  hardware-supplied values (anything other than `event`, `tag`, `devnode`,
  `syspath`, `port_path`, `subsystem`, `devtype`);
- warning: a device `match` that uses a volatile key;
- warning: an action whose program cannot be run (`run[0]`, with `~` expanded, is not
  found in `PATH`, does not exist, or is not an executable file; skipped when it
  contains a placeholder). This depends on the environment, so it is computed
  by `config.command_problems()` and not stored in `Config.warnings`: `check`
  uses the shell's `PATH`, the daemon logs it at start and on reload with its own;
- note: an action on a tag with a rule (any of its tags) where `allow_unnamed` is absent — "runs only
  for named devices in tag X; set `allow_unnamed` to choose";
- note: an `allow_unnamed = true` action on a tag whose rule is not strong — "any
  hardware presenting these properties can trigger it";
- note: a tag carried by devices but used by no action;
- note: a disabled or ignored device, a disabled action, and an action that only
  targets disabled or ignored devices (so never runs).

## 7. Daemon (`udevmon run`)

- Single instance (`flock` on `$XDG_RUNTIME_DIR/udevmon.lock`, which also
  stores the PID on the first line and the resolved config path on the second).
- Starts sources, primes caches, emits `startup` events for things already present
  (udisks ones once it attaches), then loops.
- `SIGHUP`: re-read config; on error keep the old config and log. Changes to
  `udev_filters` need a restart (logged). Warnings (§6.2) are logged again.
- The editing CLI commands, and `scan` after saving a device, send `SIGHUP` to the
  running daemon when its recorded config is the same file as theirs (compared
  after resolving symlinks; a daemon whose config is unknown, from an older lock
  file, is assumed to match). They print `Reloaded the running daemon.`
  `--no-reload` skips it. A daemon running another config is left alone.
- `SIGTERM`/`SIGINT`: emit `shutdown` events, wait for those actions (bounded by each
  action's `timeout`), stop cleanly.
- For each event, in this order:
  1. Resolve `N(E)` and `T(E)` (§5.5). Unnamed things with no tags stop here (debug
     log). If any device in `N(E)` is `ignored`, stop: nothing runs (§5.7).
  2. Walk the actions in file order and select those whose `on` includes the kind and
     that apply. A `enabled = false` action is skipped (with a reason, for `explain`):
     - `device` actions apply when a listed device is in `N(E)` and at least one such
       device is `active` (a `disabled` device's own actions are skipped); `{device}` is
       the first such active device in `N(E)` (config file order);
     - `tag` actions apply when one of the action's tags is in `T(E)`, and either
       *E* is named or the action has `allow_unnamed = true`. `{tag}` is the first of
       the action's tags (in the order written) that is in `T(E)`; `{device}` is the
       first device in `N(E)` (absent when unnamed).
     An action is selected at most once per event.
  3. If none is selected, stop. Otherwise apply **debounce** to the event's
     *subject* — the first device in `N(E)`, or the endpoint key when unnamed —
     with the event kind: repeats of `(subject, kind)` within `debounce` seconds
     are dropped.
  4. One job per event runs its selected actions **sequentially in config order**;
     different events run in parallel in a small thread pool.
- A thing matching several named devices is logged (info): usually a config smell.
- Selected actions for unnamed things are logged at info with an "unnamed" marker;
  unnamed things skipped because no action allows them are logged at debug.
- Runner: `subprocess.Popen(argv, start_new_session=True)`, combined
  stdout/stderr logged line by line, exit status logged; on timeout the process
  group gets `SIGTERM`, then `SIGKILL` after 5 s.
- Logging to stderr (journald when run as a service). `-v` enables debug.
- `--dry-run` logs what would run without executing.
- If udisks is unavailable at start, log it and keep running with the udev
  source; the udisks source attaches when the service appears
  (`Gio.bus_watch_name`).

## 8. CLI

```
udevmon run [--dry-run] [-v]            run the daemon
udevmon scan [--no-prompt] [--present]  scan mode (below)
udevmon present [--all]                 list what is present now, with
                                        strength, device name and tags
udevmon devices list|show               (bare `devices` = `devices list`)
udevmon devices add NAME --match KEY=VALUE ...
                    [--tag TAG ...] [--description TEXT]
udevmon devices remove NAME
udevmon devices tag NAME TAG [TAG ...]  add explicit tags to a device
udevmon devices disable|ignore|enable NAME...   (§5.7)
udevmon devices untag NAME TAG [TAG ...]
udevmon tags list|show
udevmon tags add NAME [--match KEY=VALUE ...] [--description TEXT]
udevmon tags remove NAME
udevmon actions list
udevmon actions add (--device NAME ... | --tag NAME ...) EVENT[,EVENT ...]
                    [--name NAME] [--timeout SECONDS]
                    [--allow-unnamed | --named-only] -- COMMAND [ARG ...]
udevmon actions remove NAME
udevmon actions disable|enable NAME...
udevmon actions set NAME [--allow-unnamed | --named-only
                          | --default-unnamed]
                    [--on EVENTS] [--timeout SECONDS]
udevmon explain [EVENT] [--device NAME] [--prop KEY=VALUE ...] [--all]
                                        what would run (§8.4)
udevmon check                           validate: errors, warnings, notes
udevmon reload                          SIGHUP the running daemon
udevmon service install [--now] [--dest DIR]
                                        write the systemd user unit
                                        (and optionally enable it)
udevmon service uninstall [--dest DIR]
                                        stop, disable, remove the unit
udevmon completion bash                 print the bash completion script
udevmon completion install [--dest FILE]
                                        install it for the user (§8.2)
udevmon completion uninstall [--dest FILE]
                                        remove the installed script
```

Global: `--config PATH`, `-v`.

The editing commands (`devices add|remove|tag|untag`, `tags add|remove`,
`actions add|remove`) also take `--no-reload` (§7).

- `--match KEY=VALUE` may be repeated. On `tags add`, repeating a key builds a
  list (`--match fs_type=ext4 --match fs_type=exfat` → `["ext4", "exfat"]`); on
  `devices add` repeating a key is an error.
- `--device` and `--tag` may be repeated (lists) but not mixed.
  `--allow-unnamed` / `--named-only` (only with `--tag`) set `allow_unnamed`
  to `true` / `false`.
- `devices remove` is refused while an action lists the device;
  `tags remove` while an action uses the tag (it removes only the `[tags.X]` block,
  never the explicit tags on devices).
- `devices list`/`show` and `tags list`/`show` display membership (explicit
  members of a tag; rule, if any) and the actions that target them.

### 8.1 Scan mode

1. Print a banner and start the same sources as the daemon.
2. Print each event as it happens (kind, description, properties, strength, and
   whether the thing is named and which tags it has).
3. After 2 s without new events, present the collected things as a numbered
   list, noting any that already belong to a named device (and their tags).
4. Prompt: choose one or more numbers (`2` or `1,2`, to name a whole stick at
   once) → for each, enter a name → confirm or edit the match keys
   (default per §5.3, weak/medium warnings shown) → optionally enter tags
   (existing tags are suggested) → device appended to the config.
   `iN` (`i3`, `i1,3`) does the same but saves the devices as `ignored` and asks
   for no tags. Repeat, Enter to continue scanning, `q` to quit.
5. `--no-prompt` (also automatic when stdin is not a TTY) only prints events.
6. `--present` also offers things that are **already present** when the scan
   starts (printed as `present`, one line each, and included in the naming
   list). Without it, scan only reports changes. System filesystems are omitted.

Scan and the daemon are independent listeners and can run at the same time.

### 8.2 Shell completion

`udevmon completion bash` prints a bash script; `udevmon completion install
[--dest FILE]` writes it to `$XDG_DATA_HOME/bash-completion/completions/udevmon`
(default `~/.local/share/...`), which bash-completion loads on demand.

The script is a few lines that call a hidden helper, `udevmon _complete CWORD
WORD...` (not listed in `--help`), with bash's `COMP_WORDS`. The helper
(`completion.py`) reads the real argparse parser, so completion follows the
CLI automatically, and prints a mode line followed by candidates:

- `words`: offer the candidates; `nospace`: the same, without a trailing space
  (`key=`); `files` / `dirs`: let bash complete paths.

`udevmon completion uninstall [--dest FILE]` removes that file. It refuses a file
that does not start with the script's header line, and does nothing (exit 0) when
the file is absent.

What it completes:

- commands, sub-commands and option names, from the parser;
- device, tag and action names, read from the config given by `--config` (or
  the default one): `devices show|remove|tag`, `devices untag NAME` (the
  device's explicit tags), `tags show` (all tags), `tags remove` (declared
  `[tags.X]` only), `actions remove`, `actions add --device|--tag`;
- event kinds as a comma-separated list, without repeating one already typed;
- `--match` keys with a trailing `=` (rules omit the volatile keys of §4.2);
- paths for `--config` and `--dest`; after `--` in `actions add`, files.

Bash splits `a=b` into `a`, `=`, `b`; the helper rejoins them. Nothing is
offered for the value half of `key=value`. Any error (unreadable or unsafe
config, bad arguments) yields no candidates and no output, and the helper
always exits 0. It imports neither `gi` nor `pyudev`.

### 8.3 Service

`service install` writes `~/.config/systemd/user/udevmon.service`
(`Type=simple`, `Restart=on-failure`, `ExecStart=<this program> run`,
`ExecReload=… reload`, `MemoryMax=100M`, `TimeoutStopSec=120` so `shutdown` actions can finish), then `systemctl --user daemon-reload`;
`--now` also runs `enable --now`. Hardening options that would confine
actions (`NoNewPrivileges`, `ProtectSystem`…) are deliberately not set, since
actions are user scripts.

`service uninstall` runs `systemctl --user disable --now udevmon.service` (so the
daemon stops and its `shutdown` actions run), deletes the unit file, then runs
`daemon-reload`. With `--dest DIR` it only deletes `DIR/udevmon.service` and never
calls `systemctl`, mirroring `install --dest`. It removes only a file that contains
`udevmon` and an `ExecStart=` line, otherwise it reports the file and exits 1; a
missing unit is reported and exits 0, and a missing `systemctl` is ignored. The
config is never touched.

### 8.4 Explain

`udevmon explain [EVENT] [--device NAME] [--prop KEY=VALUE ...] [--all]` reports what
the daemon would do, without side effects. It uses the engine's `plan(event)`, which
runs the same resolution and selection as the daemon (§5.5, §7 steps 1 and 2) but
does not debounce or run anything, and also returns the actions the gate skipped
with the reason.

- **Without `--device`/`--prop`:** every USB device and filesystem present now (the
  same snapshot as `present`; `--all` adds system filesystems). For each thing: a
  header (description, `device:` or `unnamed`, `tags:`), then, for each event kind
  that fits it (`connected`, `disconnected` for USB; `mounted`, `unmounted` for
  filesystems; `startup` and `shutdown` for both), the actions that would run
  (`runs 'NAME' (device X | tag T[, unnamed])`, followed by the command with its
  placeholders expanded, or `cannot run here: {name} is not available`) and the
  ones skipped (`skips 'NAME': reason`). Kinds with nothing to say are collapsed
  into `nothing to run for: ...`. An `EVENT` argument limits the report to that kind
  and to the things it applies to.
- **Things that match no device and no tag** cannot trigger anything, so they are
  omitted, with a count; `--all` lists them.
- **With `--device NAME` and/or `--prop`:** a simulated thing whose properties are the
  device's `match` keys plus the `--prop` pairs (a repeated key is an error). It is a
  filesystem when `EVENT` is `mounted`/`unmounted`, a USB device for
  `connected`/`disconnected`, else decided by whether it has filesystem properties.
  Only the given properties are known, which the output says.
- Exit status 0, or 1 for an unknown device or a malformed `--prop`.

## 9. Resource expectations

Event-driven; idle CPU ≈ 0 with no timers. Resident memory ≈ 20–35 MB.
A few ms of CPU per event (tag rules are a handful of dictionary comparisons).
No disk I/O apart from reading the config.

## 10. Security considerations

- Autorun on plug-in is inherently risky: prefer strong matches; weak/medium
  matches are flagged at creation and in `check`.
- **Unnamed hardware:** rule tags can select things the user never named, and
  their properties are attacker-controllable (vendor/product IDs, labels, filesystem
  types, serials are all chosen by whoever builds the device). Such things can
  trigger only actions that explicitly set `allow_unnamed = true`, which is opt-in
  per action and never the default. `check` flags weak rules, `{device}` use and
  the use of hardware-supplied values in such actions.
- No shell interpolation; placeholder values are passed as separate argv
  elements, never concatenated into a command string. The `UDEVMON_*` environment
  carries every property, so scripts must treat it as untrusted for unnamed things.
- Config ownership/permission check (§6). Runs only as the invoking user.

## 11. Extensibility

- **Sources** implement `start(emit)`, `stop()`, `snapshot()`; adding
  Bluetooth (BlueZ D-Bus) or NetworkManager means a new module and new event
  kinds, with no change to config format, matching, tags or actions.
- **More udev device classes** need only `udev_filters` plus `udev_<prop>`
  match keys, no code; tag rules make them selectable as a class.
- **TUI/GUI** can edit the same TOML via the config-edit module, and could
  later talk to the daemon over a control socket.

## 12. Optional later work

- One-time root udev rule forwarding events to a socket-activated user
  service for zero idle footprint / device permission tweaks.
- Control socket (live scan through the daemon, no SIGHUP).
- Per-action environment/cwd, `enabled = false`, startup replay option.
- Tags: an exclusion list so a named device can opt out of a rule tag
  (`exclude = [...]`), "all of" semantics for multiple tags, wildcards in rules,
  per-device variables for use in actions.

## 13. Layout and testing

```
pyproject.toml
setup.py                          version from git (§13.1)
src/udevmon/{__init__,__main__,cli,config,configedit,events,matching,
             engine,actions,daemon,scan,service,completion,explain,_scm}.py
src/udevmon/sources/{__init__,udev,udisks}.py
tests/…   (unittest)
```

`completion.py` holds the shell completion (§8.2); `explain.py` the report of §8.4.
`matching.py` holds device matching, tag rules, strength and the resolver
(§5.5); `config.py` the loader, validation, warnings and notes.

### 13.1 Versioning

The version is derived from git and formatted like setuptools-scm's defaults
(`guess-next-dev` and `node-and-date`), without depending on it:

- on a clean tag: the tag (`v0.2.0` gives `0.2.0`);
- after a tag: the last number is bumped and `.devN` added (`0.2.1.dev3`); with no
  tag at all: `0.1.devN`, N being the number of commits;
- unless exactly a clean tag: `+g<9 hex digits>` (setuptools-scm keeps the
  first 10 characters of `g<hash>`), and `.d<YYYYMMDD>` (the build date) when
  tracked files have uncommitted changes.

`src/udevmon/_scm.py` (standard library only) implements it. `setup.py` uses it
for the package metadata and writes `udevmon/_version.py` into built packages.
`udevmon.__version__` reads that file, or asks git in a source checkout. Without
git it falls back to `PKG-INFO` (an sdist), else `0.0.0+unknown`; it never
raises. git is needed only when building from a checkout.

Tests build throwaway repositories for each case above. The outputs were also
compared with the real setuptools-scm 10.2.3 on 19 repository states and were
identical.

Automated tests cover: editing hand-formatted files (sub-tables, multi-line values,
commented headers, CRLF, rollback, a missing `tomlkit`), auto-reload and the lock file, the missing-program check,
`explain` and the engine's `plan()`, the docs' renderability (balanced code fences,
code lines that fit an 80-column glow), service and completion uninstall (never touching the real
systemd), shell completion (engine, helper protocol, the real
bash function, and a guard that fails when a command, positional argument or
value option of the CLI is not covered by the completion tables), config parsing/validation (including the error, warning
and note rules of §6), matching and strength, tag rules and the resolver, the
named/unnamed gate and `allow_unnamed`, placeholder expansion and safety, action
runner (incl. timeout), debounce, config edit/rollback (including tag edits), the
udev property normalizer and the udisks object tracker state machine (fed
synthetic signals). Live behaviour (real plug / unplug / mount) is verified
manually.
