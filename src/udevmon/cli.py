"""Command-line interface."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from . import __version__
from . import config as cfgmod
from . import configedit
from .events import VALID_KINDS, Endpoint, describe
from .matching import build_match, matches, strength


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
    from .scan import known_devices, format_props
    from .matching import default_match_keys
    cfg = _load(args)
    endpoints = UdevSource(cfg.settings.udev_filters).snapshot() \
        + UdisksSource().snapshot(include_system=args.all)
    for d in endpoints:
        keys = default_match_keys(d)
        level = strength(build_match(d, keys)) if keys else "-"
        known = known_devices(cfg, d)
        print(f"{describe(d)}   [{level}]" + (f"   device: {', '.join(known)}" if known else ""))
        print(f"    {format_props(d)}")
    return 0


def cmd_devices_list(args):
    cfg = _load(args)
    if not cfg.devices:
        print("No devices. Run `udevmon scan` to create some.")
    for a in cfg.devices.values():
        n = sum(1 for x in cfg.actions if x.device == a.name)
        print(f"{a.name:<20} {strength(a.match):<7} {a.match}  ({n} action(s))")
    return 0


def cmd_devices_show(args):
    cfg = _load(args, must_exist=True)
    a = cfg.devices.get(args.name)
    if not a:
        print(f"error: no such device {args.name!r}", file=sys.stderr)
        return 1
    print(f"{a.name}: {a.description}\n  match: {a.match}  ({strength(a.match)})")
    for x in cfg.actions:
        if x.device == a.name:
            print(f"  action {x.name}: on {list(x.on)} run {list(x.run)} (timeout {x.timeout:g}s)")
    return 0


def cmd_devices_add(args):
    match = {}
    for pair in args.match:
        k, sep, v = pair.partition("=")
        if not sep or not k:
            print(f"error: bad --match {pair!r}, expected key=value", file=sys.stderr)
            return 1
        match[k] = v
    _guard(configedit.add_device, args.config, args.name, match, args.description or "")
    level = strength(match)
    print(f"Added device {args.name!r} ({level}).")
    return 0


def cmd_devices_remove(args):
    _guard(configedit.remove_device, args.config, args.name)
    print(f"Removed device {args.name!r}.")
    return 0


def cmd_actions_list(args):
    cfg = _load(args)
    for x in cfg.actions:
        print(f"{x.name:<24} {x.device:<16} on {','.join(x.on):<24} {list(x.run)}")
    if not cfg.actions:
        print("No actions.")
    return 0


def cmd_actions_add(args):
    on = [k.strip() for k in args.on.split(",") if k.strip()]
    bad = [k for k in on if k not in VALID_KINDS]
    if bad or not on:
        print(f"error: events must be from {sorted(VALID_KINDS)}", file=sys.stderr)
        return 1
    cmd = args.cmd
    if not cmd:
        print("error: no command given (put it after --)", file=sys.stderr)
        return 1
    name = _guard(configedit.add_action, args.config, args.name, args.device, on, cmd,
                  args.timeout)
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
    print(f"OK: {len(cfg.devices)} device(s), {len(cfg.actions)} action(s)")
    return 0


def cmd_migrate(args):
    changed = _guard(configedit.migrate, args.config)
    if changed:
        print(f"Migrated {args.config} to [devices] / device = (previous version saved as .bak).")
    else:
        print("Nothing to migrate: the config already uses the current names.")
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

    def device_commands(group):
        x = group.add_parser("list", help="list named devices"); x.set_defaults(fn=cmd_devices_list)
        x = group.add_parser("show"); x.add_argument("name"); x.set_defaults(fn=cmd_devices_show)
        x = group.add_parser("add")
        x.add_argument("name")
        x.add_argument("--match", action="append", required=True, metavar="KEY=VALUE")
        x.add_argument("--description")
        x.set_defaults(fn=cmd_devices_add)
        x = group.add_parser("remove"); x.add_argument("name"); x.set_defaults(fn=cmd_devices_remove)

    dev = sub.add_parser("devices", help="manage named devices")
    device_commands(dev.add_subparsers(dest="sub", required=False))
    dev.set_defaults(fn=cmd_devices_list)              # bare `udevmon devices` lists them
    old = sub.add_parser("assets", help=argparse.SUPPRESS)   # deprecated spelling
    device_commands(old.add_subparsers(dest="sub", required=True))
    old.set_defaults(deprecated_alias="devices")

    a = sub.add_parser("actions", help="manage actions").add_subparsers(dest="sub", required=True)
    x = a.add_parser("list"); x.set_defaults(fn=cmd_actions_list)
    x = a.add_parser("add", help="actions add DEVICE EVENT[,EVENT] [--name N] -- CMD [ARG...]")
    x.add_argument("device")
    x.add_argument("on", help="comma-separated: " + ", ".join(sorted(VALID_KINDS)))
    x.add_argument("--name")
    x.add_argument("--timeout", type=float)
    x.set_defaults(fn=cmd_actions_add)
    x = a.add_parser("remove"); x.add_argument("name"); x.set_defaults(fn=cmd_actions_remove)

    s = sub.add_parser("check", help="validate the config"); s.set_defaults(fn=cmd_check)
    s = sub.add_parser("migrate", help="update a config that uses the old 'assets' names")
    s.set_defaults(fn=cmd_migrate)
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
    if getattr(args, "deprecated_alias", None):
        print(f"note: 'udevmon assets' is deprecated, use 'udevmon {args.deprecated_alias}'",
              file=sys.stderr)
    args.config_explicit = args.config is not None
    if args.config is None:
        args.config = cfgmod.default_config_path()
    _setup_logging(args.verbose)
    try:
        return args.fn(args) or 0
    except KeyboardInterrupt:
        return 130
