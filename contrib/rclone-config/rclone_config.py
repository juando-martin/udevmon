"""Publish an rclone config from a removable drive while the drive is present.

Run by udevmon actions (see README.md next to this file):

    rclone-config up   MOUNTPOINT SERIAL:FS_UUID   # startup, mounted
    rclone-config down MOUNTPOINT SERIAL:FS_UUID   # unmounted, shutdown
    rclone-config status

`up` copies MOUNTPOINT/config/rclone/rclone.conf to
$XDG_RUNTIME_DIR/rclone/rclone.conf (the published config) for any rclone
user to read. The published config never outlives the drive it came from:
when that drive goes, it is switched to another present drive's config, or
deleted when none is left. Nothing else is touched: not ~/.config/rclone,
not other files next to the published one, not the drive.

Private state (lock, one record per present drive, which drive is published)
lives in $XDG_RUNTIME_DIR/rclone-config/. Messages go to stderr with INFO: or
ERROR: prefixes; credential contents are never printed. Exit status: 0 done,
1 error, 2 usage. Standard library only; Python 3.10 or newer. Installed as
the `rclone-config` command (see pyproject.toml); `python3 rclone_config.py`
runs it from the source tree too.
"""

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path

SOURCE = Path("config/rclone/rclone.conf")      # relative to the mount point
MAX_SIZE = 1 << 20                               # an rclone.conf is a few KiB
IDENTITY = re.compile(r"[^:/\\\x00]+:[A-Za-z0-9-]+\Z")


class Failure(Exception):
    """An error to report as ERROR: and exit 1."""


def info(message):
    print(f"INFO: {message}", file=sys.stderr)


# --- paths ------------------------------------------------------------------

def runtime_dir():
    value = os.environ.get("XDG_RUNTIME_DIR")
    if not value:
        raise Failure("XDG_RUNTIME_DIR is not set")
    return Path(value)


def published_dir():
    return runtime_dir() / "rclone"


def published_path():
    return published_dir() / "rclone.conf"


def state_dir():
    return runtime_dir() / "rclone-config"


def private_dir(path):
    """Create PATH (0700) or check an existing one is ours and not a link."""
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass
    info_ = os.lstat(path)
    if not stat.S_ISDIR(info_.st_mode) or info_.st_uid != os.getuid():
        raise Failure(f"{path} must be a directory owned by you, not a link")
    if stat.S_IMODE(info_.st_mode) != 0o700:
        os.chmod(path, 0o700)
    return path


def fsync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path, data):
    """Write DATA to PATH (0600) so readers see the old file or the new one."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_dir(path.parent)


def check_published_file(path):
    """The published file, if present, must be a regular file of ours."""
    try:
        info_ = os.lstat(path)
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info_.st_mode) or info_.st_uid != os.getuid():
        raise Failure(f"{path} must be a regular file owned by you, not a link")
    return True


# --- drives -----------------------------------------------------------------

def check_identity(identity):
    if not IDENTITY.fullmatch(identity):
        raise Failure(f"invalid drive identity {identity!r} (want SERIAL:FS_UUID)")
    return identity


def token(identity):
    return hashlib.sha256(identity.encode()).hexdigest()


def _walk(data, parent_serial=None):
    for device in data.get("blockdevices", data.get("children", [])) or []:
        if isinstance(device, dict):
            serial = device.get("serial") or parent_serial
            yield {**device, "serial": serial}
            yield from _walk(device, serial)


def mounted_identities():
    """Map each mounted filesystem's resolved mount point to SERIAL:FS_UUID.

    Same rule as kp-sync: the disk's serial is inherited by its partitions and
    combined with the filesystem UUID.
    """
    try:
        result = subprocess.run(
            ["lsblk", "--json", "--tree", "--output", "SERIAL,UUID,MOUNTPOINTS"],
            capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise Failure(f"cannot list mounted drives with lsblk: {exc}") from exc
    mounts = {}
    for device in _walk(data):
        serial, fs_uuid = device.get("serial"), device.get("uuid")
        if not serial or not fs_uuid:
            continue
        for point in device.get("mountpoints") or []:
            if point:
                mounts[str(Path(point).resolve())] = f"{serial.strip()}:{fs_uuid.strip()}"
    return mounts


def read_source(mountpoint):
    """Return the bytes of the drive's rclone.conf, refusing anything odd."""
    mount = Path(mountpoint).resolve()
    path = mount / SOURCE
    if not path.exists() and not path.is_symlink():
        raise Failure(f"drive at {mount} is tagged for rclone but has no {SOURCE}")
    if not path.resolve().is_relative_to(mount):
        raise Failure(f"{path} leads outside the drive")
    try:                                     # non-blocking: a FIFO must not hang us
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise Failure(f"cannot open {path}: {exc.strerror}") from exc
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise Failure(f"{path} is not a regular file")
    with os.fdopen(fd, "rb") as stream:
        data = stream.read(MAX_SIZE + 1)
    if len(data) > MAX_SIZE:
        raise Failure(f"{path} is larger than {MAX_SIZE} bytes")
    return data


# --- state ------------------------------------------------------------------

class State:
    """Records of present drives, and which one is published (under the lock)."""

    def __init__(self, directory, create=True):
        self.directory = directory
        self.sources_dir = directory / "sources"
        self.writable = create
        if create:
            private_dir(self.sources_dir)

    def sources(self):
        found = {}
        for path in sorted(self.sources_dir.glob("*.json")):
            try:
                record = json.loads(path.read_text())
                if path.stem != token(check_identity(record["identity"])):
                    raise ValueError("record name does not match its identity")
                if not (isinstance(record["mountpoint"], str)
                        and isinstance(record["hash"], str)
                        and isinstance(record["since"], int)):
                    raise ValueError("record fields have the wrong types")
                found[path.stem] = record
            except (OSError, ValueError, KeyError, TypeError, Failure):
                if self.writable:            # status only reports
                    info(f"dropping unreadable drive record {path.name}")
                    path.unlink(missing_ok=True)
        return found

    def save_source(self, record):
        path = self.sources_dir / f"{token(record['identity'])}.json"
        atomic_write(path, (json.dumps(record, sort_keys=True) + "\n").encode())

    def drop_source(self, key):
        (self.sources_dir / f"{key}.json").unlink(missing_ok=True)

    def published(self):
        try:
            record = json.loads((self.directory / "published.json").read_text())
            return record if isinstance(record, dict) else None
        except (OSError, ValueError):
            return None

    def set_published(self, record):
        path = self.directory / "published.json"
        if record is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write(path, (json.dumps(record, sort_keys=True) + "\n").encode())


def reconcile(state, mounts):
    """Drop records of drives that are no longer mounted where they were."""
    sources = state.sources()
    for key, record in list(sources.items()):
        if mounts.get(record.get("mountpoint")) != record["identity"]:
            info(f"forgetting drive {record['identity']}: no longer mounted "
                 f"at {record.get('mountpoint')}")
            state.drop_source(key)
            del sources[key]
    return sources


def publish(state, key, record, data):
    private_dir(published_dir())
    path = published_path()
    check_published_file(path)
    atomic_write(path, data)
    state.set_published({"source": key, "identity": record["identity"],
                         "source_hash": record["hash"]})
    info(f"published the rclone config of drive {record['identity']}")


def unpublish(state):
    path = published_path()
    if check_published_file(path):
        path.unlink()
        fsync_dir(path.parent)
        info("removed the published rclone config: no drive with one is present")
    state.set_published(None)
    try:
        published_dir().rmdir()              # only when nothing else is in it
    except (FileNotFoundError, OSError):
        pass


def settle(state, sources):
    """Make the published config match the drives that are present.

    Keep it while the drive it came from is present (whatever rclone wrote
    into it since, such as a refreshed token). Otherwise switch to the
    present drive that came first, or delete it when none is left.
    """
    current = state.published() or {}
    exists = check_published_file(published_path())
    if exists and current.get("source") in sources:
        record = sources[current["source"]]
        if record["hash"] == current.get("source_hash"):
            return
        info(f"drive {record['identity']} carries a changed rclone config")
    # The drive already published (if present) first, then by arrival.
    candidates = sorted(sources.items(), key=lambda item: (
        item[0] != current.get("source"), item[1]["since"]))
    for key, record in candidates:
        try:
            data = read_source(record["mountpoint"])
        except Failure as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            state.drop_source(key)
            continue
        record["hash"] = hashlib.sha256(data).hexdigest()
        state.save_source(record)
        publish(state, key, record, data)
        return
    unpublish(state)


def report_difference(sources, current):
    """Say (without contents) when present drives carry different configs."""
    hashes = {record["hash"] for record in sources.values()}
    if current and len(hashes) > 1:
        info(f"drives carry different rclone configs; the one of drive "
             f"{current.get('identity')} stays published while it is present")


# --- commands ---------------------------------------------------------------

def locked(function):
    def run(*args):
        directory = private_dir(state_dir())
        fd = os.open(directory / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            return function(State(directory), *args)
        finally:
            os.close(fd)
    return run


@locked
def up(state, mountpoint, identity):
    mount = str(Path(mountpoint).resolve())
    mounts = mounted_identities()
    sources = reconcile(state, mounts)
    try:
        if mounts.get(mount) != identity:
            raise Failure(f"drive {identity} is not the filesystem mounted at {mount}")
        data = read_source(mount)
    except Failure:
        if sources.pop(token(identity), None) is not None:
            state.drop_source(token(identity))
        settle(state, sources)               # never keep an orphaned config
        raise
    key = token(identity)
    previous = sources.get(key, {})
    record = {"identity": identity, "mountpoint": mount,
              "hash": hashlib.sha256(data).hexdigest(),
              "since": previous.get("since", time.time_ns())}
    state.save_source(record)
    sources[key] = record
    settle(state, sources)
    report_difference(sources, state.published())


@locked
def down(state, mountpoint, identity):
    key = token(identity)
    state.drop_source(key)
    sources = reconcile(state, mounted_identities())
    sources.pop(key, None)
    settle(state, sources)


def status():
    directory = state_dir()
    current = None
    sources = {}
    if (directory / "sources").is_dir() and not directory.is_symlink():
        state = State(directory, create=False)
        current, sources = state.published(), state.sources()
    path = published_path()
    print(f"published: {path} ({'present' if path.exists() else 'absent'})")
    if current:
        print(f"from drive: {current.get('identity')}")
    for record in sorted(sources.values(), key=lambda item: item["since"]):
        print(f"drive {record['identity']} at {record['mountpoint']}")


USAGE = ("usage: rclone-config up|down MOUNTPOINT SERIAL:FS_UUID\n"
         "       rclone-config status")


def main(argv):
    if argv[:1] == ["status"] and len(argv) == 1:
        status()
        return 0
    if len(argv) != 3 or argv[0] not in ("up", "down"):
        print(USAGE, file=sys.stderr)
        return 2
    command, mountpoint, identity = argv
    check_identity(identity)
    (up if command == "up" else down)(mountpoint, identity)
    return 0


def entry():
    """The `rclone-config` command."""
    try:
        sys.exit(main(sys.argv[1:]))
    except (Failure, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    entry()
