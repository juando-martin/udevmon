# udevmon — Specifications

Run user-defined actions when USB devices are connected/disconnected or when
removable filesystems are mounted/unmounted, configured entirely by an
unprivileged user.

## 1. Goals

- Detect USB connect/disconnect and filesystem mount/unmount **without root**.
- Let the user give a device or filesystem a **name** (an *asset*) by running
  the tool in a *scan mode*, then attach *actions* to that name.
- Match assets on **stable identifiers** (serial number, filesystem UUID,
  partition UUID) whenever available; clearly flag weaker matches.
- Run continuously with negligible resource use (event-driven, no polling).
- Configuration is a hand-editable **TOML** file, plus a **CLI** for common edits.
- Leave room to extend to other event sources and device types, and to add a
  TUI/GUI later.

## 2. Non-goals (v1)

- No TUI/GUI (the config format and CLI are designed so one can be added).
- No root component, no udev rules installed (optional later, see §12).
- No replay of devices already present when the daemon starts.
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
- The **engine** matches events against assets, debounces, and hands matching
  actions to the runner.
- The same sources feed `scan`, `devices` and the daemon.

### 4.1 Data model

`Device`: `source` (`"udev"`/`"udisks"`), `key` (stable id while present:
sysfs path or udisks object path), `props` (`dict[str, str]`).

`Event`: `kind`, `device`, `time`.

Event kinds (extensible registry):

| kind           | source | meaning                                           |
|----------------|--------|---------------------------------------------------|
| `connected`    | udev   | a matching udev device was added                  |
| `disconnected` | udev   | it was removed (props come from a cache)          |
| `mounted`      | udisks | a filesystem gained a mount point                 |
| `unmounted`    | udisks | it lost its mount point, or its device vanished   |

Removal events carry the properties cached when the device appeared, so
serial/UUID matching works on unplug. Devices present at daemon start are
used only to prime caches; they fire nothing.

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

Every event also provides `event` (the kind) and `asset` (matched asset name)
for actions.

## 5. Assets and matching

An asset is a name plus a `match` table of `key = "value"` pairs. An event
matches an asset when **every** pair equals the corresponding event property
(case-insensitive). A missing property never matches.

### 5.1 Match strength

Reported by `scan`, `devices`, `assets list` and warned about on creation:

- **strong** — includes an identity-grade key: `serial`, `fs_uuid`,
  `part_uuid`, `drive_serial`, and the serial is not suspicious.
- **medium** — model plus physical location: `vendor_id` + `product_id` +
  `port_path`. Matches any such device in that port.
- **weak** — anything else (e.g. only `vendor_id`+`product_id`, or a label).
  Matches every device of that model / any filesystem with that label.

A serial is *suspicious* (downgraded to weak) when it is empty, all one
repeated character, or a well-known placeholder (`0123456789ABCDEF`,
`1234567890`, …). Cheap sticks often share such serials.

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
debounce = 1.0                # seconds; repeats of (asset, kind) are dropped
udev_filters = ["usb/usb_device"]   # "subsystem" or "subsystem/devtype"
max_workers = 4

[assets.backup-disk]
description = "Weekly backup drive"
match = { fs_uuid = "3F2A-91BC" }

[assets.yubikey]
match = { vendor_id = "1050", product_id = "0407", serial = "12345678" }

[[actions]]
name = "run-backup"           # optional; generated if missing
asset = "backup-disk"
on = ["mounted"]              # string or list
run = ["~/bin/backup.sh", "{mountpoint}"]   # argv list, or a string (shlex-split)
timeout = 300                 # seconds, default 60

[[actions]]
asset = "yubikey"
on = ["connected", "disconnected"]
run = ["notify-send", "YubiKey {event}"]
```

Rules:

- Names match `[A-Za-z0-9_.-]+` and are unique.
- Every action references an existing asset; `on` values must be known kinds.
- `run` is **never passed through a shell**. Use `["sh", "-c", "…"]` explicitly
  if a shell is wanted. A leading `~` in each argument is expanded.
- Placeholders are `{name}` (`{{`/`}}` escape braces). Only simple names are
  allowed. `check` verifies each placeholder exists for the action's event kinds.
  At run time, an action whose placeholder is missing is **skipped with an
  error**, never run with an empty value (protects against e.g. `rm {mountpoint}/x`).
- Actions run with environment `UDEVMON_EVENT`, `UDEVMON_ASSET`, and
  `UDEVMON_<KEY>` for every property, cwd = `$HOME`, stdin = `/dev/null`.
- Because actions execute commands, the daemon **refuses to load** a config
  that is not owned by the user or is group/world-writable.
- The CLI edits the file by appending/removing whole blocks so comments and
  formatting elsewhere are preserved; a change that fails re-parsing is
  rolled back. A `config.toml.bak` is written before each edit.

## 7. Daemon (`udevmon run`)

- Single instance (`flock` on `$XDG_RUNTIME_DIR/udevmon.lock`, which also
  stores the PID).
- Starts sources, primes caches silently, then loops.
- `SIGHUP`: re-read config; on error keep the old config and log. Changes to
  `udev_filters` need a restart (logged).
- `SIGTERM`/`SIGINT`: stop cleanly.
- For each event: match all assets → for each matched asset the actions whose
  `on` includes the kind → debounce per `(asset, kind)` → one job per event
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
udevmon scan [--no-prompt]        scan mode (below)
udevmon devices                   list present USB devices and mounted filesystems,
                                  with match strength and known asset names
udevmon assets list|show|add|remove
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
   list, noting any that already belong to a named asset.
4. Prompt: choose a number → enter a name → confirm or edit the match keys
   (default per §5.2, weak/medium warnings shown) → asset appended to the
   config. Repeat, Enter to continue scanning, `q` to quit.
5. `--no-prompt` (also automatic when stdin is not a TTY) only prints events.

Scan and the daemon are independent listeners and can run at the same time.

### 8.2 Service

`service install` writes `~/.config/systemd/user/udevmon.service`
(`Type=simple`, `Restart=on-failure`, `ExecStart=<this program> run`,
`ExecReload=… reload`, `MemoryMax=100M`), then `systemctl --user daemon-reload`;
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
