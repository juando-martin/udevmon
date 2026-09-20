"""Command-line interface."""

from __future__ import annotations

import argparse
import functools
import logging
import sys
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import configedit
from .events import VALID_KINDS, describe
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
    """Tell the running daemon (if it uses this config) to pick up the change."""
    if getattr(args, "no_reload", False):
        return
    from .daemon import reload_running
    if reload_running(args.config):
        print("Reloaded the running daemon.")


def _reloads(fn):
    """Run an editing command, then reload the daemon if it succeeded."""
    @functools.wraps(fn)
    def wrapper(args):
        rc = fn(args)
        if not rc:
            _reload_after_edit(args)
        return rc
    return wrapper


# -- commands ------------------------------------------------------------------
def cmd_run(args):
    from .daemon import Daemon
    return Daemon(args.config, args.dry_run).run()


def cmd_scan(args):
    from .scan import Scanner
    return Scanner(args.config, prompt=not args.no_prompt and sys.stdin.isatty(),
                   present=args.present).run()


def cmd_present(args):
    from .sources.udev import UdevSource
    from .sources.udisks import UdisksSource
    from .scan import format_props, status_text
    from .matching import default_match_keys
    cfg = _load(args)
    endpoints = UdevSource(cfg.settings.udev_filters).snapshot() \
        + UdisksSource().snapshot(include_system=args.all)
    for d in endpoints:
        keys = default_match_keys(d)
        level = strength(build_match(d, keys)) if keys else "-"
        print(f"{describe(d)}   [{level}]   {status_text(cfg, d)}")
        print(f"    {format_props(d)}")
    return 0


def _actions_for(cfg, device) -> list:
    """Actions that target the device by name or through one of its explicit tags."""
    return [x for x in cfg.actions
            if device.name in x.devices or set(x.tags) & set(device.tags)]


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


def cmd_explain(args):
    from . import explain
    cfg = _load(args)
    props = _guard(_parse_props, args.prop)
    if args.device is not None and args.device not in cfg.devices:
        print(f"error: no such device {args.device!r}", file=sys.stderr)
        return 1
    if args.device is not None or props:                     # a simulated thing
        endpoint = explain.simulated(cfg, args.device, props, args.event)
        shown = "  ".join(f"{k}={v}" for k, v in endpoint.props.items())
        lines = explain.explain_endpoint(cfg, endpoint, [args.event] if args.event else None,
                                         header=f"Simulated thing ({shown})")
        print("\n".join(lines))
        print("\nOnly the properties above are known; add --prop KEY=VALUE for others "
              "(for example fs_type=ext4).")
        return 0
    from .sources.udev import UdevSource
    from .sources.udisks import UdisksSource
    things = UdevSource(cfg.settings.udev_filters).snapshot() \
        + UdisksSource().snapshot(include_system=args.all)
    blocks, idle = [], 0
    for endpoint in things:
        if args.event and args.event not in explain.kinds_for(endpoint):
            continue
        res = resolve(cfg, endpoint.props)
        if not args.all and not res.named and not res.tags:
            idle += 1                    # matches no device or tag: nothing can ever run for it
            continue
        blocks.append("\n".join(explain.explain_endpoint(
            cfg, endpoint, [args.event] if args.event else None)))
    print("\n\n".join(blocks) if blocks else "Nothing present matches a device or a tag.")
    if idle:
        print(f"\nNot shown: {idle} present thing(s) that match no device or tag "
              "(--all lists them).")
    return 0


def cmd_devices_list(args):
    cfg = _load(args)
    if not cfg.devices:
        print("No devices. Run `udevmon scan` to create some.")
    for d in cfg.devices.values():
        tags = f"  tags: {', '.join(d.tags)}" if d.tags else ""
        print(f"{device_label(d):<20} {strength(d.match):<7} {d.match}{tags}  "
              f"({len(_actions_for(cfg, d))} action(s))")
    return 0


def cmd_devices_show(args):
    cfg = _load(args, must_exist=True)
    d = cfg.devices.get(args.name)
    if not d:
        print(f"error: no such device {args.name!r}", file=sys.stderr)
        return 1
    print(f"{d.name}: {d.description}\n  match: {d.match}  ({strength(d.match)})")
    if d.status != "active":
        print(f"  status: {d.status}")
    if d.tags:
        print(f"  tags: {', '.join(d.tags)}")
    for x in _actions_for(cfg, d):
        print(f"  action {x.name}: on {list(x.on)} run {list(x.run)} (timeout {x.timeout:g}s)")
    return 0


@_reloads
def cmd_devices_add(args):
    match = _guard(_parse_matches, args.match, allow_lists=False)
    _guard(configedit.add_device, args.config, args.name, match, args.description or "",
           args.tag or [])
    print(f"Added device {args.name!r} ({strength(match)}).")
    return 0


@_reloads
def cmd_devices_remove(args):
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
    changed = _guard(configedit.set_device_status, args.config, args.names, "disabled")
    _say_changed(changed, args.names, "Disabled", "disabled")
    return 0


@_reloads
def cmd_devices_ignore(args):
    changed = _guard(configedit.set_device_status, args.config, args.names, "ignored")
    _say_changed(changed, args.names, "Ignored", "ignored")
    return 0


@_reloads
def cmd_devices_enable(args):
    changed = _guard(configedit.set_device_status, args.config, args.names, None)
    _say_changed(changed, args.names, "Enabled", "enabled")
    return 0


@_reloads
def cmd_actions_disable(args):
    changed = _guard(configedit.set_action_enabled, args.config, args.names, False)
    _say_changed(changed, args.names, "Disabled", "disabled")
    return 0


@_reloads
def cmd_actions_enable(args):
    changed = _guard(configedit.set_action_enabled, args.config, args.names, True)
    _say_changed(changed, args.names, "Enabled", "enabled")
    return 0


@_reloads
def cmd_actions_set(args):
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
    _guard(configedit.set_action_options, args.config, args.name,
           unnamed=args.unnamed, on=on, timeout=args.timeout)
    print(f"Updated action {args.name!r}.")
    return 0


@_reloads
def cmd_devices_tag(args):
    _guard(configedit.set_device_tags, args.config, args.name, add=args.tags)
    print(f"Tagged {args.name!r}: {', '.join(args.tags)}.")
    return 0


@_reloads
def cmd_devices_untag(args):
    _guard(configedit.set_device_tags, args.config, args.name, remove=args.tags)
    print(f"Removed tag(s) {', '.join(args.tags)} from {args.name!r}.")
    return 0


def _rule_text(rule) -> str:
    return "  ".join(f"{k}={'|'.join(v)}" for k, v in rule.items()) or "(no rule; explicit members only)"


def cmd_tags_list(args):
    cfg = _load(args)
    names = sorted(cfg.all_tag_names())
    if not names:
        print("No tags.")
    for name in names:
        tag = cfg.tags.get(name)
        members = [d.name for d in cfg.devices.values() if name in d.tags]
        n_actions = sum(1 for x in cfg.actions if name in x.tags)
        print(f"{name:<20} {_rule_text(tag.match) if tag else '(explicit members only)'}"
              f"  members: {', '.join(members) or '-'}  ({n_actions} action(s))")
    return 0


def cmd_tags_show(args):
    cfg = _load(args, must_exist=True)
    if args.name not in cfg.all_tag_names():
        print(f"error: no such tag {args.name!r}", file=sys.stderr)
        return 1
    tag = cfg.tags.get(args.name)
    print(f"{args.name}: {tag.description if tag else ''}")
    print(f"  rule: {_rule_text(tag.match) if tag else '(none; explicit members only)'}"
          + (f"  ({strength(tag.match)})" if tag and tag.match else ""))
    print(f"  explicit members: {', '.join(d.name for d in cfg.devices.values() if args.name in d.tags) or '-'}")
    for x in cfg.actions:
        if args.name in x.tags:
            allow = {True: "allow_unnamed", False: "named only", None: "named only (not stated)"}
            print(f"  action {x.name}: on {list(x.on)} {allow[x.allow_unnamed]}")
    return 0


@_reloads
def cmd_tags_add(args):
    rule = _guard(_parse_matches, args.match or [], allow_lists=True)
    _guard(configedit.add_tag, args.config, args.name, rule, args.description or "")
    print(f"Added tag {args.name!r}" + (f" with a rule ({strength(rule)})." if rule else "."))
    return 0


@_reloads
def cmd_tags_remove(args):
    _guard(configedit.remove_tag, args.config, args.name)
    print(f"Removed tag {args.name!r}.")
    return 0


def cmd_actions_list(args):
    cfg = _load(args)
    for x in cfg.actions:
        target = f"device:{','.join(x.devices)}" if x.devices else f"tag:{','.join(x.tags)}"
        flag = {True: " [allow_unnamed]", False: " [named only]", None: ""}[x.allow_unnamed]
        off = " [disabled]" if not x.enabled else ""
        print(f"{x.name:<24} {target:<22} on {','.join(x.on):<24} {list(x.run)}{flag}{off}")
    if not cfg.actions:
        print("No actions.")
    return 0


@_reloads
def cmd_actions_add(args):
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
    name = _guard(configedit.add_action, args.config, args.name, args.device or (),
                  args.tag or (), on, args.cmd, args.timeout, args.allow_unnamed)
    print(f"Added action {name!r}.")
    return 0


@_reloads
def cmd_actions_remove(args):
    _guard(configedit.remove_action, args.config, args.name)
    print(f"Removed action {args.name!r}.")
    return 0


def cmd_check(args):
    try:
        cfg = cfgmod.load(args.config, must_exist=True)
    except cfgmod.ConfigError as e:
        for p in e.problems:
            print(f"error: {p}")
        return 1
    for w in [*cfg.warnings, *cfgmod.command_problems(cfg)]:
        print(f"warning: {w}")
    for n in cfg.notes:
        print(f"note: {n}")
    print(f"OK: {len(cfg.devices)} device(s), {len(cfg.tags)} tag rule(s), "
          f"{len(cfg.actions)} action(s)")
    return 0


def cmd_service_uninstall(args):
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
    from .completion import HEADER, default_install_path
    dest = args.dest or default_install_path()
    if not dest.exists():
        print(f"Nothing to remove: {dest} does not exist.")
        return 0
    if not dest.read_text(errors="replace").startswith(HEADER):
        print(f"error: {dest} is not a udevmon completion script; not removing it.",
              file=sys.stderr)
        return 1
    dest.unlink()
    print(f"Removed {dest}")
    return 0


def cmd_completion_bash(args):
    from .completion import SCRIPT
    sys.stdout.write(SCRIPT)
    return 0


def cmd_completion_install(args):
    from .completion import SCRIPT, default_install_path
    dest = args.dest or default_install_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(SCRIPT)
    print(f"Wrote {dest}")
    if args.dest is None:
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


def cmd_reload(args):
    from .daemon import reload_running
    if reload_running():
        print("Sent reload to the running daemon.")
        return 0
    print("udevmon is not running.", file=sys.stderr)
    return 1


def cmd_service_install(args):
    from .service import install
    unit = install(args.dest, args.now, args.config if args.config_explicit else None)
    print(f"Wrote {unit}")
    if args.dest is None and not args.now:
        print("Enable it with: systemctl --user enable --now udevmon.service")
    return 0


# -- parser ------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="udevmon", description=__doc__ or "")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--config", type=Path, default=None, help="config file path")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd_name", required=True)
    edits = argparse.ArgumentParser(add_help=False)          # shared by the editing commands
    edits.add_argument("--no-reload", action="store_true",
                       help="don't tell the running daemon to reload")

    s = sub.add_parser("run", help="run the daemon")
    s.add_argument("--dry-run", action="store_true", help="log actions instead of running them")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("scan", help="watch events and name devices")
    s.add_argument("--no-prompt", action="store_true", help="only print events")
    s.add_argument("--present", action="store_true",
                   help="also offer devices and filesystems that are already present")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("present", help="list USB devices and filesystems that are present now")
    s.add_argument("--all", action="store_true", help="include system filesystems")
    s.set_defaults(fn=cmd_present)

    dev = sub.add_parser("devices", help="manage named devices")
    g = dev.add_subparsers(dest="sub", required=False)
    x = g.add_parser("list", help="list named devices"); x.set_defaults(fn=cmd_devices_list)
    x = g.add_parser("show"); x.add_argument("name"); x.set_defaults(fn=cmd_devices_show)
    x = g.add_parser("add", parents=[edits])
    x.add_argument("name")
    x.add_argument("--match", action="append", required=True, metavar="KEY=VALUE")
    x.add_argument("--tag", action="append", metavar="TAG", help="explicit tag (repeatable)")
    x.add_argument("--description")
    x.set_defaults(fn=cmd_devices_add)
    x = g.add_parser("remove", parents=[edits]); x.add_argument("name"); x.set_defaults(fn=cmd_devices_remove)
    x = g.add_parser("tag", help="add explicit tags to a device", parents=[edits])
    x.add_argument("name"); x.add_argument("tags", nargs="+", metavar="TAG")
    x.set_defaults(fn=cmd_devices_tag)
    x = g.add_parser("untag", help="remove explicit tags from a device", parents=[edits])
    x.add_argument("name"); x.add_argument("tags", nargs="+", metavar="TAG")
    x.set_defaults(fn=cmd_devices_untag)
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
    x = tg.add_parser("list"); x.set_defaults(fn=cmd_tags_list)
    x = tg.add_parser("show"); x.add_argument("name"); x.set_defaults(fn=cmd_tags_show)
    x = tg.add_parser("add", help="declare a tag, optionally with a rule", parents=[edits])
    x.add_argument("name")
    x.add_argument("--match", action="append", metavar="KEY=VALUE",
                   help="rule pair; repeat a key to allow several values")
    x.add_argument("--description")
    x.set_defaults(fn=cmd_tags_add)
    x = tg.add_parser("remove", parents=[edits]); x.add_argument("name"); x.set_defaults(fn=cmd_tags_remove)

    a = sub.add_parser("actions", help="manage actions").add_subparsers(dest="sub", required=True)
    x = a.add_parser("list"); x.set_defaults(fn=cmd_actions_list)
    x = a.add_parser("add", help="actions add (--device N ... | --tag N ...) EVENT[,EVENT] "
                                 "[--name N] [--allow-unnamed|--named-only] -- CMD [ARG...]",
                     parents=[edits])
    x.add_argument("on", help="comma-separated: " + ", ".join(sorted(VALID_KINDS)))
    x.add_argument("--device", action="append", metavar="NAME", help="target device (repeatable)")
    x.add_argument("--tag", action="append", metavar="NAME", help="target tag (repeatable)")
    x.add_argument("--name")
    x.add_argument("--timeout", type=float)
    grp = x.add_mutually_exclusive_group()
    grp.add_argument("--allow-unnamed", dest="allow_unnamed", action="store_const", const=True,
                     default=None, help="also run for unnamed things matching the tag")
    grp.add_argument("--named-only", dest="allow_unnamed", action="store_const", const=False,
                     help="run for named devices only (explicit)")
    x.set_defaults(fn=cmd_actions_add)
    x = a.add_parser("remove", parents=[edits]); x.add_argument("name"); x.set_defaults(fn=cmd_actions_remove)
    x = a.add_parser("disable", help="stop actions running without deleting them", parents=[edits])
    x.add_argument("names", nargs="+", metavar="NAME"); x.set_defaults(fn=cmd_actions_disable)
    x = a.add_parser("enable", help="let disabled actions run again", parents=[edits])
    x.add_argument("names", nargs="+", metavar="NAME"); x.set_defaults(fn=cmd_actions_enable)
    x = a.add_parser("set", help="change an action's allow_unnamed, events or timeout",
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
    x.set_defaults(fn=cmd_actions_set, unnamed=None)

    s = sub.add_parser("explain", help="show what would run for a thing (present or simulated)")
    s.add_argument("event", nargs="?", choices=sorted(VALID_KINDS), metavar="EVENT",
                   help="only this event (default: every event that fits)")
    s.add_argument("--device", metavar="NAME", help="simulate this named device")
    s.add_argument("--prop", action="append", metavar="KEY=VALUE",
                   help="simulate a thing with this property (repeatable)")
    s.add_argument("--all", action="store_true",
                   help="also list system filesystems and things nothing matches")
    s.set_defaults(fn=cmd_explain)

    s = sub.add_parser("check", help="validate the config"); s.set_defaults(fn=cmd_check)
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

    co = sub.add_parser("completion", help="bash tab completion").add_subparsers(
        dest="sub", required=True)
    x = co.add_parser("bash", help="print the bash completion script")
    x.set_defaults(fn=cmd_completion_bash)
    x = co.add_parser("install", help="install it for your user (no root)")
    x.add_argument("--dest", type=Path, help="write the script here instead")
    x.set_defaults(fn=cmd_completion_install)
    x = co.add_parser("uninstall", help="remove the installed script")
    x.add_argument("--dest", type=Path, help="remove this file instead")
    x.set_defaults(fn=cmd_completion_uninstall)
    return p


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["_complete"]:        # not a documented command: see completion.py
        return _complete_words(argv[1:])
    parser = build_parser()
    tail: list[str] = []
    if "--" in argv:                     # everything after -- is a command, verbatim
        i = argv.index("--")
        argv, tail = argv[:i], argv[i + 1:]
    args = parser.parse_args(argv)
    args.cmd = tail
    args.config_explicit = args.config is not None
    if args.config is None:
        args.config = cfgmod.default_config_path()
    _setup_logging(args.verbose)
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        return 130
