# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Command-line interface."""

from __future__ import annotations

import argparse
import functools
import json
import logging
import sys
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import configedit, jsonout
from .events import TRIGGER_KINDS, VALID_KINDS, describe
from .matching import build_match, device_label, resolve, strength


def _setup_logging(verbose: bool) -> None:
    fmt = "%(asctime)s %(levelname)s %(message)s" if sys.stderr.isatty() \
        else "%(levelname)s %(name)s: %(message)s"
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO, format=fmt,
                        datefmt="%H:%M:%S")


def _load(args, must_exist=False):
    try:
        return cfgmod.load(args.config, must_exist=must_exist)
    except cfgmod.ConfigError as e:
        for p in e.problems:
            print(f"error: {p}", file=sys.stderr)
        raise SystemExit(1)


def _guard(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except cfgmod.ConfigError as e:
        for p in e.problems:
            print(f"error: {p}", file=sys.stderr)
        raise SystemExit(1)


def _reload_after_edit(args) -> None:
    """Tell the running daemon (if it uses this config) to pick up the change; through
    the control socket this returns once it has reloaded (a refusal is a warning)."""
    if getattr(args, "no_reload", False):
        return
    from . import control
    from .daemon import reload_running
    try:
        if reload_running(args.config):
            print("Reloaded the running daemon.")
    except control.Refused as e:
        print(f"warning: the running daemon did not reload: {e.message}", file=sys.stderr)
        for problem in e.data.get("problems", []):
            print(f"  {problem}", file=sys.stderr)


def _pause_note() -> str | None:
    from . import pause
    paused, until = pause.status()
    if not paused:
        return None
    return f"automation is paused ({pause.describe_until(until)}): nothing runs."


def _copy_config(tmp: Path, main: Path) -> tuple[Path, dict]:
    """Copy ``main`` and its drop-ins into ``tmp``; (the copy of ``main``, the texts of
    the copied files: copy path -> text)."""
    import shutil
    copy = tmp / main.name
    if main.exists():
        shutil.copy2(main, copy)
    for f in cfgmod.dropin_files(main):
        cfgmod.dropin_dir(copy).mkdir(mode=0o700, exist_ok=True)
        shutil.copy2(f, cfgmod.dropin_dir(copy) / f.name)
    return copy, _texts(copy)


def _texts(main: Path) -> dict:
    """The text of ``main`` and of each of its drop-ins, by path."""
    return {f: f.read_text() for f in [main, *cfgmod.dropin_files(main)] if f.exists()}


def _real(f: Path, copy: Path, main: Path) -> Path:
    """The real file of ``f``, a file of the copy whose main file is ``copy``."""
    return main if f == copy else cfgmod.dropin_dir(main) / f.name


def _print_diff(before: dict, after: dict, copy: Path, main: Path) -> bool:
    """Print a unified diff per changed file, named by its real path; whether any changed."""
    import difflib
    changed = False
    for f in sorted(set(before) | set(after), key=lambda f: (f != copy, f.name)):
        old, new = before.get(f), after.get(f)
        if old == new:
            continue
        changed = True
        target = str(_real(f, copy, main))
        sys.stdout.writelines(difflib.unified_diff(
            (old or "").splitlines(keepends=True), (new or "").splitlines(keepends=True),
            fromfile=target if old is not None else "/dev/null", tofile=target))
    return changed


def _run_captured(fn, args) -> tuple[int, str, str]:
    """Run ``fn(args)`` with its output captured: (status, stdout, stderr)."""
    import contextlib
    import io
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            rc = fn(args)
        except SystemExit as e:
            rc = e.code
    return rc or 0, out.getvalue(), err.getvalue()


def _dry_run(fn, args) -> int:
    """Run the editing command ``fn`` on a copy of the configuration, then print what it
    would change as a unified diff. Writes nothing and tells no daemon to reload."""
    import tempfile
    main = Path(args.config)
    with tempfile.TemporaryDirectory(prefix="udevmon-dry-run-") as tmp:
        copy, before = _copy_config(Path(tmp), main)
        args.config = copy
        try:
            rc, out, err = _run_captured(fn, args)
        finally:
            args.config = main

        def shown(text):                          # the real folder, not the copy's
            return text.replace(str(Path(tmp)), str(main.parent))
        sys.stderr.write(shown(err))
        if rc:
            sys.stdout.write(shown(out))
            return rc
        if not _print_diff(before, _texts(copy), copy, main):
            print("No file would change.")
        sys.stdout.write(shown(out))
        print("Dry run: nothing was written.")
    return 0


#: Options an ``apply`` step may not have (``apply`` itself takes them, for all the steps).
APPLY_REFUSED = ("--config", "--dry-run", "--no-reload")


def _apply_steps(text: str) -> list[list[str]]:
    """The editing commands of an ``apply`` input: a JSON array of arrays of strings."""
    try:
        steps = json.loads(text)
    except ValueError as e:
        raise ValueError(f"the input is not JSON: {e}") from None
    if not (isinstance(steps, list) and steps
            and all(isinstance(x, list) and x and all(isinstance(w, str) for w in x)
                    for x in steps)):
        raise ValueError("the input must be a JSON array of commands, each an array of "
                         'strings such as ["devices", "add", "NAME", "--match", "KEY=VALUE"]')
    for i, argv in enumerate(steps, 1):
        refused = [w for w in argv if w.split("=", 1)[0] in APPLY_REFUSED]
        if refused:
            raise ValueError(f"step {i}: {refused[0].split('=', 1)[0]} is not allowed in a "
                             "step (give it to `apply` itself)")
    return steps


def cmd_apply(args):
    """``udevmon apply [FILE]``: run a sequence of editing commands as one change: all on a
    copy first, then written together (or, with ``--dry-run``, shown as one diff), with
    one reload. A step that fails changes nothing."""
    import tempfile
    try:
        text = sys.stdin.read() if args.file in (None, "-") else Path(args.file).read_text()
        steps = _apply_steps(text)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    main = Path(args.config)
    parser = build_parser()
    with tempfile.TemporaryDirectory(prefix="udevmon-apply-") as tmp:
        copy, before = _copy_config(Path(tmp), main)

        def shown(text):
            return text.replace(str(Path(tmp)), str(main.parent))
        outputs = []
        for i, argv in enumerate(steps, 1):
            line = ["--config", str(copy), *argv]
            rc, out, err = _run_captured(lambda a: parse_command(parser, a) and 0, line)
            if rc:
                print(f"error: step {i} ({' '.join(argv[:2])}): not a valid command",
                      file=sys.stderr)
                sys.stderr.write(shown(err))
                return 1
            step = parse_command(parser, line)
            fn = getattr(step, "fn", None)
            if not getattr(fn, "edits", False):
                print(f"error: step {i} ({' '.join(argv[:2])}): not an editing command",
                      file=sys.stderr)
                return 1
            rc, out, err = _run_captured(fn.__wrapped__, step)
            if rc:
                sys.stdout.write(shown(out))
                sys.stderr.write(shown(err))
                print(f"error: step {i} ({' '.join(argv[:2])}) failed; nothing was written",
                      file=sys.stderr)
                return 1
            outputs.append(shown(out) + shown(err))
        after = _texts(copy)
        if args.dry_run:
            if not _print_diff(before, after, copy, main):
                print("No file would change.")
            sys.stdout.write("".join(outputs))
            print("Dry run: nothing was written.")
            return 0
        gone = [f for f in before if f not in after]
        if gone:
            print(f"error: the steps removed {_real(gone[0], copy, main)}; apply does not "
                  "delete files", file=sys.stderr)
            return 1
        changed = {_real(f, copy, main): t for f, t in after.items() if before.get(f) != t}
        for f, old in before.items():                 # changed by someone else meanwhile?
            real = _real(f, copy, main)
            if not real.exists() or real.read_text() != old:
                print(f"error: {real} changed while the steps ran; nothing was written",
                      file=sys.stderr)
                return 1
        sys.stdout.write("".join(outputs))
        if not changed:
            print("No file changed.")
            return 0
        _guard(configedit.replace_files, main, changed)
    print(f"Applied {len(steps)} step(s): changed "
          + ", ".join(cfgmod.file_label(main, f) for f in changed) + ".")
    _reload_after_edit(args)
    note = _pause_note()
    if note:
        print(f"Note: {note}")
    return 0


def _reloads(fn):
    """Run an editing command, then reload the daemon if it succeeded (with
    ``--dry-run``, only show what it would change)."""
    @functools.wraps(fn)
    def wrapper(args):
        if getattr(args, "dry_run", False):
            return _dry_run(fn, args)
        rc = fn(args)
        if not rc:
            _reload_after_edit(args)
            note = _pause_note()
            if note:
                print(f"Note: {note}")
        return rc
    wrapper.edits = True                        # an editing command (``apply`` takes it)
    return wrapper


def _print_json(data) -> None:
    """Print ``data`` as indented UTF-8 JSON (the ``--json`` format, §8.7)."""
    print(json.dumps(data, indent=2, ensure_ascii=False))


# -- commands ------------------------------------------------------------------
def cmd_run(args):
    """``udevmon run``: run the daemon in the foreground until it is stopped."""
    from .daemon import Daemon
    return Daemon(args.config, args.dry_run).run()


def cmd_scan(args):
    """``udevmon scan``: print events as they come and offer to name what was plugged in."""
    from .scan import Scanner
    return Scanner(args.config, prompt=not args.no_prompt and sys.stdin.isatty(),
                   present=args.present, all_props=args.all_props).run()


def cmd_present(args):
    """``udevmon present``: the USB devices and filesystems present now (``--json``:
    :func:`present_entry` each)."""
    from .sources.udev import UdevSource
    from .sources.udisks import UdisksSource
    from .scan import format_all_props, format_props, status_text
    from .matching import level_label
    cfg = _load(args)
    endpoints = UdevSource(cfg.settings.udev_filters).snapshot() \
        + UdisksSource().snapshot(include_system=args.all)
    if args.json:
        _print_json([present_entry(cfg, d) for d in endpoints])
        return 0
    for d in endpoints:
        print(f"{describe(d)}   [{level_label(d)}]   {status_text(cfg, d)}")
        if args.all_props:
            for line in format_all_props(d):
                print(f"    {line}")
        else:
            print(f"    {format_props(d)}")
    return 0


def present_entry(cfg, endpoint) -> dict:
    """One present thing for ``present --json``: what it is, every property, and what it
    is in the configuration."""
    from .matching import default_match_keys
    res = resolve(cfg, endpoint.props)
    return {"kind": "filesystem" if endpoint.is_filesystem else "usb",
            "description": describe(endpoint),
            "props": dict(sorted(endpoint.props.items())),
            "devices": [d.name for d in res.named],
            "tags": list(res.tags),
            "suggested_match": build_match(endpoint, default_match_keys(endpoint))}


def _from(cfg, kind: str, name: str) -> str:
    """"  [config.d/NAME.toml]" for an entry of a drop-in file, else ""."""
    label = cfg.label(kind, name)
    return f"  [{label}]" if label else ""


def _file_line(cfg, kind: str, name: str) -> None:
    label = cfg.label(kind, name)
    if label:
        print(f"  file: {label}")


def _actions_for(cfg, device) -> list:
    """Actions that target the device by name or through one of its explicit tags."""
    return [x for x in cfg.actions
            if device.name in x.devices or set(x.tags) & set(device.tags)]


def _parse_env(pairs) -> dict:
    """``NAME=VALUE`` pairs (``--env``) as a dict; any NAME a command may have."""
    out = {}
    for pair in pairs or []:
        name, sep, value = pair.partition("=")
        if not sep or not cfgmod.ENV_NAME_RE.fullmatch(name):
            raise cfgmod.ConfigError([f"--env {pair!r}: use NAME=VALUE (NAME: letters, "
                                      "digits and _)"])
        if name.upper().startswith("UDEVMON_"):
            raise cfgmod.ConfigError([f"--env {name}: UDEVMON_* names are udevmon's own"])
        out[name] = value
    return out


def _parse_vars(pairs) -> dict:
    """NAME=VALUE pairs -> dict (the last one wins; the value may be empty)."""
    out: dict = {}
    for pair in pairs or []:
        key, eq, value = pair.partition("=")
        if not eq or not key:
            raise cfgmod.ConfigError([f"expected NAME=VALUE, got {pair!r}"])
        out[key] = value
    return out


def _parse_matches(pairs, *, allow_lists: bool) -> dict:
    """KEY=VALUE pairs -> dict. A repeated key becomes a list (tags) or an error (devices)."""
    match: dict = {}
    for pair in pairs:
        k, sep, v = pair.partition("=")
        if not sep or not k:
            raise cfgmod.ConfigError([f"bad --match {pair!r}, expected key=value"])
        if k in match:
            if not allow_lists:
                raise cfgmod.ConfigError([f"--match key {k!r} given twice; a device match takes "
                                          "exact values (lists are for tag rules)"])
            match[k] = [*([match[k]] if isinstance(match[k], str) else match[k]), v]
        else:
            match[k] = v
    return match


def _parse_props(pairs) -> dict:
    props: dict = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise cfgmod.ConfigError([f"bad --prop {pair!r}, expected key=value"])
        if key in props:
            raise cfgmod.ConfigError([f"--prop {key!r} given twice"])
        props[key] = value
    return props


def cmd_pause(args):
    """``udevmon pause [DURATION]``: stop running actions until ``resume`` or the time is up.

    A running daemon writes the pause flag itself (and logs it); with none answering,
    the command writes it."""
    from . import pause
    from .daemon import running_pid
    seconds = None
    if args.duration is not None:
        try:
            seconds = pause.parse_duration(args.duration)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
    result = _ask_daemon("pause", {"seconds": seconds})
    if result is not None:
        was_paused, until = result["was_paused"], result["until"]
    else:
        was_paused, _ = pause.status()
        until = pause.pause(seconds)
    print(("Already paused; now paused " if was_paused else "Automation paused ")
          + pause.describe_until(until) + ".")
    if until is not None:
        print("Run `udevmon resume` to continue sooner.")
    if running_pid() is None:
        print("No daemon is running right now; the pause applies once one starts.")
    return 0


def cmd_resume(args):
    """``udevmon resume``: end a pause (through the running daemon when it answers)."""
    from . import pause
    result = _ask_daemon("resume")
    was_paused = result["was_paused"] if result is not None else pause.resume()
    print("Automation resumed." if was_paused else "Automation was not paused.")
    return 0


def _ask_daemon(method: str, params: dict | None = None) -> dict | None:
    """The running daemon's answer to ``method``, or None when no daemon answers on the
    control socket (none runs, or an older one): the caller then does it itself."""
    from . import control
    from .daemon import running_pid
    if running_pid() is None:
        return None
    try:
        return control.call(method, params)
    except (control.NotRunning, control.ProtocolError, control.Refused, OSError):
        return None


def cmd_explain(args):
    """``udevmon explain``: what each event would run for the things present, or for a
    simulated one (``--device``, ``--prop``); runs nothing."""
    from . import explain, pause
    cfg = _load(args)
    note = None if args.json else _pause_note()
    if note:
        print(f"Note: {note}\n")
    props = _guard(_parse_props, args.prop)
    if args.device is not None and args.device not in cfg.devices:
        print(f"error: no such device {args.device!r}", file=sys.stderr)
        return 1
    kinds = [args.event] if args.event else None
    simulated = args.device is not None or bool(props)
    if simulated:
        endpoint = explain.simulated(cfg, args.device, props, args.event)
        if args.json:
            _print_json({"paused": pause.is_paused(), "simulated": True,
                         "things": [explain.explain_data(cfg, endpoint, kinds)],
                         "not_shown": 0})
            return 0
        shown = "  ".join(f"{k}={v}" for k, v in endpoint.props.items())
        lines = explain.explain_endpoint(cfg, endpoint, kinds,
                                         header=f"Simulated thing ({shown})")
        print("\n".join(lines))
        print("\nOnly the properties above are known; add --prop KEY=VALUE for others "
              "(for example fs_type=ext4).")
        return 0
    from .sources.udev import UdevSource
    from .sources.udisks import UdisksSource
    things = UdevSource(cfg.settings.udev_filters).snapshot() \
        + UdisksSource().snapshot(include_system=args.all)
    shown, idle = [], 0
    for endpoint in things:
        if args.event and args.event not in explain.kinds_for(endpoint):
            continue
        res = resolve(cfg, endpoint.props)
        if not args.all and not res.named and not res.tags:
            idle += 1                    # matches no device or tag: nothing can ever run for it
            continue
        shown.append(endpoint)
    if args.json:
        _print_json({"paused": pause.is_paused(), "simulated": False,
                     "things": [explain.explain_data(cfg, e, kinds) for e in shown],
                     "not_shown": idle})
        return 0
    blocks = ["\n".join(explain.explain_endpoint(cfg, e, kinds)) for e in shown]
    print("\n\n".join(blocks) if blocks else "Nothing present matches a device or a tag.")
    if idle:
        print(f"\nNot shown: {idle} present thing(s) that match no device or tag "
              "(--all lists them).")
    return 0


def cmd_devices_list(args):
    """``udevmon devices list``: every device (``--json``:
    :func:`~udevmon.jsonout.device_entry` each)."""
    cfg = _load(args)
    if getattr(args, "json", False):           # bare `udevmon devices` has no --json
        _print_json([jsonout.device_entry(cfg, d) for d in cfg.devices.values()])
        return 0
    if not cfg.devices:
        print("No devices. Run `udevmon scan` to create some.")
    for d in cfg.devices.values():
        tags = f"  tags: {', '.join(d.tags)}" if d.tags else ""
        print(f"{device_label(d):<20} {strength(d.match):<7} {d.match}{tags}  "
              f"({len(_actions_for(cfg, d))} action(s))" + _from(cfg, "device", d.name))
    return 0


def cmd_devices_show(args):
    """``udevmon devices show NAME``: one device and the actions that target it."""
    cfg = _load(args, must_exist=True)
    d = cfg.devices.get(args.name)
    if not d:
        print(f"error: no such device {args.name!r}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(jsonout.device_entry(cfg, d))
        return 0
    print(f"{d.name}: {d.description}\n  match: {d.match}  ({strength(d.match)})")
    _file_line(cfg, "device", d.name)
    if d.status != "active":
        print(f"  status: {d.status}")
    if d.tags:
        print(f"  tags: {', '.join(d.tags)}")
    if d.exclude_tags:
        print(f"  opts out of rule tags: {', '.join(d.exclude_tags)}")
    for key, value in d.vars.items():
        print(f"  variable {key} = {value}")
    for x in _actions_for(cfg, d):
        print(f"  action {x.name}: on {list(x.on)} run {list(x.run)} (timeout {x.timeout:g}s)")
    return 0


def mount_identity(endpoint, mountpoint: str) -> dict[str, str]:
    """``{fs_uuid, drive_serial}`` of the filesystem mounted at ``mountpoint``: the match
    ``devices add --from-mountpoint`` writes, and the values actions get as
    ``{fs_uuid}`` and ``{drive_serial}``.

    ConfigError when nothing is mounted there, the filesystem has no UUID, or its drive
    has no usable serial.
    """
    from .matching import is_suspicious_serial
    if endpoint is None:
        raise cfgmod.ConfigError([f"{mountpoint} is not the mount point of a filesystem "
                                  "udisks knows of (is the drive plugged in and mounted?)"])
    p = endpoint.props
    if not p.get("fs_uuid"):
        raise cfgmod.ConfigError([f"the filesystem at {mountpoint} has no UUID; name it with "
                                  "--match instead"])
    serial = p.get("drive_serial", "")
    if is_suspicious_serial(serial):
        why = f"a placeholder serial {serial!r}" if serial else "no serial number"
        raise cfgmod.ConfigError([f"the drive of {mountpoint} has {why}, so it cannot be told "
                                  "from another drive; name it with --match instead"])
    return {"fs_uuid": p["fs_uuid"], "drive_serial": serial}


def match_from_mount(cfg, endpoint, mountpoint: str) -> dict[str, str]:
    """:func:`mount_identity`, and ConfigError too when a device of ``cfg`` already
    matches the filesystem."""
    match = mount_identity(endpoint, mountpoint)
    named = resolve(cfg, endpoint.props).named
    if named:
        raise cfgmod.ConfigError([f"the filesystem at {mountpoint} already matches device(s) "
                                  f"{', '.join(repr(d.name) for d in named)}"])
    return match


def identify_entry(cfg, endpoint, mountpoint: str) -> dict:
    """``devices identify --json``: the filesystem at ``mountpoint``, the identity udevmon
    gives it, and the devices and tags it has in the configuration."""
    try:
        match, problem = mount_identity(endpoint, mountpoint), None
    except cfgmod.ConfigError as e:
        match, problem = None, e.problems[0]
    res = resolve(cfg, endpoint.props)
    return {"mountpoint": endpoint.props.get("mountpoint", mountpoint),
            "description": describe(endpoint),
            "fs_uuid": endpoint.props.get("fs_uuid"),
            "drive_serial": endpoint.props.get("drive_serial"),
            "match": match, "problem": problem,
            "devices": [{"name": d.name, "status": d.status,
                         **jsonout.where(cfg, "device", d.name)} for d in res.named],
            "tags": list(res.tags), "props": dict(sorted(endpoint.props.items()))}


def cmd_devices_identify(args):
    """``udevmon devices identify MOUNTPOINT``: the identity of the filesystem mounted
    there (:func:`identify_entry`); 1 when nothing udisks knows is mounted there."""
    from .sources.udisks import UdisksSource
    cfg = _load(args)
    endpoint = UdisksSource().mounted_at(args.mountpoint)
    if endpoint is None:
        print(f"error: {args.mountpoint} is not the mount point of a filesystem udisks knows "
              "of (is the drive plugged in and mounted?)", file=sys.stderr)
        return 1
    entry = identify_entry(cfg, endpoint, args.mountpoint)
    if args.json:
        _print_json(entry)
        return 0
    print(f"{entry['mountpoint']}: {entry['description']}")
    print(f"  fs_uuid: {entry['fs_uuid'] or '-'}")
    print(f"  drive_serial: {entry['drive_serial'] or '-'}")
    if entry["problem"]:
        print(f"  cannot be named by fs_uuid and drive_serial: {entry['problem']}")
    for d in entry["devices"]:
        print(f"  device: {d['name']}" + (f" [{d['status']}]" if d["status"] != "active" else "")
              + _from(cfg, "device", d["name"]))
    if not entry["devices"]:
        print("  device: none (unnamed)")
    if entry["tags"]:
        print(f"  tags: {', '.join(entry['tags'])}")
    return 0


@_reloads
def cmd_devices_add(args):
    """``udevmon devices add NAME``: add a device, from ``--match`` or ``--from-mountpoint``."""
    description = args.description or ""
    if args.from_mountpoint is not None:
        from .sources.udisks import UdisksSource
        cfg = _load(args)
        endpoint = UdisksSource().mounted_at(args.from_mountpoint)
        match = _guard(match_from_mount, cfg, endpoint, args.from_mountpoint)
        if args.description is None:
            description = describe(endpoint)
    else:
        match = _guard(_parse_matches, args.match, allow_lists=False)
    variables = _guard(_parse_vars, args.var)
    _guard(configedit.add_device, args.config, args.name, match, description,
           args.tag or [], exclude_tags=args.exclude_tag or [], variables=variables,
           file=args.file)
    print(f"Added device {args.name!r} ({strength(match)})"
          + (f": {match}." if args.from_mountpoint is not None else "."))
    return 0


@_reloads
def cmd_devices_remove(args):
    """``udevmon devices remove NAME``: remove a device (refused while actions target it)."""
    _guard(configedit.remove_device, args.config, args.name)
    print(f"Removed device {args.name!r}.")
    return 0


def _say_changed(changed, names, done, already) -> None:
    if changed:
        print(f"{done}: {', '.join(changed)}.")
    same = [n for n in dict.fromkeys(names) if n not in changed]
    if same:
        print(f"Already {already}: {', '.join(same)}.")


@_reloads
def cmd_devices_disable(args):
    """``udevmon devices disable NAME...``: set ``status = "disabled"``."""
    changed = _guard(configedit.set_device_status, args.config, args.names, "disabled")
    _say_changed(changed, args.names, "Disabled", "disabled")
    return 0


@_reloads
def cmd_devices_ignore(args):
    """``udevmon devices ignore NAME...``: set ``status = "ignored"``."""
    changed = _guard(configedit.set_device_status, args.config, args.names, "ignored")
    _say_changed(changed, args.names, "Ignored", "ignored")
    return 0


@_reloads
def cmd_devices_enable(args):
    """``udevmon devices enable NAME...``: make devices active again."""
    changed = _guard(configedit.set_device_status, args.config, args.names, None)
    _say_changed(changed, args.names, "Enabled", "enabled")
    return 0


@_reloads
def cmd_actions_disable(args):
    """``udevmon actions disable NAME...``: set ``enabled = false``."""
    changed = _guard(configedit.set_action_enabled, args.config, args.names, False)
    _say_changed(changed, args.names, "Disabled", "disabled")
    return 0


@_reloads
def cmd_actions_enable(args):
    """``udevmon actions enable NAME...``: enable actions again."""
    changed = _guard(configedit.set_action_enabled, args.config, args.names, True)
    _say_changed(changed, args.names, "Enabled", "enabled")
    return 0


@_reloads
def cmd_actions_set(args):
    """``udevmon actions set NAME``: change an action's options, targets or command."""
    if _retry_problem(args):
        return 1
    on = None
    if args.on is not None:
        on = [k.strip() for k in args.on.split(",") if k.strip()]
        bad = [k for k in on if k not in VALID_KINDS]
        if bad or not on:
            print(f"error: events must be from {sorted(VALID_KINDS)}", file=sys.stderr)
            return 1
    if args.timeout is not None and args.timeout <= 0:
        print("error: --timeout must be greater than 0", file=sys.stderr)
        return 1
    targets = _guard(configedit.set_action_options, args.config, args.name,
                     unnamed=args.unnamed, on=on, timeout=args.timeout, run=args.cmd or None,
                     tag_mode=args.tag_mode, detach=args.detach, lock=args.lock,
                     retries=args.retries, retry_delay=args.retry_delay,
                     retry_backoff=args.retry_backoff, order=args.order,
                     set_env=_guard(_parse_env, args.env), unset_env=args.unset_env,
                     cwd="" if args.no_cwd else args.cwd,
                     notify_on_failure=args.notify,
                     add_devices=args.add_device, remove_devices=args.remove_device,
                     add_tags=args.add_tag, remove_tags=args.remove_tag)
    retargeted = any((args.add_device, args.remove_device, args.add_tag, args.remove_tag))
    if (retargeted and targets is None and not args.cmd
            and (args.unnamed, args.on, args.timeout, args.tag_mode, args.detach, args.lock,
                 args.retries, args.retry_delay, args.retry_backoff, args.order,
                 args.notify, args.env, args.cwd) == (None,) * 13
            and not args.unset_env and not args.no_cwd):
        print(f"Action {args.name!r} already targets that; nothing changed.")
        return 0
    print(f"Updated action {args.name!r}.")
    if targets is not None:
        kind = "devices" if (args.add_device or args.remove_device) else "tags"
        print(f"  It now targets {kind}: {', '.join(targets)}.")
    if args.cmd:
        print(f"  Its command is now: {' '.join(args.cmd)}")
    return 0


def _say_renamed(kind: str, args, report) -> None:
    print(f"Renamed {kind} {args.old!r} to {args.new!r}.")
    if report.updated:
        print(f"  Updated: {', '.join(report.updated)}.")
    if report.generated:
        print("  Actions without a name of their own are called after their target, so these "
              "changed name:")
        for was, now in report.generated:
            print(f"    {was} -> {now}")
    if report.mentions or report.elsewhere:
        mentions = [("line", n, line) for n, line in report.mentions] + \
            [(f"{label} line", n, line) for label, n, line in report.elsewhere]
        print(f"  {args.old!r} still appears in the file{'s' if report.elsewhere else ''} "
              "(comments, descriptions, commands); left as they are:")
        for where, number, line in mentions[:8]:
            print(f"    {where} {number}: {line[:70]}")
        if len(mentions) > 8:
            print(f"    ... and {len(mentions) - 8} more")


@_reloads
def cmd_devices_rename(args):
    """``udevmon devices rename OLD NEW``, and every reference to it."""
    _say_renamed("device", args, _guard(configedit.rename_device, args.config, args.old, args.new))
    return 0


@_reloads
def cmd_tags_rename(args):
    """``udevmon tags rename OLD NEW``, and every reference to it."""
    _say_renamed("tag", args, _guard(configedit.rename_tag, args.config, args.old, args.new))
    return 0


@_reloads
def cmd_actions_rename(args):
    """``udevmon actions rename OLD NEW``."""
    _say_renamed("action", args, _guard(configedit.rename_action, args.config, args.old, args.new))
    return 0


@_reloads
def cmd_devices_set(args):
    """``udevmon devices set NAME``: change a device's match, description or variables."""
    match = _guard(_parse_matches, args.match, allow_lists=False) if args.match else None
    _guard(configedit.set_device_fields, args.config, args.name, match=match,
           description=args.description, set_vars=_guard(_parse_vars, args.var),
           unset_vars=args.unset_var)
    print(f"Updated device {args.name!r}" + (f" ({strength(match)})." if match else "."))
    return 0


@_reloads
def cmd_devices_tag(args):
    """``udevmon devices tag NAME TAG...``: add explicit tags."""
    _guard(configedit.set_device_tags, args.config, args.name, add=args.tags)
    print(f"Tagged {args.name!r}: {', '.join(args.tags)}.")
    return 0


@_reloads
def cmd_devices_exclude(args):
    """``udevmon devices exclude NAME TAG...``: opt the device out of rule tags."""
    _guard(configedit.set_device_exclusions, args.config, args.name, add=args.tags)
    print(f"{args.name!r} now opts out of the rule tag(s): {', '.join(args.tags)}.")
    return 0


@_reloads
def cmd_devices_include(args):
    """``udevmon devices include NAME TAG...``: undo ``exclude``."""
    _guard(configedit.set_device_exclusions, args.config, args.name, remove=args.tags)
    print(f"{args.name!r} no longer opts out of: {', '.join(args.tags)}.")
    return 0


@_reloads
def cmd_devices_untag(args):
    """``udevmon devices untag NAME TAG...``: remove explicit tags."""
    _guard(configedit.set_device_tags, args.config, args.name, remove=args.tags)
    print(f"Removed tag(s) {', '.join(args.tags)} from {args.name!r}.")
    return 0


def _rule_text(rule) -> str:
    return "  ".join(f"{k}={'|'.join(v)}" for k, v in rule.items()) or "(no rule; explicit members only)"


def cmd_tags_list(args):
    """``udevmon tags list``: every tag, declared or only used (``--json``:
    :func:`~udevmon.jsonout.tag_entry` each)."""
    cfg = _load(args)
    names = sorted(cfg.all_tag_names())
    if args.json:
        _print_json([jsonout.tag_entry(cfg, n) for n in names])
        return 0
    if not names:
        print("No tags.")
    for name in names:
        tag = cfg.tags.get(name)
        members = [d.name for d in cfg.devices.values() if name in d.tags]
        n_actions = sum(1 for x in cfg.actions if name in x.tags)
        print(f"{name:<20} {_rule_text(tag.match) if tag else '(explicit members only)'}"
              f"  members: {', '.join(members) or '-'}  ({n_actions} action(s))"
              + _from(cfg, "tag", name))
    return 0


def cmd_tags_show(args):
    """``udevmon tags show NAME``: one tag, its rule, members, variables and actions."""
    cfg = _load(args, must_exist=True)
    if args.name not in cfg.all_tag_names():
        print(f"error: no such tag {args.name!r}", file=sys.stderr)
        return 1
    if args.json:
        _print_json(jsonout.tag_entry(cfg, args.name))
        return 0
    tag = cfg.tags.get(args.name)
    print(f"{args.name}: {tag.description if tag else ''}")
    _file_line(cfg, "tag", args.name)
    print(f"  rule: {_rule_text(tag.match) if tag else '(none; explicit members only)'}"
          + (f"  ({strength(tag.match, wild=True)})" if tag and tag.match else ""))
    print(f"  explicit members: {', '.join(d.name for d in cfg.devices.values() if args.name in d.tags) or '-'}")
    for key, value in (tag.vars if tag else {}).items():
        print(f"  var {key} = {value!r}")
    for x in cfg.actions:
        if args.name in x.tags:
            allow = {True: "allow_unnamed", False: "named only", None: "named only (not stated)"}
            print(f"  action {x.name}: on {list(x.on)} {allow[x.allow_unnamed]}")
    return 0


@_reloads
def cmd_tags_add(args):
    """``udevmon tags add NAME``: declare a tag, with a rule from ``--match`` or none."""
    rule = _guard(_parse_matches, args.match or [], allow_lists=True)
    _guard(configedit.add_tag, args.config, args.name, rule, args.description or "",
           file=args.file)
    print(f"Added tag {args.name!r}" + (f" with a rule ({strength(rule, wild=True)})." if rule else "."))
    return 0


@_reloads
def cmd_tags_set(args):
    """``udevmon tags set NAME``: change a declared tag's rule, description or variables."""
    rule = _guard(_parse_matches, args.match, allow_lists=True) if args.match else None
    _guard(configedit.set_tag_fields, args.config, args.name, match=rule,
           clear_rule=args.no_rule, description=args.description,
           set_vars=_guard(_parse_vars, args.var), unset_vars=args.unset_var)
    print(f"Updated tag {args.name!r}"
          + (f" (rule {strength(rule, wild=True)})." if rule else ".")
          + (" It has no rule now: only devices that carry it belong to it." if args.no_rule
             else ""))
    return 0


@_reloads
def cmd_tags_remove(args):
    """``udevmon tags remove NAME``: remove a declared tag (refused while it is used)."""
    _guard(configedit.remove_tag, args.config, args.name)
    print(f"Removed tag {args.name!r}.")
    return 0


def cmd_actions_list(args):
    """``udevmon actions list``: every action, in the order they run (``--json``: in
    file order, :func:`~udevmon.jsonout.action_entry` each)."""
    cfg = _load(args)
    if args.json:                                # in file order, as `order` ties are
        _print_json([jsonout.action_entry(cfg, x) for x in cfg.actions])
        return 0
    for x in sorted(cfg.actions, key=lambda a: a.order):     # the order they run in
        target = (f"device:{','.join(x.devices)}" if x.devices
                  else f"tag:{('+' if x.tag_mode == 'all' else ',').join(x.tags)}")
        flag = {True: " [allow_unnamed]", False: " [named only]", None: ""}[x.allow_unnamed]
        flag += " [all tags]" if x.tag_mode == "all" else ""
        flag += " [detach]" if x.detach else ""
        flag += f" [lock: {x.lock}]" if x.lock else ""
        flag += f" [retries: {x.retries}]" if x.retries else ""
        flag += f" [backoff: {x.retry_backoff:g}]" if x.retries and x.retry_backoff != 1 else ""
        flag += f" [cwd: {x.cwd}]" if x.cwd else ""
        flag += f" [env: {', '.join(x.env)}]" if x.env else ""
        flag += f" [order: {x.order}]" if x.order != cfgmod.DEFAULT_ORDER else ""
        flag += " [notify on failure]" if x.notify_on_failure else ""
        off = " [disabled]" if not x.enabled else ""
        print(f"{x.name:<24} {target:<22} on {','.join(x.on):<24} {list(x.run)}{flag}{off}"
              + _from(cfg, "action", x.name))
    if not cfg.actions:
        print("No actions.")
    return 0


def _retry_problem(args) -> bool:
    """Print and return True when --retries / --retry-delay / --retry-backoff are out of
    range."""
    if args.retries is not None and args.retries < 0:
        print("error: --retries must be 0 or more", file=sys.stderr)
        return True
    if args.retry_delay is not None and args.retry_delay <= 0:
        print("error: --retry-delay must be greater than 0", file=sys.stderr)
        return True
    if args.retry_backoff is not None and args.retry_backoff < 1:
        print("error: --retry-backoff must be 1 or more", file=sys.stderr)
        return True
    if ((args.retries or args.retry_delay is not None or args.retry_backoff is not None)
            and getattr(args, "detach", None) is True):
        print("error: --retries, --retry-delay and --retry-backoff have no meaning with "
              "--detach", file=sys.stderr)
        return True
    return False


@_reloads
def cmd_actions_add(args):
    """``udevmon actions add --on EVENTS (--device|--tag) ... -- COMMAND``: add an action."""
    on = [k.strip() for k in args.on.split(",") if k.strip()]
    bad = [k for k in on if k not in VALID_KINDS]
    if bad or not on:
        print(f"error: events must be from {sorted(VALID_KINDS)}", file=sys.stderr)
        return 1
    if bool(args.device) == bool(args.tag):
        print("error: give exactly one of --device NAME or --tag NAME (repeatable)",
              file=sys.stderr)
        return 1
    if args.allow_unnamed is not None and not args.tag:
        print("error: --allow-unnamed / --named-only only apply with --tag", file=sys.stderr)
        return 1
    if not args.cmd:
        print("error: no command given (put it after --)", file=sys.stderr)
        return 1
    if args.tag_mode and not args.tag:
        print("error: --all-tags only applies with --tag", file=sys.stderr)
        return 1
    if args.lock is not None and not cfgmod.NAME_RE.match(args.lock):
        print("error: a lock name may contain only letters, digits, _ . -", file=sys.stderr)
        return 1
    if _retry_problem(args):
        return 1
    if args.detach and args.timeout is not None:
        print("error: --timeout has no meaning with --detach (a detached command is not "
              "waited for)", file=sys.stderr)
        return 1
    if args.detach and args.notify_on_failure:
        print("error: --notify-on-failure has no meaning with --detach (a detached command's "
              "result is not waited for)", file=sys.stderr)
        return 1
    name = _guard(configedit.add_action, args.config, args.name, args.device or (),
                  args.tag or (), on, args.cmd, args.timeout, args.allow_unnamed, args.tag_mode,
                  args.detach, args.lock, args.retries or 0, args.retry_delay,
                  file=args.file, order=args.order, notify_on_failure=args.notify_on_failure,
                  retry_backoff=args.retry_backoff, env=_guard(_parse_env, args.env),
                  cwd=args.cwd)
    print(f"Added action {name!r}.")
    return 0


@_reloads
def cmd_actions_remove(args):
    """``udevmon actions remove NAME``."""
    _guard(configedit.remove_action, args.config, args.name)
    print(f"Removed action {args.name!r}.")
    return 0


def cmd_edit(args):
    """``udevmon edit [FILE]``: change the main file or a drop-in in ``$EDITOR``; the
    whole configuration is checked before it is saved (:mod:`udevmon.edit`)."""
    from . import edit
    main = Path(args.config)
    target = main if args.file is None else _guard(cfgmod.dropin_path, main, args.file, "edit")
    ask = input if sys.stdin.isatty() else None
    rc, saved = edit.edit_file(main, target, ask=ask)
    if saved:
        _reload_after_edit(args)
    return rc


def cmd_check(args):
    """``udevmon check``: load the configuration and report problems; 1 on errors."""
    main = Path(args.config)
    try:
        cfg = cfgmod.load(args.config, must_exist=True)
    except cfgmod.ConfigError as e:
        if args.json:
            _print_json({"ok": False, "errors": list(e.problems), "warnings": [], "notes": [],
                         "findings": [jsonout.finding_entry(main, f) for f in e.findings],
                         "files": [], "devices": 0, "tags": 0, "actions": 0})
            return 1
        for p in e.problems:
            print(f"error: {p}")
        return 1
    found = [*(f for f in cfg.findings if f.level == "warning"),
             *cfgmod.command_problem_findings(cfg),
             *(f for f in cfg.findings if f.level == "note"),
             *cfgmod.command_note_findings(cfg)]
    warnings = [f.message for f in found if f.level == "warning"]
    notes = [f.message for f in found if f.level == "note"]
    files = jsonout.file_entries(cfg)
    if args.json:
        _print_json({"ok": True, "errors": [], "warnings": warnings, "notes": notes,
                     "findings": [jsonout.finding_entry(cfg.main, f) for f in found],
                     "files": files, "devices": len(cfg.devices), "tags": len(cfg.tags),
                     "actions": len(cfg.actions)})
        return 0
    for w in warnings:
        print(f"warning: {w}")
    for n in notes:
        print(f"note: {n}")
    if any(f != cfg.main for f in cfg.files):
        print("Files, in the order they are read:")
        for f, entry in zip(cfg.files, files):
            print(f"  {cfgmod.file_label(cfg.main, f)}: {entry['devices']} device(s), "
                  f"{entry['tags']} tag rule(s), {entry['actions']} action(s)")
    print(f"OK: {len(cfg.devices)} device(s), {len(cfg.tags)} tag rule(s), "
          f"{len(cfg.actions)} action(s)")
    return 0


def cmd_service_uninstall(args):
    """``udevmon service uninstall``: stop and remove the systemd user unit, if it is ours."""
    from .service import uninstall
    state, unit = uninstall(args.dest)
    if state == "removed":
        print(f"Removed {unit}" if args.dest else f"Stopped and removed {unit}")
    elif state == "absent":
        print(f"Nothing to remove: {unit} does not exist.")
    else:
        print(f"error: {unit} does not look like a udevmon unit; not removing it.",
              file=sys.stderr)
        return 1
    return 0


def cmd_completion_uninstall(args):
    """``udevmon completion uninstall [--shell SHELL]``: remove that shell's completion
    script, if it is ours."""
    from .completion import default_install_path, is_ours
    dest = args.dest or default_install_path(args.shell)
    if not dest.exists():
        print(f"Nothing to remove: {dest} does not exist.")
        return 0
    if not is_ours(dest.read_text(errors="replace"), args.shell):
        print(f"error: {dest} is not a udevmon completion script; not removing it.",
              file=sys.stderr)
        return 1
    dest.unlink()
    print(f"Removed {dest}")
    return 0


def cmd_completion_print(args):
    """``udevmon completion bash|zsh|fish``: print that shell's completion script."""
    from .completion import SCRIPTS
    sys.stdout.write(SCRIPTS[args.sub])
    return 0


def cmd_completion_install(args):
    """``udevmon completion install [--shell SHELL]``: write the completion script where the
    shell finds it."""
    from .completion import SCRIPTS, default_install_path
    dest = args.dest or default_install_path(args.shell)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(SCRIPTS[args.shell])
    print(f"Wrote {dest}")
    if args.dest is None:
        if args.shell == "zsh":
            print(f"zsh loads it when {dest.parent} is in fpath: add\n"
                  f"  fpath=({dest.parent} $fpath)\n"
                  "to ~/.zshrc before compinit if it is not, then open a new terminal.")
        elif args.shell == "fish":
            print("Open a new terminal to use it.")
        else:
            print("Open a new terminal (or run: source " + str(dest) + ") to use it.")
    return 0


def _complete_words(args: list[str]) -> int:
    """Hidden helper for the shell function: `udevmon _complete CWORD WORD...`."""
    from .completion import complete
    try:
        mode, candidates = complete(build_parser(), args[1:], int(args[0]))
    except Exception:                    # a broken completion must stay invisible
        return 0
    print("\n".join([mode, *candidates]))
    return 0


#: What ``udevmon version --json`` lists, so scripts test for a capability instead of
#: comparing version numbers. A name is added when its capability lands and never reused.
FEATURES = ("dropins", "order", "lock", "retries", "notify-on-failure", "vars",
            "from-mountpoint", "trigger", "trigger-wait", "trigger-json", "trigger-exit-codes",
            "present-json", "list-json", "check-json", "explain-json", "identify",
            "edit-dry-run", "version-json", "env-script-dir", "env-state-root", "edit",
            "retry-backoff", "reload-result", "max-workers-commands", "shutdown-retries",
            "trigger-output", "dropin-script-dir", "action-program",
            "apply", "status",
            "action-env-cwd", "disconnect-surprise", "check-json-findings")


def cmd_version(args):
    """``udevmon version``: the version and :data:`FEATURES`."""
    if args.json:
        _print_json({"version": __version__, "features": list(FEATURES)})
    else:
        print(__version__)
        print("features: " + " ".join(FEATURES))
    return 0


STATUS_NOT_RUNNING = 3      #: ``status`` exit status when no daemon is running
STATUS_NO_ANSWER = 7        #: ``status`` exit status when the daemon gives no usable answer


def cmd_status(args):
    """``udevmon status``: whether the daemon runs, with what, and each action's last
    result since it started (``--json``: one object); 3 when none runs, 7 when it
    does not answer."""
    from datetime import datetime

    from . import control
    from .daemon import running_daemon
    found = running_daemon()
    reply: dict = {"running": found is not None, "pid": found[0] if found else None}
    rc = 0
    if found is None:
        rc = STATUS_NOT_RUNNING
    else:
        try:
            with control.Connection() as conn:
                reply.update(conn.call("status"))
                reply["results"] = conn.call("results")["results"]
        except (control.NotRunning, control.ProtocolError, control.Refused, OSError,
                KeyError, TypeError) as e:
            rc = STATUS_NO_ANSWER
            reply["message"] = f"no usable answer from the daemon: {e}"
    if args.json:
        _print_json({"exit_status": rc, "results": [], **reply})
        return rc
    if found is None:
        print("udevmon is not running.")
        return rc
    if rc:
        print(f"udevmon is running (pid {found[0]}) but gives no usable answer: "
              f"{reply['message']}")
        return rc
    print(f"udevmon {reply['version']} is running (pid {reply['pid']}"
          + (", dry run" if reply["dry_run"] else "") + f") with {reply['config']}: "
          f"{reply['devices']} device(s), {reply['actions']} action(s).")
    if reply["paused"]:
        from . import pause
        print(f"Automation is paused {pause.describe_until(reply['until'])}.")
    if not reply["results"]:
        print("No action has run since it started.")
        return rc
    print("Last result of each action (newest first):")
    for r in reply["results"]:
        when = datetime.fromtimestamp(r["time"]).strftime("%Y-%m-%d %H:%M:%S")
        what = f"exit status {r['status']}" if r["status"] is not None else r["failure"]
        tries = f" after {r['attempts']} attempts" if r["attempts"] > 1 else ""
        print(f"  {r['action']:<24} {when}  {r['event']}"
              + (f" {r['device']}" if r["device"] else "") + f": {what}{tries}")
        if r["status"] != 0:                      # a failure: why, in its last lines
            for line in r["output"][-3:]:
                print(f"      {line}")
    return rc


def cmd_reload(args):
    """``udevmon reload``: have the running daemon reread its configuration; 1 when none is
    running or the configuration has errors (the daemon then keeps the previous one)."""
    from . import control
    from .daemon import reload_running
    try:
        result = reload_running()
    except control.Refused as e:
        print(f"error: {e.message}:", file=sys.stderr)
        for problem in e.data.get("problems", []):
            print(f"  {problem}", file=sys.stderr)
        return 1
    if result is None:
        print("udevmon is not running.", file=sys.stderr)
        return 1
    if result["via"] == "signal":
        print("Sent reload to the running daemon (it gives no answer: restart it to use "
              "its control socket).")
        return 0
    for warning in result.get("warnings", []):
        print(f"warning: {warning}", file=sys.stderr)
    print(f"Reloaded the running daemon: {result['devices']} device(s), "
          f"{result['actions']} action(s).")
    if result.get("restart_needed"):
        print("udev_filters changed: restart the daemon to apply them.")
    return 0


# `udevmon trigger` exit statuses (2 is argparse's usage error).
TRIGGER_FAILED = 1          #: ``trigger`` status when an action failed, timed out or gave no status
TRIGGER_NOT_RUNNING = 3     #: ``trigger`` status when no daemon is running
TRIGGER_UNKNOWN_DEVICE = 4  #: ``trigger`` status when the running daemon has no such device
TRIGGER_OTHER_CONFIG = 5    #: ``trigger`` status when ``--config`` names another file than the daemon's
TRIGGER_REFUSED = 6         #: ``trigger`` status when refused (not present, ignored, wrong event, paused)
TRIGGER_NO_ANSWER = 7       #: ``trigger`` status when there is no usable answer from the daemon


def _trigger_reply(args) -> tuple[int, dict]:
    """Ask the running daemon to trigger; (exit status, reply). A failure before the
    daemon answers is a reply too, with ``ok`` false and an ``error`` code."""
    from . import control
    from .daemon import _same_file, running_daemon

    def fail(status, error, message):
        return status, {"ok": False, "error": error, "message": message}
    found = running_daemon()
    if found is None:
        return fail(TRIGGER_NOT_RUNNING, "not-running",
                    "udevmon is not running (start it, or run `udevmon run`)")
    _, its_config = found
    if args.config_explicit and its_config and not _same_file(args.config, its_config):
        return fail(TRIGGER_OTHER_CONFIG, "other-config",
                    f"the running daemon uses {its_config}, not {args.config}")
    try:
        reply = control.call("trigger", {"device": args.device, "event": args.event,
                                         "wait": args.wait},
                             timeout=None if args.wait else 30.0)
    except control.NotRunning:
        return fail(TRIGGER_NOT_RUNNING, "not-running",
                    "udevmon is not running (no answer on its control socket)")
    except control.Refused as e:
        if e.code != control.REFUSED:                 # no answer in time, or a failure
            status = TRIGGER_NO_ANSWER
        else:
            status = TRIGGER_UNKNOWN_DEVICE if e.error == "unknown-device" else TRIGGER_REFUSED
        return status, {"ok": False, "error": e.error, "message": e.message}
    except (OSError, ValueError) as e:            # ProtocolError is a ValueError
        return fail(TRIGGER_NO_ANSWER, "no-answer", f"no usable answer from the daemon: {e}")
    if not isinstance(reply, dict):
        return fail(TRIGGER_NO_ANSWER, "no-answer", "no usable answer from the daemon")
    statuses = [{"action": x["action"], "status": x["status"], "failure": x.get("failure"),
                 "output": x.get("output", [])} for x in reply.get("statuses", [])]
    failed = any(x["status"] != 0 for x in statuses)
    return (TRIGGER_FAILED if failed else 0), {
        "ok": True, "error": None, "message": None, "event": reply["event"],
        "device": reply["device"],
        "things": [{"thing": t["thing"], "actions": t["actions"],
                    "skipped": [{"action": n, "reason": r} for n, r in t["skipped"]]}
                   for t in reply["things"]],
        "statuses": statuses if args.wait else None}


def cmd_trigger(args):
    """``udevmon trigger DEVICE EVENT``: have the running daemon fire an event for a present
    device; the exit status is one of the ``TRIGGER_*`` values, 0 or 2 (usage)."""
    rc, reply = _trigger_reply(args)
    if args.json:
        _print_json({"exit_status": rc, **reply})
        return rc
    if not reply["ok"]:
        print(f"error: {reply['message']}", file=sys.stderr)
        return rc
    for thing in reply["things"]:
        ran = ", ".join(thing["actions"]) or "nothing to run"
        print(f"{reply['event']} for {reply['device']} ({thing['thing']}): {ran}")
        for skip in thing["skipped"]:
            print(f"  skipped {skip['action']}: {skip['reason']}")
    for x in reply["statuses"] or []:
        if x["status"] is None:
            print(f"  {x['action']}: no exit status ("
                  + (x["failure"] or "timed out, could not start, or detached; see the log")
                  + ")")
        else:
            print(f"  {x['action']}: exit status {x['status']}")
        if x["status"] != 0 and x["output"]:
            print("    its last output:")
            for line in x["output"]:
                print(f"      {line}")
    if args.wait and not reply["statuses"]:
        print("Nothing ran.")
    return rc


def cmd_service_install(args):
    """``udevmon service install``: write the systemd user unit (and start it with ``--now``)."""
    from .service import install
    unit = install(args.dest, args.now, args.config if args.config_explicit else None)
    print(f"Wrote {unit}")
    if args.dest is None and not args.now:
        print("Enable it with: systemctl --user enable --now udevmon.service")
    return 0


# -- parser ------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    """The argument parser. Building it has no side effects (the docs use it)."""
    p = argparse.ArgumentParser(prog="udevmon", description=__doc__ or "")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", type=Path, default=None, help="config file path")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd_name", required=True)
    edits = argparse.ArgumentParser(add_help=False)          # shared by the editing commands
    edits.add_argument("--no-reload", action="store_true",
                       help="don't tell the running daemon to reload")
    edits.add_argument("--dry-run", action="store_true",
                       help="show the change as a diff and write nothing")
    adds = argparse.ArgumentParser(add_help=False, parents=[edits])     # the `add` commands
    js = argparse.ArgumentParser(add_help=False)             # the commands with --json
    js.add_argument("--json", action="store_true", help="print JSON for scripts")
    adds.add_argument("--file", metavar="DROPIN",
                      help="write to this drop-in (config.d/NAME.toml) instead of the main file")

    s = sub.add_parser("run", help="run the daemon")
    s.add_argument("--dry-run", action="store_true", help="log actions instead of running them")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("scan", help="watch events and name devices")
    s.add_argument("--no-prompt", action="store_true", help="only print events")
    s.add_argument("--present", action="store_true",
                   help="also offer devices and filesystems that are already present")
    s.add_argument("--all-props", action="store_true",
                   help="print every property, including the raw udev_* ones")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("trigger", help="run a present device's actions now, as for an event")
    s.add_argument("device", metavar="DEVICE")
    s.add_argument("event", nargs="?", default="startup", metavar="EVENT",
                   choices=TRIGGER_KINDS,
                   help="startup (default), shutdown, connected, mounted or unmounted")
    s.add_argument("--wait", action="store_true",
                   help="wait for the actions to finish and show their exit status")
    s.add_argument("--json", action="store_true",
                   help="print the result as JSON (with --wait, each action's exit status)")
    s.set_defaults(fn=cmd_trigger)

    s = sub.add_parser("present", help="list USB devices and filesystems that are present now")
    s.add_argument("--all", action="store_true", help="include system filesystems")
    s.add_argument("--json", action="store_true",
                   help="print a JSON list for scripts (every property, devices and tags)")
    s.add_argument("--all-props", action="store_true",
                   help="print every property, including the raw udev_* ones")
    s.set_defaults(fn=cmd_present)

    dev = sub.add_parser("devices", help="manage named devices")
    g = dev.add_subparsers(dest="sub", required=False)
    x = g.add_parser("list", help="list named devices", parents=[js])
    x.set_defaults(fn=cmd_devices_list)
    x = g.add_parser("show", parents=[js]); x.add_argument("name")
    x.set_defaults(fn=cmd_devices_show)
    x = g.add_parser("identify", parents=[js],
                     help="what udevmon knows about the filesystem mounted at a path")
    x.add_argument("mountpoint", metavar="MOUNTPOINT")
    x.set_defaults(fn=cmd_devices_identify)
    x = g.add_parser("add", parents=[adds])
    x.add_argument("name")
    grp = x.add_mutually_exclusive_group(required=True)
    grp.add_argument("--match", action="append", metavar="KEY=VALUE")
    grp.add_argument("--from-mountpoint", metavar="PATH",
                     help="match the filesystem mounted there by fs_uuid and drive_serial")
    x.add_argument("--tag", action="append", metavar="TAG", help="explicit tag (repeatable)")
    x.add_argument("--exclude-tag", action="append", metavar="TAG",
                   help="rule tag to opt out of (repeatable)")
    x.add_argument("--var", action="append", metavar="NAME=VALUE",
                   help="a variable for actions, used as {var_NAME} (repeatable)")
    x.add_argument("--description")
    x.set_defaults(fn=cmd_devices_add)
    x = g.add_parser("remove", parents=[edits]); x.add_argument("name"); x.set_defaults(fn=cmd_devices_remove)
    x = g.add_parser("tag", help="add explicit tags to a device", parents=[edits])
    x.add_argument("name"); x.add_argument("tags", nargs="+", metavar="TAG")
    x.set_defaults(fn=cmd_devices_tag)
    x = g.add_parser("untag", help="remove explicit tags from a device", parents=[edits])
    x.add_argument("name"); x.add_argument("tags", nargs="+", metavar="TAG")
    x.set_defaults(fn=cmd_devices_untag)
    x = g.add_parser("exclude", help="opt a device out of rule tags", parents=[edits])
    x.add_argument("name"); x.add_argument("tags", nargs="+", metavar="TAG")
    x.set_defaults(fn=cmd_devices_exclude)
    x = g.add_parser("include", help="stop opting a device out of rule tags", parents=[edits])
    x.add_argument("name"); x.add_argument("tags", nargs="+", metavar="TAG")
    x.set_defaults(fn=cmd_devices_include)
    x = g.add_parser("set", help="change a device's match keys or description", parents=[edits])
    x.add_argument("name")
    x.add_argument("--match", action="append", metavar="KEY=VALUE",
                   help="replace the match (repeat for several keys)")
    x.add_argument("--description", metavar="TEXT", help="new description (empty removes it)")
    x.add_argument("--var", action="append", metavar="NAME=VALUE",
                   help="set a variable (repeatable); the others stay")
    x.add_argument("--unset-var", action="append", default=[], metavar="NAME",
                   help="remove a variable (repeatable)")
    x.set_defaults(fn=cmd_devices_set)
    x = g.add_parser("rename", help="rename a device and everything that names it",
                     parents=[edits])
    x.add_argument("old"); x.add_argument("new"); x.set_defaults(fn=cmd_devices_rename)
    x = g.add_parser("disable", help="turn off the device's own actions (its tags' still run)",
                     parents=[edits])
    x.add_argument("names", nargs="+", metavar="NAME"); x.set_defaults(fn=cmd_devices_disable)
    x = g.add_parser("ignore", help="run nothing at all for these devices", parents=[edits])
    x.add_argument("names", nargs="+", metavar="NAME"); x.set_defaults(fn=cmd_devices_ignore)
    x = g.add_parser("enable", help="back to normal (clears disabled or ignored)",
                     parents=[edits])
    x.add_argument("names", nargs="+", metavar="NAME"); x.set_defaults(fn=cmd_devices_enable)
    dev.set_defaults(fn=cmd_devices_list)              # bare `udevmon devices` lists them

    tg = sub.add_parser("tags", help="manage tags and tag rules").add_subparsers(
        dest="sub", required=True)
    x = tg.add_parser("list", parents=[js]); x.set_defaults(fn=cmd_tags_list)
    x = tg.add_parser("show", parents=[js]); x.add_argument("name")
    x.set_defaults(fn=cmd_tags_show)
    x = tg.add_parser("add", help="declare a tag, optionally with a rule", parents=[adds])
    x.add_argument("name")
    x.add_argument("--match", action="append", metavar="KEY=VALUE",
                   help="rule pair; repeat a key to allow several values")
    x.add_argument("--description")
    x.set_defaults(fn=cmd_tags_add)
    x = tg.add_parser("set", help="change a tag's rule, description or variables",
                      parents=[edits])
    x.add_argument("name")
    grp = x.add_mutually_exclusive_group()
    grp.add_argument("--match", action="append", metavar="KEY=VALUE",
                     help="replace the rule; repeat a key to allow several values")
    grp.add_argument("--no-rule", action="store_true",
                     help="remove the rule (only devices that carry the tag belong to it)")
    x.add_argument("--description", metavar="TEXT", help="new description (empty removes it)")
    x.add_argument("--var", action="append", metavar="NAME=VALUE",
                   help="set a default variable for the things it tags (repeatable)")
    x.add_argument("--unset-var", action="append", default=[], metavar="NAME",
                   help="remove a variable (repeatable)")
    x.set_defaults(fn=cmd_tags_set)
    x = tg.add_parser("rename", help="rename a tag wherever it is used", parents=[edits])
    x.add_argument("old"); x.add_argument("new"); x.set_defaults(fn=cmd_tags_rename)
    x = tg.add_parser("remove", parents=[edits]); x.add_argument("name"); x.set_defaults(fn=cmd_tags_remove)

    a = sub.add_parser("actions", help="manage actions").add_subparsers(dest="sub", required=True)
    x = a.add_parser("list", parents=[js]); x.set_defaults(fn=cmd_actions_list)
    x = a.add_parser("add", help="actions add (--device N ... | --tag N ...) EVENT[,EVENT] "
                                 "[--name N] [--allow-unnamed|--named-only] -- CMD [ARG...]",
                     parents=[adds])
    x.add_argument("on", help="comma-separated: " + ", ".join(sorted(VALID_KINDS)))
    x.add_argument("--device", action="append", metavar="NAME", help="target device (repeatable)")
    x.add_argument("--tag", action="append", metavar="NAME", help="target tag (repeatable)")
    x.add_argument("--name")
    x.add_argument("--order", type=int, metavar="N",
                   help="within one event, lower runs first (default 50; ties: file order)")
    x.add_argument("--notify-on-failure", action="store_true",
                   help="show a desktop notification when it fails or times out")
    x.add_argument("--timeout", type=float)
    x.add_argument("--all-tags", dest="tag_mode", action="store_const", const="all",
                   help="with several --tag: run only for things that have all of them "
                        "(default: any of them)")
    x.add_argument("--detach", action="store_true",
                   help="start the command and do not wait for it (no timeout)")
    x.add_argument("--lock", metavar="NAME",
                   help="actions with the same lock name never run at the same time")
    x.add_argument("--retries", type=int, metavar="N",
                   help="run again after a non-zero exit, up to N more times")
    x.add_argument("--retry-delay", type=float, metavar="SECONDS",
                   help="seconds between attempts (default 1)")
    x.add_argument("--retry-backoff", type=float, metavar="FACTOR",
                   help="multiply the delay by FACTOR after each attempt (default 1)")
    x.add_argument("--env", action="append", metavar="NAME=VALUE",
                   help="set an environment variable for the command (repeatable)")
    x.add_argument("--cwd", metavar="DIR", help="the folder the command starts in "
                   "(default: your home folder)")
    grp = x.add_mutually_exclusive_group()
    grp.add_argument("--allow-unnamed", dest="allow_unnamed", action="store_const", const=True,
                     default=None, help="also run for unnamed things matching the tag")
    grp.add_argument("--named-only", dest="allow_unnamed", action="store_const", const=False,
                     help="run for named devices only (explicit)")
    x.set_defaults(fn=cmd_actions_add)
    x = a.add_parser("remove", parents=[edits]); x.add_argument("name"); x.set_defaults(fn=cmd_actions_remove)
    x = a.add_parser("rename", help="give an action a new name", parents=[edits])
    x.add_argument("old"); x.add_argument("new"); x.set_defaults(fn=cmd_actions_rename)
    x = a.add_parser("disable", help="stop actions running without deleting them", parents=[edits])
    x.add_argument("names", nargs="+", metavar="NAME"); x.set_defaults(fn=cmd_actions_disable)
    x = a.add_parser("enable", help="let disabled actions run again", parents=[edits])
    x.add_argument("names", nargs="+", metavar="NAME"); x.set_defaults(fn=cmd_actions_enable)
    x = a.add_parser("set", help="change an action's command, targets, events, timeout or allow_unnamed",
                     parents=[edits])
    x.add_argument("name")
    grp = x.add_mutually_exclusive_group()
    grp.add_argument("--allow-unnamed", dest="unnamed", action="store_const", const="allow",
                     help="also run for unnamed things matching the tag")
    grp.add_argument("--named-only", dest="unnamed", action="store_const", const="named-only",
                     help="run for named devices only")
    grp.add_argument("--default-unnamed", dest="unnamed", action="store_const", const="default",
                     help="clear the setting (named devices only, `check` will note it)")
    x.add_argument("--on", metavar="EVENTS", help="replace the events (comma-separated)")
    x.add_argument("--timeout", type=float, metavar="SECONDS")
    x.add_argument("--retries", type=int, metavar="N",
                   help="run again after a non-zero exit, up to N more times (0 removes it)")
    x.add_argument("--retry-delay", type=float, metavar="SECONDS",
                   help="seconds between attempts")
    x.add_argument("--retry-backoff", type=float, metavar="FACTOR",
                   help="multiply the delay by FACTOR after each attempt (1 removes it)")
    x.add_argument("--env", action="append", metavar="NAME=VALUE",
                   help="set an environment variable for the command (repeatable)")
    x.add_argument("--unset-env", action="append", default=[], metavar="NAME",
                   help="remove one (repeatable)")
    cw = x.add_mutually_exclusive_group()
    cw.add_argument("--cwd", metavar="DIR", help="the folder the command starts in")
    cw.add_argument("--no-cwd", action="store_true", help="start it in your home folder again")
    x.add_argument("--order", type=int, metavar="N",
                   help="within one event, lower runs first (50, the default, removes it)")
    nt = x.add_mutually_exclusive_group()
    nt.add_argument("--notify-on-failure", dest="notify", action="store_const", const=True,
                    help="show a desktop notification when it fails or times out")
    nt.add_argument("--no-notify-on-failure", dest="notify", action="store_const",
                    const=False, help="stop notifying")
    lk = x.add_mutually_exclusive_group()
    lk.add_argument("--lock", dest="lock", metavar="NAME", default=None,
                    help="actions with the same lock name never run at the same time")
    lk.add_argument("--no-lock", dest="lock", action="store_const", const="",
                    help="remove the action's lock")
    det = x.add_mutually_exclusive_group()
    det.add_argument("--detach", dest="detach", action="store_const", const=True, default=None,
                     help="start the command and do not wait for it (no timeout)")
    det.add_argument("--no-detach", dest="detach", action="store_const", const=False,
                     help="wait for the command again (the default)")
    mode = x.add_mutually_exclusive_group()
    mode.add_argument("--all-tags", dest="tag_mode", action="store_const", const="all",
                      help="run only for things that have all of the action's tags")
    mode.add_argument("--any-tags", dest="tag_mode", action="store_const", const="any",
                      help="run for things that have any of them (the default)")
    for verb, past in (("add", "to"), ("remove", "from")):
        for kind in ("device", "tag"):
            x.add_argument(f"--{verb}-{kind}", dest=f"{verb}_{kind}", action="append", default=[],
                           metavar="NAME", help=f"{verb} a {kind} {past} the action's targets "
                                                "(repeatable)")
    x.set_defaults(fn=cmd_actions_set, unnamed=None)

    s = sub.add_parser("explain", help="show what would run for a thing (present or simulated)")
    s.add_argument("event", nargs="?", choices=sorted(VALID_KINDS), metavar="EVENT",
                   help="only this event (default: every event that fits)")
    s.add_argument("--device", metavar="NAME", help="simulate this named device")
    s.add_argument("--prop", action="append", metavar="KEY=VALUE",
                   help="simulate a thing with this property (repeatable)")
    s.add_argument("--all", action="store_true",
                   help="also list system filesystems and things nothing matches")
    s.add_argument("--json", action="store_true", help="print the result as JSON")
    s.set_defaults(fn=cmd_explain)

    s = sub.add_parser("pause", help="stop every action until `resume` (or for a while)")
    s.add_argument("--for", dest="duration", metavar="DURATION",
                   help="how long: 30m, 2h, 1h30m, 90s, 1d")
    s.set_defaults(fn=cmd_pause)
    s = sub.add_parser("resume", help="let actions run again after a pause")
    s.set_defaults(fn=cmd_resume)

    s = sub.add_parser("edit", help="edit the config in $EDITOR, checked before it is saved")
    s.add_argument("file", nargs="?", metavar="FILE",
                   help="a drop-in (NAME, config.d/NAME.toml); default: the main file")
    s.add_argument("--no-reload", action="store_true",
                   help="don't tell the running daemon to reload")
    s.set_defaults(fn=cmd_edit)
    s = sub.add_parser("apply", help="make several edits as one change (JSON from FILE or "
                       "stdin)", parents=[edits])
    s.add_argument("file", nargs="?", metavar="FILE",
                   help="a JSON array of editing commands, each an array of strings "
                        "(default: standard input)")
    s.set_defaults(fn=cmd_apply)
    s = sub.add_parser("status", help="whether the daemon runs, and each action's last "
                       "result", parents=[js])
    s.set_defaults(fn=cmd_status)
    s = sub.add_parser("check", help="validate the config", parents=[js])
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("version", help="print the version and the features scripts can test",
                       parents=[js])
    s.set_defaults(fn=cmd_version)
    s = sub.add_parser("reload", help="tell the running daemon to reload"); s.set_defaults(fn=cmd_reload)

    sv = sub.add_parser("service", help="systemd user service").add_subparsers(
        dest="sub", required=True)
    x = sv.add_parser("install")
    x.add_argument("--now", action="store_true", help="also enable and start it")
    x.add_argument("--dest", type=Path, help="write the unit here instead (no systemctl)")
    x.set_defaults(fn=cmd_service_install)
    x = sv.add_parser("uninstall", help="stop, disable and remove the service")
    x.add_argument("--dest", type=Path, help="remove the unit from here instead (no systemctl)")
    x.set_defaults(fn=cmd_service_uninstall)

    co = sub.add_parser("completion", help="tab completion for bash, zsh and fish") \
        .add_subparsers(dest="sub", required=True)
    for shell in ("bash", "zsh", "fish"):
        x = co.add_parser(shell, help=f"print the {shell} completion script")
        x.set_defaults(fn=cmd_completion_print)
    shells = argparse.ArgumentParser(add_help=False)
    shells.add_argument("--shell", choices=("bash", "zsh", "fish"), default="bash",
                        help="the shell (default: bash)")
    x = co.add_parser("install", help="install it for your user (no root)", parents=[shells])
    x.add_argument("--dest", type=Path, help="write the script here instead")
    x.set_defaults(fn=cmd_completion_install)
    x = co.add_parser("uninstall", help="remove the installed script", parents=[shells])
    x.add_argument("--dest", type=Path, help="remove this file instead")
    x.set_defaults(fn=cmd_completion_uninstall)
    return p


def parse_command(parser, argv: list[str]):
    """Parse a command line (without the program name) as :func:`main` does: what comes
    after ``--`` is the command (``args.cmd``), and ``--config`` defaults to the usual
    file (``args.config_explicit`` says whether it was given)."""
    tail: list[str] = []
    if "--" in argv:                     # everything after -- is a command, verbatim
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    args = parser.parse_args(argv)
    args.cmd = tail
    args.config_explicit = args.config is not None
    if args.config is None:
        args.config = cfgmod.default_config_path()
    return args


def main(argv=None) -> int:
    """Run the command in ``argv`` (default ``sys.argv[1:]``) and return its exit status."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["_complete"]:        # not a documented command: see completion.py
        return _complete_words(argv[1:])
    args = parse_command(build_parser(), argv)
    _setup_logging(args.verbose)
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        return 130
