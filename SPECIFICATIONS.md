# udevmon — Specifications

Run user-defined actions when USB devices are connected/disconnected or when
removable filesystems are mounted/unmounted, configured entirely by an
unprivileged user.

## 1. Goals

- Detect USB connect/disconnect and filesystem mount/unmount **without root**.
- Let the user give a USB device or filesystem a **name** (a *device*) by running
  the tool in a *scan mode*, then attach *actions* to that name.
- Match devices on **stable identifiers** (serial number, filesystem UUID,
  partition UUID) whenever available; clearly flag weaker matches.
- Run continuously with negligible resource use (event-driven, no polling).
- Configuration is a hand-editable **TOML** file, plus a **CLI** for common edits.
- Leave room to extend to other event sources and device types, and to add a
  TUI/GUI later.

## 2. Non-goals (v1)

- No TUI/GUI (the config format and CLI are designed so one can be added).
- No root component, no udev rules installed (optional later, see §12).
- No control socket between CLI and daemon (reload is by `SIGHUP`).
- No Bluetooth / network / display sources yet (extension points exist, see §11).

## 3. Platform and dependencies

- Linux, Python >= 3.11 (uses `tomllib`).
- `pyudev` — udev netlink monitor (readable by unprivileged users).
- PyGObject (`gi`: `GLib`, `Gio`) — main loop and D-Bus client for
  `org.freedesktop.UDisks2` on the system bus. No extra D-Bus library.
- `udisks2` running (already true on automounting desktops).
- Tests use only `unittest`.

## 4. Architecture

```
 sources                  core                         output
 ┌──────────────┐
 │ UdevSource   │─┐
 └──────────────┘ │   Event(kind, device)   ┌─────────┐   ┌──────────────┐
 ┌──────────────┐ ├──────────────────────▶ │ Engine  │──▶│ ActionRunner │
 │ UdisksSource │─┘                         │ match + │   │ (threads,    │
 └──────────────┘                           │debounce │   │  subprocess) │
                                            └─────────┘   └──────────────┘
                                                 ▲
                                          Config (TOML)
```

- One GLib main loop drives every source. Nothing polls.
- A **source** turns raw system notifications into normalized `Event`s.
- The **engine** matches events against devices, debounces, and hands matching
  actions to the runner.
- The same sources feed `scan`, `devices` and the daemon.

### 4.1 Data model

`Endpoint`: a USB device or filesystem as observed by a source (not to be confused with
a user-named *device* in the config): `source` (`"udev"`/`"udisks"`), `key` (stable id while present:
sysfs path or udisks object path), `props` (`dict[str, str]`).

`Event`: `kind`, `endpoint`, `time`.

Event kinds (extensible registry):

| kind           | source | meaning                                           |
|----------------|--------|---------------------------------------------------|
| `connected`    | udev   | a matching udev device was added                  |
| `disconnected` | udev   | it was removed (props come from a cache)          |
| `mounted`      | udisks | a filesystem gained a mount point                 |
| `unmounted`    | udisks | it lost its mount point, or its device vanished   |
| `startup`      | both   | the device was already present when the daemon started |
| `shutdown`     | both   | the device is still present when the daemon exits |

Removal events carry the properties cached when the device appeared, so
serial/UUID matching works on unplug.

`startup` and `shutdown` are the "already there" counterparts: once the sources
are attached, the daemon emits one `startup` event per present USB device and
filesystem (mounted or not, system ones included); on `SIGTERM`/`SIGINT` it emits
one `shutdown` event per device still present, then waits for those actions to
finish before exiting. They are ordinary kinds, so `on = ["startup", "connected"]`
means "whenever this device becomes known to me". Placeholders for these kinds are
the union of USB and filesystem keys; a missing one (e.g. `{mountpoint}` for an
unmounted filesystem) skips that action. `shutdown` actions do **not** run if the
daemon is killed (`SIGKILL`), crashes, or the machine loses power.

### 4.2 Normalized properties

Match keys and placeholders use these names.

USB devices (udev, `usb/usb_device`):

| key | source |
|---|---|
| `vendor_id`, `product_id` | `ID_VENDOR_ID`, `ID_MODEL_ID` (lowercase hex) |
| `serial` | `ID_SERIAL_SHORT` |
| `vendor`, `model` | `ID_VENDOR_FROM_DATABASE`/`ID_VENDOR`, `ID_MODEL_FROM_DATABASE`/`ID_MODEL` |
| `port_path` | sysfs device name, e.g. `2-1.3` (physical port) |
| `devnode`, `syspath`, `subsystem`, `devtype` | as reported |
| `udev_<prop>` | every raw udev property, lowercased (e.g. `udev_id_input_keyboard`) — an escape hatch for device classes with no normalized keys |

Filesystems (udisks):

| key | source |
|---|---|
| `fs_uuid`, `fs_label`, `fs_type` | `Block.IdUUID/IdLabel/IdType` |
| `part_uuid` | `Partition.UUID` (GPT) |
| `devnode` | `Block.Device` |
| `mountpoint` | first entry of `Filesystem.MountPoints` (mount/unmount events) |
| `drive_vendor`, `drive_model`, `drive_serial` | `Drive` of the block device |

Every event also provides `event` (the kind) and `device` (the matched named device)
for actions.

## 5. Devices and matching

A device is a name plus a `match` table of `key = "value"` pairs. An event
matches a device when **every** pair equals the corresponding event property
(case-insensitive). A missing property never matches.

### 5.1 Match strength

Reported by `scan`, `devices`, `devices list` and warned about on creation:

- **strong** — includes an identity-grade key: `serial`, `fs_uuid`,
  `part_uuid`, `drive_serial`, and the serial is not suspicious.
- **medium** — model plus physical location: `vendor_id` + `product_id` +
  `port_path`. Matches any such device in that port.
- **weak** — anything else (e.g. only `vendor_id`+`product_id`, or a label).
  Matches every device of that model / any filesystem with that label.

A serial is *suspicious* (downgraded to weak) when it is empty, all one
repeated character, or a well-known placeholder (`0A1B2C3D4E59ABCDEF`,
`A1B2C3D4E590`, …). Cheap sticks often share such serials.

### 5.2 Default keys chosen by scan

- USB: `vendor_id`, `product_id`, `serial` if a good serial exists; otherwise
  `vendor_id`, `product_id`, `port_path` (medium, flagged).
- Filesystem: `fs_uuid`; else `part_uuid`; else `drive_serial`+`fs_label`;
  else `fs_label` (weak, flagged).

The user can override the keys interactively.

## 6. Configuration

Path: `$XDG_CONFIG_HOME/udevmon/config.toml` (default `~/.config/udevmon/`),
overridable with `--config`. Unknown keys are errors (catches typos).

```toml
[daemon]                      # all optional
debounce = 1.0                # seconds; repeats of (device, kind) are dropped
udev_filters = ["usb/usb_device"]   # "subsystem" or "subsystem/devtype"
max_workers = 4

[devices.backup-disk]
description = "Weekly backup drive"
match = { fs_uuid = "3F2A-91BC" }

[devices.yubikey]
match = { vendor_id = "1050", product_id = "0407", serial = "A1B2C3D4E5" }

[[actions]]
name = "run-backup"           # optional; generated if missing
device = "backup-disk"
on = ["mounted"]              # string or list
run = ["~/bin/backup.sh", "{mountpoint}"]   # argv list, or a string (shlex-split)
timeout = 300                 # seconds, default 60

[[actions]]
device = "yubikey"
on = ["connected", "disconnected"]
run = ["notify-send", "YubiKey {event}"]
```

Rules:

- Names match `[A-Za-z0-9_.-]+` and are unique.
- Every action references an existing device; `on` values must be known kinds.
- `run` is **never passed through a shell**. Use `["sh", "-c", "…"]` explicitly
  if a shell is wanted. A leading `~` in each argument is expanded.
- Placeholders are `{lower_snake_name}`; `{{`/`}}` produce literal braces. Any other
  brace use is left untouched, so shell text such as `${1:-x}`, `${HOME}` or
  `awk '{print $1}'` needs no escaping. `check` verifies each placeholder exists for the action's event kinds.
  At run time, an action whose placeholder is missing is **skipped with an
  error**, never run with an empty value (protects against e.g. `rm {mountpoint}/x`).
- Actions run with environment `UDEVMON_EVENT`, `UDEVMON_DEVICE`, and
  `UDEVMON_<KEY>` for every property, cwd = `$HOME`, stdin = `/dev/null`.
- Because actions execute commands, the daemon **refuses to load** a config
  that is not owned by the user or is group/world-writable.
- The CLI edits the file by appending/removing whole blocks so comments and
  formatting elsewhere are preserved; a change that fails re-parsing is
  rolled back. A `config.toml.bak` is written before each edit.

## 7. Daemon (`udevmon run`)

- Single instance (`flock` on `$XDG_RUNTIME_DIR/udevmon.lock`, which also
  stores the PID).
- Starts sources, primes caches, emits `startup` events for devices already present
  (udisks ones once it attaches), then loops.
- `SIGHUP`: re-read config; on error keep the old config and log. Changes to
  `udev_filters` need a restart (logged).
- `SIGTERM`/`SIGINT`: emit `shutdown` events, wait for those actions (bounded by each
  action's `timeout`), stop cleanly.
- For each event: match all devices → for each matched device the actions whose
  `on` includes the kind → debounce per `(device, kind)` → one job per event
  runs its actions **sequentially in config order**; different events run in
  parallel in a small thread pool.
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
udevmon run [--dry-run] [-v]      run the daemon
udevmon scan [--no-prompt] [--present]
                                  scan mode (below)
udevmon present [--all]           list USB devices and filesystems present now,
                                  with match strength and known device names
udevmon devices list|show|add|remove
udevmon actions list|add|remove
udevmon check                     validate config, report problems, exit 1 on error
udevmon reload                    SIGHUP the running daemon
udevmon service install [--now] [--dest DIR]
                                  write the systemd user unit (and optionally enable)
```

Global: `--config PATH`, `-v`.

### 8.1 Scan mode

1. Print a banner and start the same sources as the daemon.
2. Print each event as it happens (kind, description, properties, strength).
3. After 2 s without new events, present the collected devices as a numbered
   list, noting any that already belong to a named device.
4. Prompt: choose a number → enter a name → confirm or edit the match keys
   (default per §5.2, weak/medium warnings shown) → device appended to the
   config. Repeat, Enter to continue scanning, `q` to quit.
5. `--no-prompt` (also automatic when stdin is not a TTY) only prints events.

6. `--present` also offers devices and filesystems that are **already present** when
   the scan starts (printed as `present`, one line each, and included in the naming
   list). Without it, scan only reports changes. System filesystems are omitted.

Scan and the daemon are independent listeners and can run at the same time.

### 8.2 Service

`service install` writes `~/.config/systemd/user/udevmon.service`
(`Type=simple`, `Restart=on-failure`, `ExecStart=<this program> run`,
`ExecReload=… reload`, `MemoryMax=100M`, `TimeoutStopSec=120` so `shutdown` actions can finish), then `systemctl --user daemon-reload`;
`--now` also runs `enable --now`. Hardening options that would confine
actions (`NoNewPrivileges`, `ProtectSystem`…) are deliberately not set, since
actions are user scripts.

## 9. Resource expectations

Event-driven; idle CPU ≈ 0 with no timers. Resident memory ≈ 20–35 MB.
A few ms of CPU per event. No disk I/O apart from reading the config.

## 10. Security considerations

- Autorun on plug-in is inherently risky: prefer strong matches; weak/medium
  matches are flagged at creation and in `check`.
- No shell interpolation; placeholder values are passed as separate argv
  elements, never concatenated into a command string.
- Config ownership/permission check (§6). Runs only as the invoking user.

## 11. Extensibility

- **Sources** implement `start(emit)`, `stop()`, `snapshot()`; adding
  Bluetooth (BlueZ D-Bus) or NetworkManager means a new module and new event
  kinds, with no change to config format, matching or actions.
- **More udev device classes** need only `udev_filters` plus `udev_<prop>`
  match keys, no code.
- **TUI/GUI** can edit the same TOML via the config-edit module, and could
  later talk to the daemon over a control socket.

## 12. Optional later work

- One-time root udev rule forwarding events to a socket-activated user
  service for zero idle footprint / device permission tweaks.
- Control socket (live scan through the daemon, no SIGHUP).
- Per-action environment/cwd, `enabled = false`, startup replay option.

## 13. Layout and testing

```
pyproject.toml
src/udevmon/{__init__,__main__,cli,config,configedit,events,matching,
             engine,actions,daemon,scan,service}.py
src/udevmon/sources/{__init__,udev,udisks}.py
tests/…   (unittest)
```

Automated tests cover: config parsing/validation, matching and strength,
placeholder expansion and safety, action runner (incl. timeout), debounce,
config edit/rollback, the udev property normalizer and the udisks object
tracker state machine (fed synthetic signals). Live behaviour (real plug /
unplug / mount) is verified manually.
