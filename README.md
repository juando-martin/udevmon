# udevmon

Run your own commands when a USB device is plugged in or removed, or when a
removable filesystem is mounted or unmounted — configured entirely as a normal
user, with no root privileges.

- **Name your devices** with a *scan mode*: plug something in, give it a name.
- **Attach actions** to those names: "when `backup-disk` is mounted, run my backup script".
- **Identify devices reliably** by serial number or filesystem UUID, not just
  by "some USB stick".
- **Tiny footprint**: one event-driven background process (about 20–35 MB of
  RAM, no polling, no CPU when idle).

## How it works

udevmon listens to two sources, both available to unprivileged users:

- **udev** (device connect/disconnect), through the kernel's udev event stream.
- **udisks2** (filesystem mount/unmount), through D-Bus. Most desktops already
  run it, and it is what automounts your USB sticks.

Concepts:

| Term | Meaning |
|---|---|
| **device** | A name you give to a physical USB device or a filesystem, plus the properties that identify it (e.g. `fs_uuid = "3F2A-91BC"`). Devices are what you attach actions to. |
| **event** | Something that happened to a device: `connected`, `disconnected`, `mounted`, `unmounted`, `startup`, `shutdown`. |
| **action** | A command to run when a given event happens to a given device. |

## Requirements

- Linux with systemd and udisks2 (standard on GNOME/KDE/Cinnamon desktops)
- Python 3.11 or newer
- Python packages `pyudev` and `PyGObject`. On Debian/Ubuntu/Mint these are
  the system packages `python3-pyudev` and `python3-gi`, which are usually
  already installed.

## Installation

From the project directory, install for your user only (no root needed):

```
pip install --user --break-system-packages .
```

`--break-system-packages` is required on Debian 12+/Ubuntu 23.04+ because of
PEP 668. It is safe here: `--user` installs into `~/.local` and does not touch
system files. If you'd rather use a virtual environment, install
`python3-venv` first (needs root) and use
`python3 -m venv --system-site-packages ~/.venvs/udevmon`
(the flag keeps the system `gi` and `pyudev` visible), then
`~/.venvs/udevmon/bin/pip install .`.

Make sure `~/.local/bin` is on your `PATH` and check:

```
udevmon --version
```

Without installing, you can also run it straight from the source tree:
`PYTHONPATH=src python3 -m udevmon ...`.

To uninstall: `pip uninstall udevmon` (plus removing
`~/.config/udevmon/` if you no longer want your configuration).

## Quick start

**1. Scan for a device and name it**

```
udevmon scan
```

Plug in a USB stick (or unplug one, or mount/unmount a filesystem). udevmon
prints what it sees, then after a short pause asks you what to do:

```
Detected:
  [1] USB SanDisk Cruzer [0781:5567]
  [2] Filesystem BACKUP (ext4) on /dev/sdb1 at /media/me/BACKUP
Number to name, Enter to keep scanning, q to quit: 2
Device name: backup-disk
Match on [fs_uuid] (Enter to accept):
Match strength: strong — identifies this exact device
Saved device 'backup-disk' to /home/me/.config/udevmon/config.toml
```

A physical stick usually shows up twice: as a **USB device** (identified by
vendor/product ID and serial number) and as one or more **filesystems**
(identified by UUID). Name whichever you want to react to. Name the USB
device to react to it being plugged in at all; name the filesystem to react
to it being mounted and unmounted.

To also offer devices that are *already* plugged in, use `udevmon scan --present`.

**2. Attach an action**

```
udevmon actions add backup-disk mounted -- notify-send "Backup disk ready" "{mountpoint}"
udevmon actions add backup-disk mounted --name run-backup --timeout 600 -- ~/bin/backup.sh "{mountpoint}"
```

The format is `actions add DEVICE EVENT[,EVENT...] [--name NAME] [--timeout SECONDS] -- COMMAND [ARGS...]`.
Options go **before** the `--`; everything after it is the command, verbatim.

**3. Run the daemon**

Try it in a terminal first:

```
udevmon -v run
```

Plug and unplug the device and watch the log. Use `udevmon run --dry-run` to
log what *would* run without running anything.

**4. Run it in the background**

```
udevmon service install --now
```

This writes `~/.config/systemd/user/udevmon.service`, enables it, and starts
it. From then on it starts with your login session. Useful commands:

```
systemctl --user status udevmon
journalctl --user -u udevmon -f        # follow the log (including action output)
udevmon reload                         # re-read the config after editing it
systemctl --user restart udevmon
```

To only write the unit without enabling it, leave out `--now`.

## Events

| Event | Fires when | Available details |
|---|---|---|
| `connected` | a USB device is plugged in | USB properties |
| `disconnected` | a USB device is unplugged | USB properties |
| `mounted` | a filesystem is mounted | filesystem properties, `mountpoint` |
| `unmounted` | a filesystem is unmounted, or its device disappears while mounted | filesystem properties, `mountpoint` |
| `startup` | the device was **already present** when udevmon started | USB or filesystem properties |
| `shutdown` | the device is **still present** when udevmon exits | USB or filesystem properties |

Notes:

- Unplugging a mounted stick without unmounting first produces both
  `unmounted` (for its filesystem) and `disconnected` (for the USB device).
- Events for the same device and kind that repeat within one second are
  dropped (`debounce`, configurable).
- `shutdown` actions run when udevmon receives a normal stop signal
  (`systemctl --user stop`, Ctrl+C, logout). The daemon waits for them to
  finish, up to each action's `timeout`. They do **not** run if udevmon is
  killed hard (`kill -9`), crashes, or the machine loses power.
- `startup` fires for *every* matching present device each time the daemon
  starts, including after `systemctl --user restart`. A configuration reload
  (`udevmon reload`) does not trigger it.

## Properties you can match on and use in commands

USB devices:

| Property | Example | Notes |
|---|---|---|
| `vendor_id`, `product_id` | `0781`, `5567` | lowercase hex |
| `serial` | `4C530001230815112433` | best identifier, when the device has one |
| `vendor`, `model` | `SanDisk`, `Cruzer Blade` | human-readable |
| `port_path` | `3-1.2` | the physical port/hub position |
| `devnode`, `syspath` | `/dev/bus/usb/003/007` | |
| `udev_<name>` | `udev_id_input_keyboard` | every raw udev property, lowercased |

Filesystems:

| Property | Example | Notes |
|---|---|---|
| `fs_uuid` | `3F2A-91BC` | best identifier |
| `part_uuid` | `2b5c...` | partition UUID (GPT disks) |
| `fs_label`, `fs_type` | `BACKUP`, `ext4` | labels are not unique |
| `devnode` | `/dev/sdb1` | |
| `mountpoint` | `/media/me/BACKUP` | only on mounted/unmounted (and mounted startup/shutdown) events |
| `drive_vendor`, `drive_model`, `drive_serial` | | of the physical drive holding it |

`udevmon present` lists everything currently present with these values, and
how strongly each could be matched.

### Match strength

When you create a device, udevmon tells you how precisely it identifies things:

- **strong**: includes a serial number or a UUID. Identifies exactly this
  device.
- **medium**: model plus physical port (`vendor_id` + `product_id` +
  `port_path`). Matches *any* device of that model in *that port*. This is
  what you get for devices without a serial number.
- **weak**: anything else, for example a filesystem label or just the model.
  Matches every device with those values.

Some cheap devices report fake serials (`0000000`, `A1B2C3D4E590`, ...). udevmon
treats those as weak. Prefer strong matches for anything that runs a script.

## Configuration file

The file is `~/.config/udevmon/config.toml` (respects `$XDG_CONFIG_HOME`). Use
`--config PATH` to use a different one; note that `--config` goes *before* the
subcommand: `udevmon --config my.toml run`.

You can edit it by hand or through the CLI. CLI edits keep your comments and
formatting, and save the previous version as `config.toml.bak`. After
editing by hand, run `udevmon check`, then `udevmon reload`.

```toml
[daemon]                       # every setting here is optional
debounce = 1.0                 # seconds; repeated (device, event) pairs are ignored
max_workers = 4                # actions from different events can run in parallel
udev_filters = ["usb/usb_device"]

[devices.backup-disk]
description = "Weekly backup drive"
match = { fs_uuid = "3F2A-91BC" }

[devices.yubikey]
match = { vendor_id = "1050", product_id = "0407", serial = "A1B2C3D4E5" }

[[actions]]
name = "run-backup"            # optional
device = "backup-disk"
on = ["mounted"]               # one event or a list
run = ["~/bin/backup.sh", "{mountpoint}"]
timeout = 600                  # seconds before the action is killed (default 60)

[[actions]]
device = "yubikey"
on = ["connected", "disconnected"]
run = ["notify-send", "YubiKey {event}"]
```

### How commands are run

- `run` is an argument list, or a string split shell-style. It is
  **never** passed to a shell: no pipes, globbing or `$VARIABLE` expansion in
  the command itself. If you need a shell, ask for one explicitly:
  `run = ["sh", "-c", "cd \"$1\" && ls | wc -l >> ~/count.log", "sh", "{mountpoint}"]`
- A leading `~` in an argument is expanded to your home directory.
- `{name}` is replaced by a property value: `{mountpoint}`, `{serial}`,
  `{fs_uuid}`, `{devnode}`, and so on, plus `{event}` and `{device}`. Values
  are always passed as a single argument, so spaces or shell characters in a
  mount point can't break out of the command.
- Only `{lowercase_names}` are placeholders. Other braces (`${HOME}`,
  `awk '{print $1}'`) are left alone. Write `{{` or `}}` for a literal brace.
- If a placeholder isn't available for the event (for example `{mountpoint}`
  on a `connected` event), `udevmon check` reports it. If it turns out to be
  missing at run time, the action is **skipped** with an error rather than
  run with an empty value.
- The same information is available as environment variables:
  `UDEVMON_EVENT`, `UDEVMON_DEVICE`, and `UDEVMON_<PROPERTY>` (for example
  `UDEVMON_MOUNTPOINT`, `UDEVMON_SERIAL`).
- Commands run as you, with your home directory as working directory and no
  standard input. Their output goes to the log.
- Several actions for one event run one after another, in file order.
  A failing action does not stop the next.
- An action that runs longer than its `timeout` is terminated (along with any
  child processes it started).

## Command reference

```
udevmon scan [--present] [--no-prompt]   watch events and name devices
udevmon present [--all]                  list USB devices and filesystems that are present now
udevmon devices list | show NAME | add NAME --match KEY=VALUE ... | remove NAME
udevmon actions list | add ... | remove NAME
udevmon check                            validate the config
udevmon run [--dry-run]                  run the daemon in the foreground
udevmon reload                           tell the running daemon to reload its config
udevmon service install [--now]          install (and optionally start) the systemd user service
```

Global options, placed before the subcommand: `--config PATH`, `-v` (debug logging).

Manual device creation, without scanning:

```
udevmon devices add my-key --match vendor_id=1050 --match product_id=0407 --match serial=A1B2C3D4E5
```

A device can't be removed while actions still use it; remove those first.
Actions created by hand without a `name` can't be removed through the CLI
(edit the file instead).

## Examples

Notify when a specific USB stick is plugged in or removed:

```
udevmon actions add my-stick connected,disconnected -- notify-send "USB stick {event}"
```

Run a backup when the backup drive is mounted, and safely unmount afterwards:

```toml
[[actions]]
name = "backup"
device = "backup-disk"
on = ["mounted"]
run = ["sh", "-c", "rsync -a ~/Documents/ \"$1/Documents/\" && udisksctl unmount -b \"$2\"",
       "sh", "{mountpoint}", "{devnode}"]
timeout = 3600
```

Lock the screen when a security key is removed:

```
udevmon actions add my-key disconnected -- loginctl lock-session
```

Do something with a drive that is already connected when you log in *and*
when it is plugged in later:

```
udevmon actions add backup-disk startup,mounted -- ~/bin/sync-check.sh
```

## Security notes

Running commands automatically when hardware appears is convenient and also a
classic attack vector ("BadUSB"). udevmon helps by:

- warning about weak matches when you create a device;
- preferring serial numbers and UUIDs (a plain vendor/product ID can be
  imitated by anyone);
- never running commands through a shell and never splicing device values
  into a command line;
- refusing to load a config file that is not owned by you or is writable by
  group/others, since that file decides what gets executed.

Keep scripts triggered by removable devices short, and don't run actions that
trust the *contents* of the device without care.

## Troubleshooting

- **Nothing happens when I plug the device in.** Run `udevmon -v run` in a
  terminal and plug it in. Look for `no device matched`, which means the
  properties differ from what you saved. Compare with `udevmon present` or
  `udevmon scan`. Remember that a stick appears as both a USB device and a
  filesystem, and a device matches only one of those.
- **Filesystem events never fire.** Check that udisks2 is running
  (`systemctl status udisks2`) and that the drive is mounted through it (for
  example by your desktop's automounter or `udisksctl mount`). Mounts done
  with `sudo mount` do not go through udisks2 and are not seen.
- **`notify-send` or other graphical commands don't work from the service.**
  A systemd user service only sees the graphical session's environment if the
  desktop has imported it. If it doesn't, run
  `systemctl --user import-environment DISPLAY WAYLAND_DISPLAY XAUTHORITY DBUS_SESSION_BUS_ADDRESS`
  from your session and restart the service. (Whether this is needed depends
  on the desktop; this has not been verified on every desktop.)
- **"refusing to load".** The config file is group- or world-writable or owned
  by someone else. Fix it with `chmod go-w ~/.config/udevmon/config.toml`.
- **"udevmon is already running".** Only one daemon runs per user. Stop the
  service (`systemctl --user stop udevmon`) before running it in a terminal.
- **The command isn't found.** Services don't always see the same `PATH` as
  your shell. Use full paths in `run`.

## Limitations

- Events are seen only while your user session is running and udevmon is
  started; there is no replay of missed events (apart from `startup`).
- Only USB devices and udisks2 filesystems are supported out of the box. The
  `udev_filters` setting in `[daemon]` (for example `["usb/usb_device", "input"]`) lets
  the daemon watch other kinds of udev devices, matched through the
  `udev_<name>` properties, but this is experimental and only the default
  USB filter has been tested.
- No GUI or TUI yet; configuration is the TOML file plus the CLI.

## Development

```
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

`SPECIFICATIONS.md` describes the design, data model and behaviour in detail.
