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
- The control socket (§8.6) carries `trigger` only; reload is still by `SIGHUP`
  and pause by a flag file.
- No Bluetooth / network / display sources yet (extension points exist, see §11).
- No backwards compatibility with earlier config spellings while the tool is
  preliminary; format changes are made in place.

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
| `removed`      | udisks | a mounted filesystem vanished without an unmount  |
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

**Surprise removal.** udisks reports a clean unmount as a change of the filesystem's
mount points, and a vanished device as the removal of its interfaces. The tracker
keeps them apart: a clean unmount fires `unmounted` with `surprise = "0"`; a
filesystem that disappears while mounted fires `unmounted` (as before, so actions
written for it keep working) and then `removed`, both with `surprise = "1"` and the
properties the filesystem had (including `mountpoint`). Nothing fires for a
filesystem that was not mounted, nor a second time. The USB device's own
`disconnected` comes separately from udev, in either order.

### 4.2 Normalized properties

Match keys and placeholders use these names.

USB devices (udev, `usb/usb_device`):

- `vendor_id`, `product_id` — `ID_VENDOR_ID`, `ID_MODEL_ID` (lowercase hex)
- `serial` — `ID_SERIAL_SHORT`
- `vendor`, `model` — `ID_VENDOR_FROM_DATABASE`/`ID_VENDOR`, `ID_MODEL_FROM_DATABASE`/`ID_MODEL`
- `port_path` — sysfs device name, e.g. `2-1.3` (physical port)
- `devnode`, `syspath`, `subsystem`, `devtype` — as reported
- `udev_<prop>` — every raw udev property, lowercased (e.g. `udev_id_input_keyboard`) — an escape hatch for device classes with no normalized keys. As a placeholder
`{udev_<prop>}` is accepted for every event (whether the thing has it is known only at
run time: a missing one skips the action, like any unavailable placeholder); it is
hardware-supplied, so it is not trusted for the `allow_unnamed` warning (§6.2)

Filesystems (udisks):

- `fs_uuid`, `fs_label`, `fs_type` — `Block.IdUUID/IdLabel/IdType`
- `part_uuid` — `Partition.UUID` (GPT)
- `devnode` — `Block.Device`
- `mountpoint` — first entry of `Filesystem.MountPoints` (mount/unmount/removed events)
- `surprise` — `"1"` when the filesystem vanished while mounted, `"0"` after a clean
  unmount; only on `unmounted` and `removed` events
- `drive_vendor`, `drive_model`, `drive_serial` — `Drive` of the block device
- `instance` — one value per mount (`fs-<start>-<n>`, new for each mount, kept from
  `mounted` or `startup` to `unmounted`/`removed`/`shutdown`)

USB devices (and other udev devices) also have `instance` (`<subsystem>-<usec>` from
`USEC_INITIALIZED`, else `<subsystem>-<hash of syspath>-<n>`), fixed by the source at
first sight and carried by the later `disconnected` (§7.5).

Added by the engine for actions (not present on the endpoint, so not matchable):

- `event` — the event kind
- `device` — name of the named device that applies; **absent** when the thing is unnamed
- `tag` — for tag-targeted actions, the first of the action's tags the thing has

**Volatile keys** change from plug to plug or event to event and are rejected in
tag rules (§5.4): `mountpoint`, `devnode`, `syspath`, `event`, `device`, `tag`,
`surprise`, `instance`, and the raw properties `udev_devname`, `udev_devpath`, `udev_busnum`,
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
values only (no lists or wildcards; a `*` or `?` is an ordinary character there and
`check` warns about it): a device identifies one thing.

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

- USB: `vendor_id`, `product_id`, `serial` if a good serial exists (a placeholder
  serial does not count). Otherwise nothing identifies the unit and **nothing is
  pre-selected**: `scan` offers the choices of §5.3.1 and the user picks one.
- Filesystem: `fs_uuid`; else `part_uuid`; else `drive_serial`+`fs_label`;
  else `fs_label` (weak, flagged).

#### 5.3.1 USB devices without a serial number

`matching.lacks_identity()` is true for a USB device with no serial, or only a
placeholder one (§5.2). For it `scan`, when naming, prints that nothing identifies the
particular unit and offers, without a default:

1. *this model, in this port only*: `vendor_id`, `product_id`, `port_path`
   (offered only when `port_path` is known);
2. *this model, in any port*: `vendor_id`, `product_id`.

Or a typed list of properties. Enter alone skips. The result is medium (1) or weak (2), so
the usual "Save anyway? [y/N]" follows. The event line printed while scanning says there
is no serial number instead of suggesting keys. `present` and the scan list label such
a device `[medium: no serial, matches the port]` (`matching.level_label()`): the level
alone would suggest an identity that a port does not give.

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

**Wildcards.** In a rule (only), `*` matches any run of characters (including none)
and `?` exactly one; the whole value must match, case-insensitively, and every other
character is literal. A value may be a list mixing patterns and plain values.

A rule's strength is computed as in §5.2, except that a key with a list value or a
wildcard pattern never counts as identity-grade or as location (either can match
many things).

Tag names use the device-name character set and live in their own namespace
(a tag and a device may share a name).

**Opting out.** A device may list rule tags in `exclude_tags` (a name or a non-empty
list of names). During resolution (§5.5) a rule tag that would select the thing is
left out, and reported as *excluded*, when **any** device matching the thing lists
it. Only rule selection is affected: a tag carried explicitly by a matching device
is always kept, and a device listing the same tag in `tags` and `exclude_tags` is
an error. The device stays named. `check` notes an excluded tag that has no rule.

**All of.** An action's `tag_mode` is `"any"` (the default: one of its tags is
enough) or `"all"` (the thing needs every tag it lists, whether explicit or from a
rule). It is valid only with `tag`. `{tag}` and `UDEVMON_TAG` are then the first
tag in the list.

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

### 5.8 Variables

A device may have `vars`, a table of `name = "value"` (names: lowercase letters,
digits and `_`, not starting with a digit; integer values become text). A command
may use `{var_NAME}`, and the environment gets `UDEVMON_VAR_NAME`, with the values of
the device the job runs for: the listed device that applies for a `device` action,
the first matching named device for a `tag` action. A thing that matches no device
has none, so `{var_NAME}` is unavailable and the action is skipped (logged like any
missing placeholder). Loading the configuration fails when a command uses
`{var_NAME}` and no device defines `NAME`; `check` warns for each device the action
can reach (its own devices, or the devices that carry its tags explicitly) that does
not, and for an `allow_unnamed` action that uses a variable. Variables are written
by the user, so they are counted as trusted placeholders for that warning (§6.2).

## 6. Configuration

Path: `$XDG_CONFIG_HOME/udevmon/config.toml` (default `~/.config/udevmon/`),
overridable with `--config`. Unknown keys are errors (catches typos).

```toml
[daemon]                      # all optional
debounce = 1.0                # ignore repeats within N seconds
udev_filters = ["usb/usb_device"]   # "subsystem" or "subsystem/devtype"
max_workers = 4
script_dir = ["~/.local/udev/scripts"]   # bare command names (§7.1)

[devices.backup-disk]
description = "Weekly backup drive"
match = { fs_uuid = "3F2A-91BC" }
tags = ["backup", "storage"]           # explicit tags (optional)
# status = "disabled"   # or "ignored"; absent = active (§5.7)
# exclude_tags = ["storage"]   # opt out of a rule tag (§5.4)
# vars = { dest = "/srv/backups" }   # {var_dest} in commands (§5.8)

[devices.yubikey]
match = { vendor_id = "1050", product_id = "0407", serial = "A1B2C3D4E5" }

[devices.card-reader]
match = { vendor_id = "2ce3", product_id = "9563", port_path = "3-3" }

[tags.storage]                         # rule tag: selected by properties
description = "Filesystems I care about"
match = { fs_type = ["ext4", "exfat"] }    # list = any of
# match = { fs_label = "BACKUP*" }   # * and ? are wildcards (§5.4)

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

- Names (devices, tags, actions) match `[A-Za-z0-9_.-]+` (a name with a dot is
  written quoted in a header); device, tag and action
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
- The graphical session's variables (`DISPLAY`, `WAYLAND_DISPLAY`, `XAUTHORITY`,
  `DBUS_SESSION_BUS_ADDRESS`, `XDG_SESSION_TYPE`, `XDG_CURRENT_DESKTOP`,
  `XDG_SESSION_DESKTOP`, `DESKTOP_SESSION`) are looked up for every action
  (`udevmon/session.py`), because a systemd user service gets the manager's
  environment as it was when it started, which may be before the desktop
  imported them, and some desktops never import them. Under the service
  (`INVOCATION_ID` set), or with no display of its own: first the user
  manager's environment now (`systemctl --user show-environment`), whose
  values replace the daemon's under the service; if that has no
  `DISPLAY`/`WAYLAND_DISPLAY`, the variables of the newest process of the
  same user that has one (`/proc/<pid>/environ`), a process of a graphical
  logind session of the user (matched on `XDG_SESSION_ID`) before any other.
  If there is still none but logind lists a graphical session (`Type` x11,
  wayland or mir, `State` active or online), the action waits for one, polling
  both every 0.5 s for up to 30 s (the service starts with the user manager,
  before the desktop has a display); then an X11 session's logind `Display`
  is used alone, when logind has one (GDM on Ubuntu 22.04 leaves it empty, so
  there the processes are the only fallback). From a terminal with a display nothing is looked up. The display in use is logged when it changes;
  none found is a warning.
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
- **`devices set`** replaces the match of a device (an inline table is replaced in
  place, keeping a comment after it; a `[devices.x.match]` sub-table is emptied and
  refilled) and sets or, for an empty text, removes `description`.
- **Renames** (`devices|tags|actions rename OLD NEW`) need care because `tomlkit`
  cannot rename a key in place (popping and re-adding a table moves it to the end).
  So references are changed through `tomlkit` (each `device` or `tag` value of an
  action, whether a string or an array item; the `tags` of each device; an action's
  `name`), which keeps comments and layout, and the table headers (`[devices.OLD]`
  and its sub-tables, or `[tags.OLD]`) are renamed in place by one anchored
  substitution that accepts indentation, spaces, either kind of quotes and a
  comment. The new name is written quoted if it contains a dot. The result must
  equal the original parsed config with only that name changed **and in the same
  order**, or nothing is written; this also catches a header-like line inside a
  multi-line string. A device written with dotted keys (no header of its own), and an
  action without an explicit `name`, are refused with a reason. The new name must be
  valid (§6) and unused in its kind (for tags, undeclared tags count). The report lists
  what was updated, made-up action names that changed (they follow the target), and
  the remaining lines that mention the old name (comments, descriptions, commands),
  which are never edited.
- Every edit is written only if the result re-parses with the strict reader and
  does what was asked; otherwise the file is left byte for byte as it was. A
  `config.toml.bak` is written before each edit. Line endings (LF or CRLF) and a
  missing final newline are preserved. A comment above a removed block stays.

### 6.0 Drop-in files

- Besides the main file `M` (`config.toml`, or `--config NAME.toml`), the files
  `M.d/*.toml` (`config.d/`) are read after it, sorted by file name (byte order).
  Only regular files named `NAME.toml` with `NAME` starting with a letter, digit
  or `_` are read (no hidden files, no `.bak`/`.tmp`). `config.load()` reads them
  all; a missing main file with drop-ins is fine (`must_exist` then holds).
- A drop-in has the main file's schema except `[daemon]` (an error there). The
  `devices`, `tags` and `actions` of all files are merged before validation, so
  references cross files. A device, declared tag or explicitly named action
  defined in two files is an error naming both; errors inside a drop-in are
  prefixed with its label (`config.d/NAME.toml: ...`, and an action's number is
  its position in that file). With only the main file every message is as before.
- Actions keep file order across files: the main file's, then each drop-in's
  (`order`, §7 step 3, sorts first; one file order cannot nest one tool's setup
  and teardown around another's, `order` can). A
  made-up action name (`TARGET-N`) uses its position `N` in that combined list.
- Each file, and the folder when a drop-in exists, passes the ownership and
  permission check (§6).
- `Config.sources` maps (`device` | `tag` | `action`, name) to the file defining
  it; `Config.files` lists the files read. `check` prints them with their counts
  when there are drop-ins; `devices|tags|actions list` add `[config.d/NAME.toml]`,
  `devices|tags show` a `file:` line and `explain` `from config.d/NAME.toml` for
  drop-in entries only.
- Editing (§6): `devices|tags|actions add` write to the main file, or with
  `--file DROPIN` (`NAME`, `NAME.toml`, `config.d/NAME.toml` or its full path;
  nothing else) to that drop-in, created with mode 0600. Every other edit is made
  in the file that defines the entry; `devices disable` and the like may edit
  several files at once. A rename changes the references in every file and the
  header in the defining one. An edit is checked against the whole configuration, and every
  file it wrote is rolled back on failure. Each edited file gets its own `.bak`.

### 6.1 Errors

`check` reports these and exits 1; the daemon refuses to start with them, and on
reload keeps the previous configuration:

- unknown keys; wrong types; invalid names; duplicate names;
- an action with both or neither of `device`/`tag`; a `device` that does not exist;
- `allow_unnamed` on an action that targets `device`;
- a tag rule with an empty list, a non-string value, or a volatile key;
- a placeholder unavailable for the action's event kinds (`{mountpoint}` on
  `connected`; `{tag}` on a `device` action);
- an invalid `timeout`, a `detach` that is not a boolean, or a `timeout` together with
  `detach = true` (§7.2); a `lock` that is not a name (§7.3); `retries` that is not a
whole number of 0 or more, `order` that is not a whole number, `retry_delay` that
is not a number above 0, or either of
them with `detach` (§7.4);
- a device `status` other than `"disabled"`, `"ignored"`, `"active"` or `""`, or a
  non-boolean `enabled` (§5.7);
- `exclude_tags` that is not a name or a non-empty list of names, or that lists a tag
  also in the device's `tags` (§5.4);
- `tag_mode` other than `"any"` or `"all"`, or on an action that targets `device`
  (§5.4);
- `vars` that is not a table, a variable name that is not lowercase letters, digits
  and `_` (not starting with a digit), or a non-text value; a `{var_NAME}` that no
  device defines (§5.8).

### 6.2 Warnings and notes

Warnings are printed by `check` and logged at daemon start and reload; notes are
printed by `check` only. Neither changes the exit status.

- warning: a device with a weak match (as today);
- warning: an action's tag that no device carries and that has no rule (never runs);
- warning: an `allow_unnamed = true` action that uses `{device}` (skipped for
  unnamed things);
- warning: an `allow_unnamed = true` action that uses placeholders derived from
  hardware-supplied values (anything other than `event`, `tag`, `devnode`,
  `syspath`, `port_path`, `subsystem`, `devtype`, `surprise`);
- warning: a `script_dir` entry that is not a folder, and (with `script_dir` set) an
  action whose bare command name is in none of the folders nor the standard system
  paths (computed by `config.command_problems()`, like the next one). An entry that
  does not exist while another entry is a folder is only a note
  (`config.command_notes()`, logged at the normal level by the daemon);
- warning: a device `match` that uses a volatile key;
- warning: a device `match` value containing `*` or `?` (literal there; wildcards
  work in tag rules only, §5.4);
- warning: an action that uses `{var_NAME}` for a device it can reach (a listed
  device, or one carrying its tag explicitly) that does not define `NAME`;
- warning: an `allow_unnamed = true` action that uses a variable (skipped for
  unnamed things, §5.8);
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
- note: a device `match` or a tag rule that uses `port_path` (it only matches in that
  USB port, and any device of the same model in it matches);
- note: `tag_mode = "all"` on an action with a single tag; a tag listed in a device's
  `exclude_tags` that has no rule (nothing to opt out of);
- note: a disabled or ignored device, a disabled action, and an action that only
  targets disabled or ignored devices (so never runs).

## 7. Daemon (`udevmon run`)

### 7.1 Finding a command (`script_dir`)

`[daemon] script_dir` is a string or a list of strings (`~` expanded), empty by default.
When it is set, a command whose `run[0]` is a bare name (no `/`, no placeholder) is
resolved at every event by `Engine.select`: the folders in order, then only
`config.SYSTEM_PATHS` (`/usr/local/sbin`, `/usr/local/bin`, `/usr/sbin`, `/usr/bin`,
`/sbin`, `/bin`); the first *executable file* wins and the job's action gets that full
path (so `explain` shows it). The daemon's `PATH` is not consulted. If nothing is
found the action is skipped with the reason `program 'x' not found in script_dir or
the standard system paths` (logged at WARNING for a real event, listed by `explain`).
When it is not set nothing changes: a bare name is left to `PATH`. Changing
`script_dir` takes a reload; scripts added afterwards need none.

- Single instance (`flock` on `$XDG_RUNTIME_DIR/udevmon.lock`, which also
  stores the PID on the first line and the resolved config path on the second).
- Starts sources, primes caches, emits `startup` events for things already present
  (udisks ones once it attaches), then loops.
- **Pause (§8.5):** for every event, including `startup` and `shutdown`, first
  check whether automation is paused; if so, log it (debug) and stop. The start and
  the end of a pause are logged once each (info); the start-up line ends with
  `[PAUSED]` when paused.
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
  0. Unless automation is paused, **debounce**: an event of the same kind as the
     last event of the same endpoint (the thing, not the device it matches),
     within `debounce` seconds of it, is dropped and logged at info. Every event
     that is not dropped is recorded, whether it has actions or not, so any other
     event of the endpoint in between ends the repeat (a `mounted`, `unmounted`,
     `mounted` within a second is three events). Two endpoints matching one
     device are never repeats of each other.
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
  3. If none is selected, stop. Otherwise sort the selected actions by `order`
     (an integer, default 50, lower first); the sort is stable, so equal ones keep
     config order (main file, then drop-ins, §6.0). Skips are sorted the same way.
  4. One job per event runs its selected actions **sequentially in that order**;
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

### 7.2 Running a command

`actions.run_action` starts the command with `Popen` (never a shell), in its own
session, with stdin closed and stdout and stderr on one pipe. A reader thread logs the
pipe line by line (`[action] line`) until its last writer closes it; it outlives the
command if a program the command left running still holds the pipe, so such a
program can keep writing without a broken pipe.

- **Normal action:** the runner waits for the *process* to exit, with the action's
  `timeout`, and not for the pipe. It lets the reader catch up for at most 0.2 s, then
  logs the exit status (INFO for 0, WARNING otherwise) and returns it. On timeout the
  process group is sent SIGTERM, then SIGKILL after 5 s, and the action returns None.
  The timeout and the kill apply only while the command itself is running.
- **`detach = true`:** the command is started the same way and `run_action` returns
  None at once. A reaper thread logs its exit status when it ends (prefixed
  `detached`). There is no timeout, so a `timeout` key together with `detach` is an
  error (§6.1). A command that cannot start is logged as an error either way.
- The actions of one event run one after another, by `order` then file order
  (§7, step 3); a detached one
  does not hold up the next.
- Everything the actions started belongs to the daemon's service group: stopping the
  systemd service ends it, detached or not.

### 7.3 Locks

An action may name a `lock` (a name). `ActionRunner` keeps one `FifoLock` per name: a
ticket counter and a "now serving" counter under a condition variable. `submit()` is
called on the daemon's main thread, in the order events arrive, and takes one ticket
for every job that has a lock; the worker that later runs the job waits until its
ticket is being served and serves the next one when the job has finished, however it
finished (a crash, or an exit that is not an exception, still serves it, and so do the
tickets of the rest of that event's jobs). So the jobs of one lock run one at a time, in
arrival order; jobs without a lock, and locks with other names, are not affected;
within one event the jobs still run by `order`, then file order.

- **No deadlock:** a job only waits for jobs of earlier events (or earlier in its own
  event, which run before it in the same worker), and the pool starts tasks in arrival
  order, so the oldest unfinished job always has a worker; tickets follow arrival order
  for every lock, so two locks taken in opposite orders by two events cannot cycle.
- **Bounded wait:** every command has a `timeout`. A `detach`ed command releases the
  lock once started; any other releases it when its process has exited (§7.2), whatever
  it left running.
- A waiting job occupies one worker (`max_workers`); locks are for short sections.
- The log says (INFO) when a job had to wait. A `lock` is a name (letters, digits,
  `_ . -`); anything else is an error (§6.1).

### 7.4 Retries

`retries` (a whole number, default 0) and `retry_delay` (seconds, more than 0, default 1)
belong to an action. `ActionRunner._run_with_retries` runs the command up to
`retries + 1` times: after an attempt that returns a non-zero status it waits
`retry_delay` and tries again. `run_action` returns None for a timeout, a failure to
start, a skipped action (unavailable placeholder) and a dry run, and 0 for success, so
none of those is retried. Before each retry the runner checks that automation is not
paused, and it waits on the event that `shutdown()` sets, so a stop interrupts the wait
and ends the retrying. Every failed attempt is logged (WARNING), and after the last one
`gave up after N attempts`. Each attempt has its own timeout; a `lock` (§7.3) is held
across all of them. `retries` or `retry_delay` with `detach` is an error (§6.1).

### 7.4.1 Failure notifications

`notify_on_failure` (boolean, default false; an error with `detach`): when the
action's last attempt (§7.4) exits non-zero, times out or cannot start, the
runner calls `notify.action_failed`, which sends one
`org.freedesktop.Notifications.Notify` (app `udevmon`, icon `dialog-error`,
urgency critical; summary `udevmon: action NAME failed (DEVICE)`, body the exit
status, `timed out after N s` or `could not start: ...`, plus `(after N
attempts)` with retries) on the session bus: `DBUS_SESSION_BUS_ADDRESS` as
`session.update` finds it for actions, else `unix:path=$XDG_RUNTIME_DIR/bus` if
that exists. No bus: nothing is sent (info log); a sending error is a warning
and never affects the runner. Nothing is sent for a skipped action or a dry run.

### 7.5 Instances and state folders

Every event of a thing carries `instance`, chosen by its source when the thing is first
seen and kept until its presence ends (§4.2). A USB device's instance is fixed at the
first `connected` or `startup` and copied onto the `disconnected` even if that event's
own properties would give another value; a filesystem's is new at each mount and shared
by `startup`, `mounted`, `unmounted`, `removed` and `shutdown` of it.

`state.py` keeps one folder per instance, `$XDG_RUNTIME_DIR/udevmon/state/<instance>`
(mode 0700; the instance is reduced to `A-Za-z0-9._-` so it cannot leave the folder).
`ActionRunner` creates it before the first action of an event that has an instance
(never for a dry run) and passes it as `{state_dir}` and `$UDEVMON_STATE_DIR`.
`Engine.handle` passes the instance a presence *ends* with: `disconnected`, `removed`,
and `unmounted` unless `surprise` is `1` (`presence_ends()`). `ActionRunner` removes that
folder when the jobs of the event have finished, even if there are none and even when the
event was debounced or automation is paused. The whole tree is wiped when the daemon
starts (leftovers of a crash) and after it has stopped. A running action of an earlier
event may find the folder gone; a `lock` (§7.3) orders them.

## 8. CLI

```
udevmon run [--dry-run] [-v]            run the daemon
udevmon scan [--no-prompt] [--present] [--all-props]
                                        scan mode (below)
udevmon present [--all] [--all-props] [--json]
                                        list what is present now, with
                                        strength, device name and tags
udevmon devices list|show               (bare `devices` = `devices list`)
udevmon devices add NAME (--match KEY=VALUE ... | --from-mountpoint PATH)
                    [--tag TAG ...] [--exclude-tag TAG ...]
                    [--var NAME=VALUE ...] [--description TEXT]
                    [--file DROPIN]
udevmon devices remove NAME
udevmon devices tag NAME TAG [TAG ...]  add explicit tags to a device
udevmon devices disable|ignore|enable NAME...   (§5.7)
udevmon devices set NAME [--match KEY=VALUE ...] [--description TEXT]
                    [--var NAME=VALUE ...] [--unset-var NAME ...]  (§5.8)
udevmon devices exclude|include NAME TAG...    (§5.4)
udevmon devices|tags|actions rename OLD NEW
udevmon devices untag NAME TAG [TAG ...]
udevmon tags list|show
udevmon tags add NAME [--match KEY=VALUE ...] [--description TEXT]
                     [--file DROPIN]
udevmon tags set NAME [--match KEY=VALUE ... | --no-rule]
                     [--description TEXT]
udevmon tags remove NAME
udevmon actions list
udevmon actions add (--device NAME ... | --tag NAME ...) EVENT[,EVENT ...]
                    [--name NAME] [--timeout SECONDS] [--all-tags]
                    [--detach] [--lock NAME] [--retries N] [--retry-delay S]
                    [--order N] [--notify-on-failure]
                    [--allow-unnamed | --named-only] [--file DROPIN]
                    -- COMMAND [ARG ...]
udevmon actions remove NAME
udevmon actions disable|enable NAME...
udevmon actions set NAME [--allow-unnamed | --named-only
                          | --default-unnamed]
                    [--on EVENTS] [--timeout SECONDS]
                    [--add-device N ...] [--remove-device N ...]
                    [--add-tag N ...] [--remove-tag N ...]
                    [--all-tags | --any-tags] [--detach | --no-detach]
                    [--lock NAME | --no-lock] [--retries N]
                    [--retry-delay S] [--order N]
                    [--notify-on-failure | --no-notify-on-failure]
                    [-- COMMAND ...]
udevmon explain [EVENT] [--device NAME] [--prop KEY=VALUE ...] [--all]
                                        what would run (§8.4)
udevmon pause [--for DURATION]          stop every action (§8.5)
udevmon resume                          let actions run again
udevmon check                           validate: errors, warnings, notes
udevmon reload                          SIGHUP the running daemon
udevmon trigger DEVICE [EVENT] [--wait] run a present device's actions now
                                        (§8.6)
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
- `devices add --from-mountpoint PATH` (instead of `--match`) asks udisks2 for
  the filesystem mounted at `PATH` (any of its mount points, symlinks resolved)
  and writes `match = { fs_uuid = ..., drive_serial = ... }`, with its
  description unless `--description` is given. It refuses, writing nothing, when
  nothing is mounted there, the filesystem has no `fs_uuid`, the drive's serial
  is missing or suspicious (§5.2), or a device already matches the filesystem.
- `present --json` prints a JSON list, one object per thing: `kind` (`usb` or
  `filesystem`), `description`, `props` (all, `udev_*` included), `devices` and
  `tags` (as resolved, §5.5) and `suggested_match` (the keys of §5.2).
- `actions add` appends the new `[[actions]]` table at the end of the file (or
  of the `--file` drop-in); `order` (§7, step 3), not the position, is the way
  to run it before others.
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
   once). For each, in turn:
   1. **Kind**: `d` (a device, one particular trusted thing) or `c` (a
      category: a declared tag with a rule, §5.4). Default `c` for a thing with
      no serial number (§5.3.1), else `d`; `q` skips this thing.
   2. **Name**: the device or tag name. Blank skips this thing.
   3. **Match keys**: as for a device (default per §5.3, or the choices of
      §5.3.1 for a thing with no serial number); the same selection becomes the
      tag's `match` for a category.
   4. **Tags** (device only): explicit tags, comma-separated; existing tags are
      suggested. Skipped for a category (a tag has no tags of its own).
   5. **Confirm**: a summary (name, match/rule with its strength, and tags for
      a device) followed by `[Enter] save   [r] rename   [m] change match(/rule)
      [t] change tags (device only)   [c] cancel`. `r`, `m` and `t` redo that one
      step (Enter there keeps the previous value; `-` for tags clears them) and
      return to this same confirm screen; `c` (also `n`, `no`, `q`) cancels and
      leaves the thing in the list to pick again. Blank saves only when the
      match/rule is strong; otherwise `y` is required (blank prints a reminder
      and asks again). A category has no strength gate: blank always saves. A
      name already in use (device or tag) is reported and the screen is shown
      again so it can be renamed.
   `iN` (`i3`, `i1,3`) skips the kind choice and goes straight to steps 2–3
   (match) and the confirm screen, saving the device as `ignored` and asking
   for no tags (step 4). Repeat, Enter to continue scanning, `q` to quit.

   After saving: the same reload/pause notice as the other editing commands,
   then a suggested `actions add` command — `--device NAME EVENT` for a device,
   `--tag NAME EVENT --allow-unnamed` for a category (§5.6), `mounted` for a
   filesystem and `connected` otherwise. Nothing is printed for an ignored
   device.
5. `--no-prompt` (also automatic when stdin is not a TTY) only prints events.
   `--all-props` (here and on `present`) prints every property of each thing, one per
   line and sorted, including the raw `udev_*` ones, marking those in the volatile set
   (§4.2) with `[changes per plug]`; without it the summary line hides them.
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
  filesystems, plus `removed`; `startup` and `shutdown` for both), the actions that would run
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

### 8.5 Pause

`udevmon pause [--for DURATION]` stops all automation without touching the config;
`udevmon resume` ends it.

- The state is the file `pause.pause_path()`, `$XDG_RUNTIME_DIR/udevmon.paused`
  (beside the lock file; mode 0600, written atomically). It is empty (paused until
  `resume`) or holds the end time in seconds since the epoch (`--for`). The daemon
  reads it for every event: one `open` on a file that usually does not exist. The
  runtime directory is cleared at logout and reboot, so a pause cannot outlive the
  session. A file whose time has passed is ignored and removed; one that is
  unreadable or holds something else counts as paused until resumed.
- `DURATION` is one or more `<number><unit>` groups with units `s`, `m`, `h`, `d`
  (`90s`, `30m`, `1h30m`); anything else, or zero, is an error (exit 1).
- While paused no action runs, including `startup` and `shutdown` ones (the
  daemon's exit pass is held back too). Events keep arriving and are not replayed on
  resume. `Engine.plan()` (and so `explain`) ignores the pause.
- `pause` succeeds even when no daemon is running (and says so): the daemon obeys
  the file once it starts. Pausing again replaces the previous pause.
- `explain` prints `Note: automation is paused (...)`, and the editing commands and
  `scan`, after a successful change, print the same note.

### 8.6 Trigger

- The daemon listens on `$XDG_RUNTIME_DIR/udevmon.sock` (mode 0600, removed on
  stop; a stale one is replaced, the lock file proving no other daemon runs).
  A connection whose peer uid (`SO_PEERCRED`) is not the daemon's is refused.
  One request per connection: one JSON line in, one out. Reading and replying
  happen in a thread; the request is handled on the main loop (`idle_add`),
  so it is ordered with events, and lock tickets are taken there as for events.
  If the socket cannot be opened the daemon logs a warning and runs without it.
- `{"cmd": "trigger", "device": NAME, "event": KIND, "wait": BOOL}`. `KIND` is
  one of `startup` (default), `shutdown`, `connected` (a USB thing), `mounted`
  (a filesystem with a mount point), `unmounted` (a filesystem; `surprise=0`).
  Refused: an unknown device (in the daemon's configuration), an ignored one,
  a pause, no present thing matching the device, none for which `KIND` fits.
- For each present thing that is the device, an event of `KIND` is built from
  its current properties and goes through `Engine.trigger`: resolution and
  selection as for a real event (§5.5, §7), but no debounce (and it does not
  count for it) and no presence end (no state folder removed). It is logged as
  `KIND (udevmon trigger): NAME, N action(s)`, with each skip.
- The reply lists, per thing, the actions submitted and those skipped with the
  reason. With `wait`, it is sent once they finish, with each action's exit
  status (`null`: timed out, not started, skipped or detached).
- CLI: exit 1 when no daemon runs (lock file or socket), when an explicit
  `--config` is not the daemon's, on a refusal, and with `--wait` when a status
  is not 0.

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
- **TUI/GUI** can edit the same TOML via the config-edit module, and talk to the
  daemon over the control socket (§8.6).

**Interface for other programs.** Other programs use udevmon through the CLI
(commands, options, exit statuses), the configuration format with drop-in
files (§6.0), what an action receives (placeholders, `UDEVMON_*` variables,
the state folder; §7.2, §7.5) and `present --json`. These are provisional
while udevmon is 0.x. The Python modules, the control socket protocol and the
files in the runtime folder are internal: there is no public Python API
(`docs/api.rst` keeps the list).

## 12. Optional later work

Nothing here is started. Ordered roughly from small to large.

**Small**

- Completion for `pause --for` durations (`15m 30m 1h 2h 1d`), so TAB shows
  the format. Needs a new option kind in `completion.py`.
- `check` notes:
  - a device whose match is a subset of another's, so the two can never be
    told apart;
  - a `lock` name used by one action only (it then orders only that action's
    own runs: intended, or a typo);
  - an `exclude_tags` entry whose rule can never match the device;
  - two configured devices matching the same thing from different files, where
    `{device}` then depends on file names (§7 step 2).
- `$UDEVMON_SCRIPT_DIR` and `$UDEVMON_STATE_ROOT` in the action environment
  (today a script computes its own path, and `dirname "$UDEVMON_STATE_DIR"`).
- Retry back-off (`retry_delay` growing each attempt); retrying `shutdown`
  actions, which stopping currently ends.
- zsh and fish completion: only a small wrapper per shell around
  `udevmon _complete`.

**Medium**

- `udevmon edit [FILE]`, like `visudo`: open the main file or a drop-in in
  `$EDITOR` on a copy, check it on save as `check` does (across all files),
  offer to edit again or discard on errors, else replace the file (keeping the
  `.bak`) and reload. Covers every key without per-option UI; this replaced the
  interactive menu idea.
- Tag-level default variables that devices override (needs a rule for devices
  in several tags).
- Per-action environment and working directory.
- `pause`, `resume` and `reload` through the control socket instead of a flag
  file and `SIGHUP`; `scan` showing live events through the running daemon, so
  that only one process watches.

**Needs a design decision**

- The lock and the worker pool: a job waiting for its lock occupies a worker
  (accepted, documented). A queue per lock would free it, at the cost of the
  per-event run order (§7) unless the event's worker waits anyway.
- Marking a USB device's `disconnected` as a surprise when one of its
  filesystems was still mounted. Needs a link from the USB device to its
  filesystems: `drive_serial == serial` when both exist, or walking the block
  device's sysfs path up to its USB device (exact, more work).

**Large**

- One-time root udev rule forwarding events to a socket-activated user
  service, for zero idle footprint and device permission tweaks (optional,
  since the design avoids root).
- Other event sources: Bluetooth (BlueZ), power (UPower), network
  (NetworkManager); each a `sources/` module plus new event kinds (§11).
- A `changed` event (udev `change`, `bind`, `unbind`) and watching every
  subsystem; non-USB defaults in `scan`.
- A public API (§11 says there is none). Most reusable: the event layer
  (`events`, `sources/`), USB and mount events without root, which needs a
  blocking `watch()` helper (today `start(emit)` assumes the caller's GLib
  main loop), a declared source protocol and public names; it could become
  its own package. Then reading the configuration (`config.load`,
  `matching.resolve`) and editing it (`configedit`), which kp-sync now
  reimplements or calls as commands. Until then, `--json` for `check`,
  `explain`, `trigger --wait` and `--version` covers what integrations
  parse from text today, and a documented, versioned control-socket
  protocol would serve a GUI.

**Deliberately not planned**

- The old prototype's `environ` globs, `name:filter` INI format and importer,
  and `[abc]` glob classes: everything is exported as `UDEVMON_*`, and `*` and
  `?` cover rules.

## 13. Layout and testing

```
pyproject.toml
setup.py                          version from git (§13.1)
src/udevmon/{__init__,__main__,cli,config,configedit,events,matching,
             engine,actions,daemon,control,notify,session,state,scan,
             service,completion,explain,pause,runtime,_scm}.py
src/udevmon/sources/{__init__,udev,udisks}.py
contrib/rclone-config/            helper run as an action (own package)
docs/                             Sphinx sources of the HTML manual
docs/_ext/{cli_help,module_reference}.py   generated reference pages
tools/docs.py                     build (strict) or preview (127.0.0.1)
tests/…   (unittest)
```

The HTML manual (optional, `docs` extra) is for integrators and maintainers;
README and the man pages remain the user documentation. Its command reference
is generated from `cli.build_parser()`, its module reference from docstrings.
Building it imports the package but reads no config and starts nothing.

`contrib/` holds helpers that udevmon runs as ordinary actions; they are not part
of the udevmon package and import nothing from it; each has its own README and is
installed on its own (`rclone-config` is a separate small package). `rclone-config` publishes the rclone config of a drive tagged for it while
the drive is present.

`completion.py` holds the shell completion (§8.2); `explain.py` the report of §8.4;
`control.py` the control socket (§8.6); `notify.py` the desktop notifications of
`notify_on_failure`; `session.py` the lookup of the graphical session's variables for
actions; `state.py` the per-instance state folders.
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
code lines that fit an 80-column glow), the HTML manual (strict offline build with
every config, source, socket and subprocess entry point made to fail; preview on
127.0.0.1 only), service and completion uninstall (never touching the real
systemd), shell completion (engine, helper protocol, the real
bash function, and a guard that fails when a command, positional argument or
value option of the CLI is not covered by the completion tables), config parsing/validation (including the error, warning
and note rules of §6), matching and strength, tag rules and the resolver, the
named/unnamed gate and `allow_unnamed`, placeholder expansion and safety, action
runner (incl. timeout), debounce, config edit/rollback (including tag edits), the
udev property normalizer and the udisks object tracker state machine (fed
synthetic signals). Live behaviour (real plug / unplug / mount) is verified
manually.
