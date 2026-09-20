"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import configedit
from .events import VALID_KINDS, describe
from .matching import build_match, strength


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


def cmd_devices_list(args):
    cfg = _load(args)
    if not cfg.devices:
        print("No devices. Run `udevmon scan` to create some.")
    for d in cfg.devices.values():
        tags = f"  tags: {', '.join(d.tags)}" if d.tags else ""
        print(f"{d.name:<20} {strength(d.match):<7} {d.match}{tags}  "
              f"({len(_actions_for(cfg, d))} action(s))")
    return 0


def cmd_devices_show(args):
    cfg = _load(args, must_exist=True)
    d = cfg.devices.get(args.name)
    if not d:
        print(f"error: no such device {args.name!r}", file=sys.stderr)
        return 1
    print(f"{d.name}: {d.description}\n  match: {d.match}  ({strength(d.match)})")
    if d.tags:
        print(f"  tags: {', '.join(d.tags)}")
    for x in _actions_for(cfg, d):
        print(f"  action {x.name}: on {list(x.on)} run {list(x.run)} (timeout {x.timeout:g}s)")
    return 0


def cmd_devices_add(args):
    match = _guard(_parse_matches, args.match, allow_lists=False)
    _guard(configedit.add_device, args.config, args.name, match, args.description or "",
           args.tag or [])
    print(f"Added device {args.name!r} ({strength(match)}).")
    return 0


def cmd_devices_remove(args):
    _guard(configedit.remove_device, args.config, args.name)
    print(f"Removed device {args.name!r}.")
    return 0


def cmd_devices_tag(args):
    _guard(configedit.set_device_tags, args.config, args.name, add=args.tags)
    print(f"Tagged {args.name!r}: {', '.join(args.tags)}.")
    return 0


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


def cmd_tags_add(args):
    rule = _guard(_parse_matches, args.match or [], allow_lists=True)
    _guard(configedit.add_tag, args.config, args.name, rule, args.description or "")
    print(f"Added tag {args.name!r}" + (f" with a rule ({strength(rule)})." if rule else "."))
    return 0


def cmd_tags_remove(args):
    _guard(configedit.remove_tag, args.config, args.name)
    print(f"Removed tag {args.name!r}.")
    return 0


def cmd_actions_list(args):
    cfg = _load(args)
    for x in cfg.actions:
        target = f"device:{','.join(x.devices)}" if x.devices else f"tag:{','.join(x.tags)}"
        flag = {True: " [allow_unnamed]", False: " [named only]", None: ""}[x.allow_unnamed]
        print(f"{x.name:<24} {target:<22} on {','.join(x.on):<24} {list(x.run)}{flag}")
    if not cfg.actions:
        print("No actions.")
    return 0


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
    for w in cfg.warnings:
        print(f"warning: {w}")
    for n in cfg.notes:
        print(f"note: {n}")
    print(f"OK: {len(cfg.devices)} device(s), {len(cfg.tags)} tag rule(s), "
          f"{len(cfg.actions)} action(s)")
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
    x = g.add_parser("add")
    x.add_argument("name")
    x.add_argument("--match", action="append", required=True, metavar="KEY=VALUE")
    x.add_argument("--tag", action="append", metavar="TAG", help="explicit tag (repeatable)")
    x.add_argument("--description")
    x.set_defaults(fn=cmd_devices_add)
    x = g.add_parser("remove"); x.add_argument("name"); x.set_defaults(fn=cmd_devices_remove)
    x = g.add_parser("tag", help="add explicit tags to a device")
    x.add_argument("name"); x.add_argument("tags", nargs="+", metavar="TAG")
    x.set_defaults(fn=cmd_devices_tag)
    x = g.add_parser("untag", help="remove explicit tags from a device")
    x.add_argument("name"); x.add_argument("tags", nargs="+", metavar="TAG")
    x.set_defaults(fn=cmd_devices_untag)
    dev.set_defaults(fn=cmd_devices_list)              # bare `udevmon devices` lists them

    tg = sub.add_parser("tags", help="manage tags and tag rules").add_subparsers(
        dest="sub", required=True)
    x = tg.add_parser("list"); x.set_defaults(fn=cmd_tags_list)
    x = tg.add_parser("show"); x.add_argument("name"); x.set_defaults(fn=cmd_tags_show)
    x = tg.add_parser("add", help="declare a tag, optionally with a rule")
    x.add_argument("name")
    x.add_argument("--match", action="append", metavar="KEY=VALUE",
                   help="rule pair; repeat a key to allow several values")
    x.add_argument("--description")
    x.set_defaults(fn=cmd_tags_add)
    x = tg.add_parser("remove"); x.add_argument("name"); x.set_defaults(fn=cmd_tags_remove)

    a = sub.add_parser("actions", help="manage actions").add_subparsers(dest="sub", required=True)
    x = a.add_parser("list"); x.set_defaults(fn=cmd_actions_list)
    x = a.add_parser("add", help="actions add (--device N ... | --tag N ...) EVENT[,EVENT] "
                                 "[--name N] [--allow-unnamed|--named-only] -- CMD [ARG...]")
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
    x = a.add_parser("remove"); x.add_argument("name"); x.set_defaults(fn=cmd_actions_remove)

    s = sub.add_parser("check", help="validate the config"); s.set_defaults(fn=cmd_check)
    s = sub.add_parser("reload", help="tell the running daemon to reload"); s.set_defaults(fn=cmd_reload)

    sv = sub.add_parser("service", help="systemd user service").add_subparsers(
        dest="sub", required=True)
    x = sv.add_parser("install")
    x.add_argument("--now", action="store_true", help="also enable and start it")
    x.add_argument("--dest", type=Path, help="write the unit here instead (no systemctl)")
    x.set_defaults(fn=cmd_service_install)
    return p


def main(argv=None) -> int:
    parser = build_parser()
    argv = list(sys.argv[1:] if argv is None else argv)
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
