# rclone-config: rclone credentials from a drive

A udevmon helper that makes the rclone config carried on a removable drive
available while that drive is plugged in, and removes it as soon as the drive
is gone. It is not part of udevmon itself: udevmon only runs it as an action.

Use it on a computer that has no permanent rclone config of its own, so that
the credentials travel on the drive. Anything can use them: kp-sync, or your
own actions (a backup of some folders to the cloud, for example).

## What it does

The drive must carry the config at `config/rclone/rclone.conf` (from the
drive's root). Only that file is used; anything else in the folder is ignored.

- **When a tagged drive is mounted** (or is already mounted when udevmon
  starts), the helper checks that the mounted filesystem really is that drive,
  then copies its `rclone.conf` to the *published config*:

  ```
  $XDG_RUNTIME_DIR/rclone/rclone.conf     (usually /run/user/UID/...)
  ```

  The folder is private (mode 0700, file 0600) and lives in memory (tmpfs),
  so it never reaches the disk and is gone after logout.
- **When the drive goes away** (unmounted or pulled out), or udevmon stops, the
  published config is deleted, unless another tagged drive is still present:
  then that drive's config is published instead. A published config never
  outlives the drive it came from.
- **With several tagged drives**, the first one's config stays published while
  it is present. If they carry different configs, the log says so (it never
  shows their contents).
- **Nothing else is touched:** not `~/.config/rclone/rclone.conf` (rclone's
  usual config, which plain `rclone` keeps using), not other files next to the
  published one, and never the drive (it only reads from it).

A drive that is tagged for this helper but has no `config/rclone/rclone.conf`
is an error, shown in udevmon's log, so a wrong tag doesn't go unnoticed.

## Using the published config

Point rclone at it explicitly:

```
rclone --config "$XDG_RUNTIME_DIR/rclone/rclone.conf" lsd GoogleDrive:
```

or set `RCLONE_CONFIG` to that path. The file is either there, complete, or
not there at all; when it isn't, no credentials are available right now.

rclone writes refreshed access tokens back into the file it was given. That's
fine here: the copy on the drive is never updated, and for Google Drive the
copy stays valid. Backends whose refresh token changes on every use (OneDrive,
Box) are not a good fit: the drive's copy would stop working.

A program that keeps working after the drive is removed (kp-sync's last sync,
for example) should take its own private copy instead of reading this file
until the end. For kp-sync that's part of its own design; see its
`design/endpoints-and-access.md`, "Integration points".

## Installing

It's a small Python package (standard library only, Python 3.10 or newer)
that provides the `rclone-config` command. Install it into the virtual
environment you keep your tools in, e.g. `~/.venvs/tools`, from the udevmon
source folder:

```
uv pip install --python ~/.venvs/tools/bin/python ./contrib/rclone-config
```

(or `~/.venvs/tools/bin/pip install --no-deps ./contrib/rclone-config` if
that environment has pip). The command lands in `~/.venvs/tools/bin`. The
same command updates it. Building leaves `build/` and `*.egg-info` in
`contrib/rclone-config/`; they are git-ignored and can be deleted.

udevmon looks up bare command names only in `[daemon] script_dir` and the
system folders, so add the environment's `bin` to `script_dir`:

```toml
[daemon]
script_dir = ["~/.local/udev/scripts", "~/.venvs/tools/bin"]
```

Every command in that folder can then be used by its bare name in actions
(`kp-sync` too), ahead of the system folders: a bare `python3` in an action
becomes the environment's Python.

Then add the two actions from [`udevmon.toml.example`](udevmon.toml.example)
to your config, **above** any action that uses the credentials (actions of
one event run in config order, so the credentials are ready first), and tag
the drives that carry credentials:

```
udevmon devices tag KEYDRIVE-FS rclone
```

Check with `udevmon explain` that both actions are selected for the drive,
with the full path of `rclone-config`.

## Checking

```
rclone-config status
```

says whether a config is published, from which drive, and which tagged drives
are present. It never shows the config itself.

## Removing

Delete the two actions, then
`uv pip uninstall --python ~/.venvs/tools/bin/python udevmon-rclone-config`.
If a config is still published,
unplug the drive first (or remove `$XDG_RUNTIME_DIR/rclone/rclone.conf`).

## Limits

- If udevmon is killed or crashes, it has no chance to remove the published
  config. The next event of a tagged drive fixes it, and logout clears it.
- While udevmon is paused (`udevmon pause`), no action runs: a drive removed
  during a pause leaves its config published until the next event of a
  tagged drive. Unplug drives with credentials after resuming.
- A program still running rclone when the published config is deleted may
  write it back when it saves a refreshed token. The next event of a tagged
  drive deletes it again.

## Files

| Path | What |
|---|---|
| `$XDG_RUNTIME_DIR/rclone/rclone.conf` | the published config |
| `$XDG_RUNTIME_DIR/rclone-config/lock` | serializes runs |
| `$XDG_RUNTIME_DIR/rclone-config/sources/` | one record per tagged drive present |
| `$XDG_RUNTIME_DIR/rclone-config/published.json` | which drive is published |

Messages go to stderr (udevmon's log) as `INFO:` or `ERROR:` lines. Exit
status: 0 done, 1 error, 2 wrong usage.
