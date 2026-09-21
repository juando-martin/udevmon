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

- **device**: A name you give to a physical USB device or a filesystem, plus the properties that identify it (e.g. `fs_uuid = "3F2A-91BC"`). Devices are what you attach actions to.
- **event**: Something that happened to a device: `connected`, `disconnected`, `mounted`, `unmounted`, `startup`, `shutdown`.
- **tag**: A label for a group of devices, so one action can cover several of them. A tag can be set on devices by hand, or selected automatically by a rule on properties (see [Tags](#tags-one-action-for-several-devices)).
- **action**: A command to run when a given event happens to a given device (or to any device with a given tag).

## Requirements

- Linux with systemd and udisks2 (standard on GNOME/KDE/Cinnamon desktops)
- Python 3.10 or newer (Ubuntu 22.04 and later, Debian 12 and later)
- Python packages `pyudev` and `PyGObject`. On Debian/Ubuntu/Mint these are
  the system packages `python3-pyudev` and `python3-gi`, which are usually
  already installed.
- On Python 3.10 only: the `tomli` package (system package `python3-tomli`),
  which `pip` pulls in automatically when you install udevmon.
- The `tomlkit` package, version 0.11.4 or newer. Only the commands that
  change something already in your config (`remove`, `tag`, `untag`) use it;
  adding new entries, `check`, `explain` and the daemon don't. `pip` pulls it
  in automatically. The system package `python3-tomlkit` is recent enough on
  Debian 13 and Ubuntu 24.04, but too old on Ubuntu 22.04 (it leaves stray
  lines in the file), where `pip` fetches a current one instead.

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

To remove it again, see [Uninstalling](#uninstalling) below.

### Tab completion (bash)

```
udevmon completion install
```

writes the script to `~/.local/share/bash-completion/completions/udevmon`
(no root needed). Open a new terminal and press TAB. That location is loaded
automatically when the `bash-completion` package is installed, which is the
default on Ubuntu, Debian and Mint. Without it, put this in `~/.bashrc`
instead:

```
source <(udevmon completion bash)
```

It completes commands, sub-commands and options, and it reads your config
for the names that belong in each place:

- device names for `devices show|remove|tag|untag` and `--device`;
- tag names for `devices tag`, `--tag`; on `devices untag` only the tags that
  device really has, and on `tags remove` only declared tag rules;
- action names for `actions remove`;
- event names as a list (`connected,dis` becomes `connected,disconnected`);
- property names for `--match` (rules leave out the ones that change from
  plug to plug), file names for `--config`, folders for `service install
  --dest`.

If you pass `--config PATH`, the names come from that file. A missing,
invalid or unsafe config just means no names, never an error message. Each
TAB runs udevmon once (about 50 ms) and doesn't touch udev or D-Bus. It works
for the `udevmon` command, not for `python3 -m udevmon`.

### Manual pages

`pip install` also installs two manual pages, `udevmon(1)` (every command and
option) and `udevmon.toml(5)` (every key of the configuration file, the events,
the properties and how commands are run):

```
man udevmon
man udevmon.toml
```

For a user install they go to `~/.local/share/man`, which `man` finds when
`~/.local/bin` is on your `PATH`. From a source checkout, read them without
installing: `man -l man/udevmon.1` and `man -l man/udevmon.toml.5`.

### Uninstalling

Remove the extras first, then the package:

```
udevmon service uninstall       # stop the daemon, remove the unit
udevmon completion uninstall    # remove the tab-completion script
pip uninstall udevmon
```

- `service uninstall` runs `systemctl --user disable --now`, so the daemon
  stops cleanly (its `shutdown` actions run), then deletes the unit file and
  reloads systemd.
- Both commands only delete files udevmon wrote itself. A `udevmon.service`
  or completion file that doesn't look like udevmon's is left alone and
  reported, and both commands are safe to run again.
- With `--dest` they only remove that file and never call `systemctl`,
  mirroring `install --dest`.
- Your configuration in `~/.config/udevmon/` (and its `.bak`) is never
  touched. Delete it yourself if you no longer want it.
- `pip uninstall` alone leaves the unit file and the completion script
  behind, which is why they come first.

### The version number

`udevmon --version` prints a version taken from git, in the same format as
[setuptools-scm](https://setuptools-scm.readthedocs.io/), for example
`0.1.dev6+gebbca8fea.d20260920`:

- `0.1.dev6`: 6 commits and no release tag yet. Once you tag a commit (say
  `git tag v0.2.0`), that commit reads `0.2.0`, and later commits read
  `0.2.1.dev3` (3 commits after the tag).
- `+gebbca8fea`: the commit the build came from (`g` plus 9 hex digits).
- `.d20260920`: tracked files had uncommitted changes; the date is the
  build date. Untracked files don't count.

From a source checkout the version is asked from git each time. A copy that
was installed carries the version of the commit it was built from. Without
git the version comes from the source tarball's metadata, else it reads
`0.0.0+unknown`; the install never fails because of it.

### Copying it to another machine

Build a wheel once, where the source is:

```
pip wheel . --no-deps -w dist/
```

then copy the `.whl` file to the other machine and install it there. That
machine needs neither the source nor git:

```
pip install --user --break-system-packages udevmon-*.whl
```

Add `--no-deps` if it has no network access and already has `python3-pyudev`
and `python3-gi` (and, on Python 3.10, `python3-tomli`; and a recent enough
`python3-tomlkit`) installed.

## Quick start

**1. Scan for a device and name it**

```
udevmon scan
```

Plug in a USB stick (or unplug one, or mount/unmount a filesystem). udevmon
prints what it sees, then after a short pause asks you what to do:

```
Detected:
  [1] USB SanDisk Cruzer [0781:5567]   unnamed
  [2] Filesystem BACKUP (ext4) on /dev/sdb1   unnamed
Number(s) to name, iN to ignore, Enter = scan on, q = quit: 2
Device name (Enter to skip): backup-disk
Properties you can match on: fs_uuid, fs_label, fs_type, devnode
Match on [fs_uuid] (Enter to accept, or type a list):
Match strength: strong — identifies this exact device
Tags, comma-separated (Enter for none): backup
Saved device 'backup-disk' to /home/me/.config/udevmon/config.toml
```

The prompts are asked in that order. If you type a property that doesn't
exist (say, a device name at the "Match on" prompt), it tells you and asks
again. When nothing more is left to name it prints `Waiting for more events`
and keeps scanning; press Ctrl+C to leave (the `q` answer works only at the
"Number(s) to name" prompt). To silence something you never want to react to
(a USB hub, an internal card reader), answer with `i` and its number (`i3`, or
`i1,3`): it is saved as an ignored device, without asking for tags (see
[Switching things off](#switching-things-off)).

A physical stick usually shows up twice: as a **USB device** (identified by
vendor/product ID and serial number) and as one or more **filesystems**
(identified by UUID). Name whichever you want to react to. Name the USB
device to react to it being plugged in at all; name the filesystem to react
to it being mounted and unmounted.

To also offer devices that are *already* plugged in, use `udevmon scan --present`.

**2. Attach an action**

```
udevmon actions add --device backup-disk mounted -- \
  notify-send "Backup disk ready" "{mountpoint}"
udevmon actions add --device backup-disk mounted \
  --name run-backup --timeout 600 -- ~/bin/backup.sh "{mountpoint}"
```

The format is
`actions add (--device NAME ... | --tag NAME ...) EVENT[,EVENT...] [--name NAME] [--timeout SECONDS] [--allow-unnamed | --named-only] -- COMMAND [ARGS...]`.
Options go **before** the `--`; everything after it is the command, verbatim.
`--device` and `--tag` can be repeated to target several devices or tags.

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
journalctl --user -u udevmon -f        # follow the log and action output
udevmon reload                         # re-read the config after editing it
systemctl --user restart udevmon
```

To only write the unit without enabling it, leave out `--now`.

## Events

- `connected` — a USB device is plugged in (USB properties).
- `disconnected` — a USB device is unplugged (USB properties).
- `mounted` — a filesystem is mounted (filesystem properties, `mountpoint`).
- `unmounted` — a filesystem is unmounted, or its device disappears while mounted (filesystem properties, `mountpoint`, `surprise`).
- `removed` — a mounted filesystem **vanished without being unmounted first** (the stick was pulled, or the drive failed). It comes right after `unmounted`, which fires too (filesystem properties, `mountpoint`, `surprise`).
- `startup` — the device was **already present** when udevmon started (USB or filesystem properties).
- `shutdown` — the device is **still present** when udevmon exits (USB or filesystem properties).

Notes:

- Pulling a mounted stick without ejecting it produces `unmounted` and then
  `removed` for its filesystem, and `disconnected` for the USB device. A normal
  eject produces only `unmounted`. The `surprise` value tells them apart: `1`
  for a pulled stick, `0` for a clean unmount, so one action on `unmounted` can
  behave differently (`{surprise}` in the command, `$UDEVMON_SURPRISE` in the
  environment). To act only on pulled sticks, use `on = ["removed"]`.
- A `removed` action cannot use the stick: it is already gone, so the mount
  point no longer exists and any command that reads or writes there fails.
  `{mountpoint}` is still filled in, but only as text (for a message or a log
  line). Use it to warn, log or clean up something of your own.
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

- `vendor_id`, `product_id` — e.g. `0781`, `5567`; lowercase hex.
- `serial` — e.g. `4C530001230815112433`; best identifier, when the device has one.
- `vendor`, `model` — e.g. `SanDisk`, `Cruzer Blade`; human-readable.
- `port_path` — e.g. `3-1.2`; the physical port/hub position.
- `devnode`, `syspath` — e.g. `/dev/bus/usb/003/007`.
- `udev_<name>` — e.g. `udev_id_input_keyboard`; every raw udev property, lowercased.

Filesystems:

- `fs_uuid` — e.g. `3F2A-91BC`; best identifier.
- `part_uuid` — e.g. `2b5c...`; partition UUID (GPT disks).
- `fs_label`, `fs_type` — e.g. `BACKUP`, `ext4`; labels are not unique.
- `devnode` — e.g. `/dev/sdb1`.
- `mountpoint` — e.g. `/media/me/BACKUP`; only on mounted/unmounted/removed (and mounted startup/shutdown) events.
- `surprise` — `1` if the filesystem vanished while mounted, `0` after a clean unmount; only on `unmounted` and `removed`. It can be used in commands but not in tag rules.
- `drive_vendor`, `drive_model`, `drive_serial` — of the physical drive holding it.

`udevmon present` lists everything currently present with these values, and
how strongly each could be matched.

Values are compared without regard to capitals (`fs_label = "backup"` matches a
drive labelled `BACKUP`), but otherwise exactly: no wildcards, and a thing that
lacks the property never matches.

### Match strength

When you create a device, udevmon tells you how precisely it identifies things:

- **strong**: includes a serial number or a UUID. Identifies exactly this
  device.
- **medium**: model plus physical port (`vendor_id` + `product_id` +
  `port_path`). Matches *any* device of that model in *that port*. This is
  what you get for devices without a serial number.
- **weak**: anything else, for example a filesystem label or just the model.
  Matches every device with those values.

Some cheap devices report fake serials (`0000000`, `1234567890`, ...). udevmon
treats those as weak. Prefer strong matches for anything that runs a script.

## Tags: one action for several devices

There are three ways to point one action at more than one device:

**1. A list of devices**

```
udevmon actions add --device yubikey --device card-reader \
  connected,disconnected -- notify-send "{device} {event}"
```

**2. Tags set on devices.** A tag is a label; give it to as many devices as you like and
target the label:

```
udevmon devices tag backup-disk backup
udevmon devices tag second-disk backup
udevmon actions add --tag backup mounted -- \
  ~/bin/backup.sh "{mountpoint}" "{device}"
```

(`devices add ... --tag backup` sets a tag when creating a device.)
Adding a third drive later is one command (`udevmon devices tag third-disk backup`);
the action doesn't change. A device can carry several tags, and an action can list
several tags (it runs if the thing has any of them). `udevmon devices untag NAME TAG`
removes one. Tags you set this way need no declaration.

**3. Tag rules.** A rule selects things by their properties, so you don't have to name
each one. A value can be a list, meaning "any of":

```
udevmon tags add storage --match fs_type=ext4 --match fs_type=exfat
```

which is this in the config file:

```toml
[tags.storage]
match = { fs_type = ["ext4", "exfat"] }
```

Rules can use any property from the [list above](#properties-you-can-match-on-and-use-in-commands)
except ones that change from plug to plug (`mountpoint`, `devnode`, `syspath`, and a few
raw udev ones); `udevmon check` rejects those. `udevmon tags list` and `udevmon tags show NAME`
show what each tag contains and which actions use it.

**Wildcards.** In a tag rule (not in a device's `match`), `*` stands for any run of
characters and `?` for exactly one, and the whole value must match, ignoring
capitals:

```
udevmon tags add backups --match "fs_label=BACKUP*"
```

```toml
[tags.backups]
match = { fs_label = "BACKUP*" }     # BACKUP, backup-2024, Backup_x ...
```

Nothing else is special (`.`, `[`, `(` mean themselves), so a label that really
contains a `*` or `?` cannot be matched literally. A pattern can select many
things, so a rule that uses one is rated **weak**, like a list, and `check` notes it
for an `allow_unnamed` action. A `*` or `?` in a device's own `match` is compared
literally, and `check` warns about it.

### Several tags: any of, or all of

An action with several tags runs when the thing has **any** of them. To require
**all** of them, say so:

```
udevmon actions add --tag backup --tag offsite --all-tags mounted -- \
  ~/bin/copy.sh "{mountpoint}"
udevmon actions set copy --all-tags     # or --any-tags for the default
```

```toml
[[actions]]
tag = ["backup", "offsite"]
tag_mode = "all"           # "any" is the default
on = ["mounted"]
run = ["~/bin/copy.sh", "{mountpoint}"]
```

The tags may come from any mix of explicit tags and rules. `{tag}` (and
`$UDEVMON_TAG`) is the first tag in the list. `tag_mode` only applies to actions
that use `tag`, and `explain` shows `tags backup + offsite` for such an action.

### Opting a device out of a rule tag

A rule tag selects things you never named, which is what you want for most
drives. A device of yours that a rule happens to select, but which should be left
out, opts out of that tag:

```
udevmon devices exclude my-disk storage
udevmon devices include my-disk storage     # back to normal
```

```toml
[devices.my-disk]
match = { fs_uuid = "3F2A-91BC" }
exclude_tags = ["storage"]
```

Now `my-disk` does not get the tag `storage` from its rule, so none of that tag's
actions run for it, but it is still a **named** device: it never falls back into
the unnamed category, and its own actions (`device = "my-disk"`) still run. This is
different from `status = "ignored"`, which silences the device completely.

An opt-out only stops a *rule* from selecting the thing. A tag the device carries
explicitly (`tags = [...]`) is never affected: listing the same tag in both
`tags` and `exclude_tags` is an error, and an explicit tag that *another* device
matching the same thing carries is kept. `udevmon explain` shows
`excluded from: storage`. `devices add` takes `--exclude-tag TAG` (repeatable).

### Named and unnamed things

A rule can select things you never named, for example any ext4 drive someone plugs in.
That is useful, but it is also exactly what "BadUSB" attacks rely on: the properties a
rule looks at (filesystem type, vendor ID, label) are chosen by whoever built the device.
So udevmon separates two cases:

- a **named device** (it matches one of your `[devices.*]`), whether it got the tag explicitly or through a rule: every action on the tag.
- **unnamed** (matches a rule only): only actions with `allow_unnamed = true`.

Naming a device is you vouching for it. An action must opt in before it may run for
hardware you haven't vetted:

```toml
[[actions]]
tag = "storage"
on = ["mounted"]
allow_unnamed = true              # also runs for drives I never named
run = ["notify-send", "Drive mounted"]

[[actions]]
tag = "storage"
on = ["mounted"]
allow_unnamed = false             # named devices only (the default)
run = ["~/bin/backup.sh", "{mountpoint}"]
```

On the command line: `--allow-unnamed` or `--named-only` on `actions add --tag ...`.

Things to know:

- `allow_unnamed` only *widens* who may trigger an action. A named device is never
  restricted by it.
- If you leave it out, the action behaves as `false`, and `udevmon check` prints a note
  for actions on rule tags so you make the choice on purpose.
- Keep `allow_unnamed = true` actions harmless (notifications, logging). They run on
  hardware whose labels, model names and serial numbers are chosen by its maker, so
  don't let them read or run anything from the device. `check` warns when such an
  action uses those values in its command, or uses `{device}` (which doesn't exist
  for unnamed things).
- Trust belongs to each thing, not to the whole stick: a stick appears as a USB device
  *and* as a filesystem. If you named only the filesystem, its USB device is still
  unnamed. In `scan` you can pick both entries at once (`1,2`) to name them together.
- `startup` and `shutdown` follow the same rules, so an allowed action on a broad rule
  fires once for every matching thing that is present, including system filesystems.
- `udevmon present` shows, for everything plugged in now, whether it is named and which
  tags it has, which is the quickest way to check a rule.

## Variables per device

A device can carry values of its own, so one action can behave differently for
each device it applies to. Instead of one action per drive:

```toml
[devices.photos]
match = { fs_uuid = "3F2A-91BC" }
tags = ["backup"]
vars = { dest = "/srv/backups/photos", keep = "30" }

[devices.music]
match = { fs_uuid = "7B10-44E0" }
tags = ["backup"]
vars = { dest = "/srv/backups/music", keep = "7" }

[[actions]]
tag = "backup"
on = ["mounted"]
run = ["~/bin/backup.sh", "{mountpoint}", "{var_dest}", "{var_keep}"]
```

`{var_NAME}` in a command is replaced by the value of the variable `NAME` of the
device the action runs for, and the environment gets `UDEVMON_VAR_NAME`. A
fourth drive is one more device; the action never changes.

```
udevmon devices add music --match fs_uuid=7B10-44E0 --var dest=/srv/m
udevmon devices set music --var keep=14 --unset-var old
```

`--var NAME=VALUE` sets (or changes) a variable and keeps the others; use it
more than once for several. Names are lowercase letters, digits and `_`.

- **Only named devices have variables.** A thing matched only by a tag rule has
  none, so an action that uses one is skipped for it, and `check` warns when an
  `allow_unnamed` action uses a variable.
- **A device that lacks the value** is skipped with an error (as for any missing
  placeholder), never run with an empty value. `check` warns about every device an
  action can reach that does not define a variable it uses, and rejects one that
  no device defines at all (usually a typo).
- The values are yours, not the hardware's, so unlike a label or serial number
  they count as safe in the checks for `allow_unnamed` actions.
- When a thing matches several devices, the first one's variables are used.
- `devices show` lists them, and `explain` shows the command with them filled in.

## Checking what will run

`udevmon explain` answers "what would happen if this were plugged in?"
without plugging anything in. It applies the same rules as the daemon (named or
unnamed, tags, `allow_unnamed`) and shows the commands with their values filled
in.

```
udevmon explain                     # everything plugged in now
udevmon explain mounted             # only what "mounted" would do
udevmon explain connected --device stick
udevmon explain mounted --prop fs_type=ext4 \
  --prop mountpoint=/media/x
```

With `--device NAME` it simulates that named device; `--prop KEY=VALUE` adds
properties (repeat it), so you can try an unnamed ext4 drive against a rule
without owning one. Only the properties you give are known. The output looks
like this:

```
Filesystem X (ext4) at /media/x   unnamed   tags: storage
  mounted       runs 'open' (tag storage, unnamed)
                  notify-send 'mounted /media/x'
  mounted       skips 'closed': an unnamed thing, and the action
                does not set allow_unnamed = true
  nothing to run for: unmounted, startup, shutdown
```

Things that match no device and no tag are left out (a count says how many);
`--all` lists them, and system filesystems too.

## Switching things off

You can stop things running without deleting them.

**A device has a status.** Left out (or `""` or `"active"`) it works normally.

- `disabled`: the device's *own* actions, the ones that name it
  (`device = "stick"`), don't run. It is still recognised, so if it belongs to a
  tag, the tag's actions still run for it.
- `ignored`: nothing runs for it, whichever way an action would reach it
  (its own actions, tag actions, `allow_unnamed` ones). If a thing matches two
  devices and either is ignored, nothing runs.

A disabled or ignored device still counts as *named*: it never falls back into
the unnamed category, where a rule tag could start running `allow_unnamed`
actions for it. `startup` and `shutdown` follow the same rules.

```
udevmon devices disable stick hub    # own actions off, tags still apply
udevmon devices ignore hub           # nothing runs for it at all
udevmon devices enable stick hub     # back to normal
```

```toml
[devices.hub]
match = { vendor_id = "1d6b", product_id = "0002", port_path = "usb1" }
status = "ignored"          # or "disabled"; leave it out for normal
```

**An action can be disabled** with `enabled = false`, or with
`udevmon actions disable NAME` and `actions enable NAME`. It stays in the file
and in `udevmon explain`, marked as disabled, so it is never a mystery why it
didn't run.

**`udevmon actions set NAME`** changes an action without removing and adding it
again:

```
udevmon actions set backup --timeout 900
udevmon actions set backup --on mounted,startup
udevmon actions set notify --allow-unnamed
udevmon actions set notify --named-only
udevmon actions set notify --default-unnamed   # clear it: not stated
udevmon actions set notify --add-device card --remove-device stick
udevmon actions set archive --add-tag backup
udevmon actions set notify --timeout 30 -- notify-send "{device} {event}"
```

A command after `--` replaces the action's command (the same as in `actions add`;
it must come last). The new command is checked like any other, so a placeholder
that the action's events don't provide (`{mountpoint}` on `connected`) is
refused and nothing is written. A comment after the old `run` line is kept.

`--add-device`, `--remove-device`, `--add-tag` and `--remove-tag` change what the
action targets, and can be repeated and combined with the other options. An
action targets devices or tags, never both, so the device options work on
actions that use `device` and the tag options on actions that use `tag`. A
device you add must exist; a tag needs no declaration. Removing something the
action does not target is an error, and so is removing its last target (use
`actions remove` for that). Adding what is already there changes nothing. A
single name stays a plain string, a list is edited in place, and comments and
layout are kept.

`--default-unnamed` removes the `allow_unnamed` line; the action then behaves as
named-only, and `check` notes it again if its tag has a rule. `--allow-unnamed`
and the other two apply only to actions that use `tag`.

The list commands, `present`, `scan`, `explain` and `check` show what is switched
off, and every one of these commands reloads a running daemon (`--no-reload`
skips it), like the other editing commands.

## Renaming and changing things

```
udevmon devices rename stick usb-key
udevmon tags rename old-tag archive
udevmon actions rename backup nightly-backup
udevmon devices set usb-key --match vendor_id=1050 --match serial=Q1
udevmon devices set usb-key --description "Work key"
```

**Renaming** changes the name everywhere it is used, in one step:

- a device: its `[devices.NAME]` header (and sub-tables such as
  `[devices.NAME.match]`) and every action that names it, whether as
  `device = "NAME"` or in a list, even one spread over several lines;
- a tag: its `[tags.NAME]` block, the `tags` of every device that carries it,
  and the `tag` of every action that uses it (a tag you never declared can be
  renamed too);
- an action: its `name`.

The header stays where it was and comments and layout are kept. Before writing,
udevmon checks that the result is exactly your old config with only that name
changed, in the same order; if not, nothing is written. The command then tells you:

- which actions or devices it updated;
- which actions changed name because they have none of their own (an action
  without a `name` line is called after its target, such as `stick-4`);
- where the old name still appears in **comments, descriptions or commands**
  (`run = ["echo", "stick"]`). Those are left alone, since they may not mean
  the device.

Limits: a device written with dotted keys under `[devices]` instead of its own
`[devices.NAME]` table can't be renamed (it says so), and an action that has no
`name` line has to be given one first (edit the file).

**`devices set`** replaces a device's whole match (`--match`, repeated for
several keys; a device match takes single values, capitals ignored) and/or its `--description`
(empty removes it). A comment after the old `match` line is kept.

**`tags set`** changes a declared tag (one with a `[tags.NAME]` table):

```
udevmon tags set storage --match fs_type=ext4 --match fs_type=exfat
udevmon tags set storage --description "Filesystems I care about"
udevmon tags set storage --no-rule
```

`--match` replaces the whole rule (repeat a key to allow several values, as in
`tags add`), and `--no-rule` removes it, leaving a tag that only the devices that
carry it belong to. A comment after the old `match` is kept, and a rule that
uses a property changing from plug to plug is refused. A tag that only devices
carry has no table to edit: declare it with `tags add`. A new rule changes which
things the tag's actions run for, so run `udevmon explain` afterwards.

Names may contain letters, digits, `_`, `.` and `-`. A name with a dot is written
in quotes in the file (`[devices."usb.1"]`), which the commands handle for you.

## Pausing everything

`udevmon pause` stops every action without touching your config, and
`udevmon resume` starts them again. Use it while you do a backup by hand, test
with sticks that would otherwise trigger actions, or give a demonstration.

```
udevmon pause             # until you run `udevmon resume`
udevmon pause --for 30m   # or for a while: 90s, 30m, 2h, 1d, 1h30m
udevmon resume
```

- While paused **nothing runs**, not even `startup` and `shutdown` actions. The
  daemon keeps receiving events and logs them; events that happen during a
  pause are **not** replayed afterwards.
- The pause is a small file in your runtime directory
  (`$XDG_RUNTIME_DIR/udevmon.paused`), so it survives a daemon restart but not a
  logout or reboot: a forgotten pause can't outlive your session. A pause with
  `--for` ends by itself.
- You can pause when no daemon is running; it applies once one starts.
- `udevmon explain` and the editing commands say `Note: automation is paused`
  so a paused daemon doesn't look broken, and the daemon's start-up log line
  shows `[PAUSED]`.

## Configuration file

The file is `~/.config/udevmon/config.toml` (respects `$XDG_CONFIG_HOME`). Use
`--config PATH` to use a different one; note that `--config` goes *before* the
subcommand: `udevmon --config my.toml run`.

`man udevmon.toml` is the full reference for the file. You can edit it by hand
or through the CLI. CLI edits keep your comments and
formatting, and save the previous version as `config.toml.bak`.

- **CLI edits reload the daemon for you.** After `devices add`, `actions
  remove` and the other editing commands (and after `scan` saves a device),
  a running daemon that uses the same config file picks the change up right
  away, and the command says `Reloaded the running daemon.` A daemon that
  uses a different config file is left alone. Add `--no-reload` to skip it.
- **After editing by hand,** run `udevmon check`, then `udevmon reload`.
- `udevmon check` also warns when an action's program can't be found or isn't
  executable (say, a mistyped script path). The daemon logs the same warning
  when it starts and on every reload, using *its own* `PATH`, which under
  systemd is often shorter than your shell's.

```toml
[daemon]                       # every setting here is optional
debounce = 1.0                 # ignore repeats within N seconds
max_workers = 4                # max events handled in parallel
udev_filters = ["usb/usb_device"]

[devices.backup-disk]
description = "Weekly backup drive"
match = { fs_uuid = "3F2A-91BC" }
tags = ["backup", "storage"]   # optional explicit tags

[devices.yubikey]
match = { vendor_id = "1050", product_id = "0407", serial = "A1B2C3D4E5" }

[devices.card-reader]
match = { vendor_id = "2ce3", product_id = "9563", port_path = "3-3" }

[tags.storage]                 # a rule tag: things selected by properties
description = "Filesystems I care about"
match = { fs_type = ["ext4", "exfat"] }    # a list means "any of"
# match = { fs_label = "BACKUP*" }         # * and ? are wildcards in a rule

# A device can also have:
#   exclude_tags = ["storage"]     # opt out of a rule tag
#   vars = { dest = "/srv/x" }     # used as {var_dest} in commands
# and an action on several tags: tag_mode = "all"   # default "any"

[[actions]]                    # one device
name = "run-backup"            # optional
device = "backup-disk"         # a name, or a list of names
on = ["mounted"]               # one event or a list
run = ["~/bin/backup.sh", "{mountpoint}"]
timeout = 600                  # kill after N seconds (default 60)

[[actions]]                    # several devices
device = ["yubikey", "card-reader"]
on = ["connected", "disconnected"]
run = ["notify-send", "{device} {event}"]

[[actions]]                    # a tag, including things I never named
tag = "storage"                # a name, or a list (any of)
on = ["mounted"]
allow_unnamed = true
run = ["notify-send", "Drive mounted ({tag})"]

[[actions]]                    # a tag, named devices only
tag = "backup"
on = ["mounted"]
allow_unnamed = false
run = ["~/bin/backup.sh", "{mountpoint}", "{device}"]
```

An action has exactly one of `device` or `tag`. Tag names, device names and action names
use letters, digits, `_`, `.` and `-`.

### How commands are run

- `run` is an argument list, or a string split shell-style. It is
  **never** passed to a shell: no pipes, globbing or `$VARIABLE` expansion in
  the command itself. If you need a shell, ask for one explicitly:
  `run = ["sh", "-c", "cd \"$1\" && ls | wc -l >> ~/count.log", "sh", "{mountpoint}"]`
- A leading `~` in an argument is expanded to your home directory.
- `{var_NAME}` is a variable of the device (see
  [Variables per device](#variables-per-device)).
- `{name}` is replaced by a property value: `{mountpoint}`, `{serial}`,
  `{fs_uuid}`, `{devnode}`, and so on, plus `{event}`, `{device}` (the name of the device
  that applies; not available for unnamed things) and `{tag}` (tag actions only). Values
  are always passed as a single argument, so spaces or shell characters in a
  mount point can't break out of the command.
- Only `{lowercase_names}` are placeholders. Other braces (`${HOME}`,
  `awk '{print $1}'`) are left alone. Write `{{` or `}}` for a literal brace.
- If a placeholder isn't available for the event (for example `{mountpoint}`
  on a `connected` event), `udevmon check` reports it. If it turns out to be
  missing at run time, the action is **skipped** with an error rather than
  run with an empty value.
- The same information is available as environment variables:
  `UDEVMON_EVENT`, `UDEVMON_DEVICE` (when the thing is named), `UDEVMON_TAG` (tag actions),
  and `UDEVMON_<PROPERTY>` (for example `UDEVMON_MOUNTPOINT`, `UDEVMON_SERIAL`).
- Commands run as you, with your home directory as working directory and no
  standard input. Their output goes to the log.
- Several actions for one event run one after another, in file order.
  A failing action does not stop the next.
- An action that runs longer than its `timeout` is terminated (along with any
  child processes it started).

## Command reference

```
udevmon scan [--present] [--no-prompt]  watch events and name things
udevmon present [--all]                 list what is plugged in now
udevmon devices list | show NAME | remove NAME
udevmon devices add NAME --match KEY=VALUE ...
                    [--tag TAG ...] [--exclude-tag TAG ...]
                    [--var NAME=VALUE ...] [--description TEXT]
udevmon devices tag NAME TAG...         add explicit tags to a device
udevmon devices set NAME [--match KEY=VALUE ...] [--description TEXT]
                    [--var NAME=VALUE ...] [--unset-var NAME ...]
udevmon devices|tags|actions rename OLD NEW
udevmon devices disable | ignore | enable NAME...
                                        switch devices off (see above)
udevmon devices untag NAME TAG...
udevmon devices exclude NAME TAG...     opt a device out of rule tags
udevmon devices include NAME TAG...     stop opting it out
udevmon tags list | show NAME | remove NAME
udevmon tags add NAME [--match KEY=VALUE ...] [--description TEXT]
udevmon tags set NAME [--match KEY=VALUE ... | --no-rule]
                      [--description TEXT]
udevmon actions list | remove NAME
udevmon actions add (--device NAME ... | --tag NAME ...) EVENT[,EVENT...]
                    [--name NAME] [--timeout S] [--all-tags]
                    [--allow-unnamed | --named-only] -- COMMAND...
udevmon actions set NAME [--allow-unnamed | --named-only
                          | --default-unnamed] [--on EVENTS] [--timeout S]
                    [--add-device NAME ...] [--remove-device NAME ...]
                    [--add-tag NAME ...] [--remove-tag NAME ...]
                    [--all-tags | --any-tags] [-- COMMAND...]
udevmon actions disable | enable NAME...
udevmon explain [EVENT] [--device NAME] [--prop KEY=VALUE ...] [--all]
                                        what would run (see above)
udevmon pause [--for DURATION]          stop every action (see above)
udevmon resume                          let actions run again
udevmon check                           validate the config
udevmon run [--dry-run]                 run the daemon in the foreground
udevmon reload                          make the running daemon reload
udevmon service install [--now]         install the systemd user service
udevmon service uninstall               stop and remove it
udevmon completion bash | install | uninstall
                                         bash tab completion
```

`man udevmon` describes each command and option in full.

Global options, placed before the subcommand: `--config PATH`, `-v` (debug logging).
The editing commands (`devices`, `tags`, `actions`: add, remove, tag, untag)
also take `--no-reload`.

Manual device creation, without scanning:

```
udevmon devices add my-key --match vendor_id=1050 \
  --match product_id=0407 --match serial=A1B2C3D4E5
```

A device can't be removed while an action lists it by name, and a declared tag (`[tags.X]`)
can't be removed while an action uses it; remove those actions first. Repeating `--match` with
the same key on `tags add` builds a list (`--match fs_type=ext4 --match fs_type=exfat`);
on `devices add` a repeated key is an error, since a device match is exact.
Actions created by hand without a `name` can't be removed through the CLI
(edit the file instead).

## Examples

Notify when a specific USB stick is plugged in or removed:

```
udevmon actions add --device my-stick connected,disconnected -- \
  notify-send "USB stick {event}"
```

Run a backup when the backup drive is mounted, and safely unmount afterwards:

```toml
[[actions]]
name = "backup"
device = "backup-disk"
on = ["mounted"]
run = ["sh", "-c",
       'rsync -a ~/Documents/ "$1/Documents/" && udisksctl unmount -b "$2"',
       "sh", "{mountpoint}", "{devnode}"]
timeout = 3600
```

Warn when a drive is pulled without ejecting it (its data may be damaged):

```
udevmon actions add --device backup-disk removed -- \
  notify-send -u critical "Drive pulled without ejecting" "{fs_label}"
```

One action can handle both cases by looking at `surprise`. This logs a clean
eject quietly and warns loudly about a pull:

```
udevmon actions add --device backup-disk unmounted -- sh -c '
  if [ "$1" = 1 ]; then
    notify-send -u critical "Pulled without ejecting" "$2"
  else
    echo "$2 ejected cleanly" >> ~/eject.log
  fi' sh {surprise} {fs_label}
```

Lock the screen when a security key is removed:

```
udevmon actions add --device my-key disconnected -- loginctl lock-session
```

Do something with a drive that is already connected when you log in *and*
when it is plugged in later:

```
udevmon actions add --device backup-disk startup,mounted -- \
  ~/bin/sync-check.sh
```

Get a notification for any ext4/exfat drive that is mounted, and run the backup
only for the drives you named and tagged `backup`:

```
udevmon tags add storage --match fs_type=ext4 --match fs_type=exfat
udevmon actions add --tag storage mounted --allow-unnamed -- \
  notify-send "Drive mounted: {tag}"
udevmon devices tag backup-disk backup
udevmon actions add --tag backup mounted --named-only -- \
  ~/bin/backup.sh "{mountpoint}"
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
  group/others, since that file decides what gets executed;
- running actions for hardware you haven't named only when the action says
  `allow_unnamed = true` (see [Named and unnamed things](#named-and-unnamed-things)).

Keep scripts triggered by removable devices short, and don't run actions that
trust the *contents* of the device without care.

## Troubleshooting

- **Nothing happens when I plug the device in.** Run `udevmon -v run` in a
  terminal and plug it in. Look for `nothing matched`, which means the
  properties differ from what you saved. Compare with `udevmon present` or
  `udevmon scan`. Remember that a stick appears as both a USB device and a
  filesystem, and a device matches only one of those.
- **An action doesn't run when I expect it to.** First check you haven't paused
  automation (`udevmon explain` says so; `udevmon resume` ends it). Then run
  `udevmon explain`. It
  shows, for everything plugged in now, whether the thing is `unnamed` or a
  named `device:`, which `tags:` it has, and which actions would run or be
  skipped, with the reason. An unnamed thing runs only actions with
  `allow_unnamed = true`, and a thing with no tags matches no tag action at
  all. `udevmon -v run` logs the same reasons, and `udevmon check` explains
  the notes about `allow_unnamed`.
- **The daemon says a command was not found.** It checked with its own
  `PATH`. Under systemd that is often shorter than your shell's, so use the
  full path to the program in `run`, or put the directory on the service's
  `PATH`.
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
- Rule values are strings, lists of them, or patterns with `*` and `?` (capitals
  ignored); there are no regular expressions. Variables belong to devices, not to tags.
- No GUI or TUI yet; configuration is the TOML file plus the CLI.

## Development

```
PYTHONPATH=src python3 -m unittest discover -s tests -t .
```

Running from the source tree needs the dependencies installed too, `tomlkit`
included (`pip install --user tomlkit`, or `apt install python3-tomlkit`).

### Real-hardware test

The unit tests use synthetic events. To check the real thing, including a
stick pulled out while mounted, run the guided script with a USB stick you
don't mind plugging in and out:

```
systemctl --user stop udevmon     # only if you use the service
python3 tools/usb-test.py
```

It asks you to plug the stick in, pull it out without ejecting it (nothing
should be writing to it) and plug it in again, and does everything else. It
uses a throwaway config and runtime folder, switches off the desktop's "open a
window when a volume is mounted" setting while it runs and restores it, and
prints PASS or FAIL for each check. If one fails it writes
`usb-test-report.txt` in the current folder. `--keep-popups` leaves the
desktop setting alone.

`SPECIFICATIONS.md` describes the design, data model and behaviour in detail.
`CHANGELOG.md` records what changed in each version; add a line under
"Unreleased" with any change a user would notice.
