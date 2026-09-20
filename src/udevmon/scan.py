"""Scan mode: watch events live and let the user name things as devices."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from . import config as cfgmod
from . import configedit
from .events import STARTUP, Endpoint, Event, describe
from .matching import (STRONG, build_match, default_match_keys, resolve, strength,
                       strength_note)
from .sources.udev import UdevSource
from .sources.udisks import UdisksSource

QUIET_MS = 2000
SKIP_KEYS = ("syspath", "subsystem", "devtype")


def status_text(cfg: cfgmod.Config, endpoint: Endpoint) -> str:
    """'device: NAME  tags: a, b' for named things, 'unnamed  tags: ...' otherwise."""
    res = resolve(cfg, endpoint.props)
    who = f"device: {', '.join(d.name for d in res.named)}" if res.named else "unnamed"
    return who + (f"  tags: {', '.join(res.tags)}" if res.tags else "")


def format_props(endpoint: Endpoint) -> str:
    return "  ".join(f"{k}={v}" for k, v in endpoint.props.items()
                     if not k.startswith("udev_") and k not in SKIP_KEYS)


class Scanner:
    def __init__(self, config_path: Path, prompt: bool, present: bool = False):
        self.config_path = config_path
        self.prompt = prompt
        self.present = present
        self.pending: dict[str, Endpoint] = {}
        self._timer = None

    def _config(self) -> cfgmod.Config:
        try:
            return cfgmod.load(self.config_path)
        except cfgmod.ConfigError:
            return cfgmod.Config()

    def _emit(self, event: Event) -> None:
        from gi.repository import GLib
        d = event.endpoint
        stamp = time.strftime("%H:%M:%S", time.localtime(event.time))
        keys = default_match_keys(d)
        level = strength(build_match(d, keys)) if keys else None
        status = status_text(self._config(), d)
        if event.kind == STARTUP:                 # already-present thing: keep it compact
            print(f"[{stamp}] present      {describe(d)}" + (f"  ({level})" if level else "")
                  + f"   {status}")
        else:
            print(f"[{stamp}] {event.kind:<12} {describe(d)}   {status}")
            print(f"           {format_props(d)}")
            if keys:
                print(f"           suggested match: {', '.join(keys)}  ({level})")
        if keys:
            self.pending[f"{d.source}:{d.key}"] = d
        sys.stdout.flush()
        if self._timer is not None:
            GLib.source_remove(self._timer)
        if self.prompt and self.pending:
            self._timer = GLib.timeout_add(QUIET_MS, self._quiet)

    def _quiet(self) -> bool:
        self._timer = None
        try:
            self._prompt_user()
        except (EOFError, KeyboardInterrupt):
            print()
            self.loop.quit()
        return False

    def _prompt_user(self) -> None:
        endpoints = list(self.pending.values())
        self.pending.clear()
        while endpoints:
            cfg = self._config()
            print("\nDetected:")
            for i, d in enumerate(endpoints, 1):
                print(f"  [{i}] {describe(d)}   {status_text(cfg, d)}")
            ans = input("Number(s) to name (e.g. 2 or 1,2), "
                        "Enter = scan on, q = quit: ").strip().lower()
            if ans == "q":
                self.loop.quit()
                return
            if not ans:
                return
            try:
                picks = list(dict.fromkeys(int(x) for x in ans.replace(" ", "").split(",")))
            except ValueError:
                picks = []
            if not picks or not all(1 <= n <= len(endpoints) for n in picks):
                print("Not a valid choice.")
                continue
            named = [n for n in picks if self._name_endpoint(endpoints[n - 1])]
            endpoints = [e for i, e in enumerate(endpoints, 1) if i not in named]
        print("Waiting for more events (Ctrl+C to quit)...")

    def _name_endpoint(self, endpoint: Endpoint) -> bool:
        name = input("Device name (Enter to skip): ").strip()
        if not name:
            print("Skipped.")
            return False
        if not cfgmod.NAME_RE.match(name):
            print("Names may contain only letters, digits, _ . -")
            return False
        defaults = default_match_keys(endpoint)
        available = [k for k in endpoint.props if not k.startswith("udev_")
                     and k not in ("syspath", "subsystem", "devtype", "mountpoint")]
        print(f"Properties you can match on: {', '.join(available)}")
        while True:
            raw = input(f"Match on [{', '.join(defaults)}] "
                        "(Enter to accept, or type a list): ").strip()
            keys = [k.strip() for k in raw.split(",") if k.strip()] if raw else defaults
            unknown = [k for k in keys if not endpoint.props.get(k)]
            if not keys:
                print("Nothing to match on; skipped.")
                return False
            if not unknown:
                break
            print(f"Not a property of this thing: {', '.join(unknown)}. "
                  "Choose from the list above.")
        match = build_match(endpoint, keys)
        level = strength(match)
        print(f"Match strength: {level} — {strength_note(level)}")
        if level != STRONG:
            if input("Save anyway? [y/N] ").strip().lower() != "y":
                return False
        existing = sorted(self._config().all_tag_names())
        hint = f" [existing: {', '.join(existing)}]" if existing else ""
        raw = input(f"Tags, comma-separated{hint} (Enter for none): ").strip()
        tags = [t.strip() for t in raw.split(",") if t.strip()]
        if not all(cfgmod.NAME_RE.match(t) for t in tags):
            print("Tag names may contain only letters, digits, _ . -; nothing saved.")
            return False
        try:
            configedit.add_device(self.config_path, name, match, describe(endpoint), tags)
        except cfgmod.ConfigError as e:
            print(f"Could not save: {e}")
            return False
        print(f"Saved device {name!r} to {self.config_path}")
        from .daemon import reload_running
        if reload_running(self.config_path):
            print("Reloaded the running daemon.")
        print("Add an action with e.g.:\n  udevmon actions add --device "
              f"{name} {'mounted' if endpoint.is_filesystem else 'connected'} "
              "-- notify-send 'hello'")
        return True

    def run(self) -> int:
        from gi.repository import GLib
        try:
            cfg = cfgmod.load(self.config_path)
        except cfgmod.ConfigError as e:
            print(f"Config problem: {e}", file=sys.stderr)
            return 1
        self.loop = GLib.MainLoop()
        GLib.unix_signal_add(GLib.PRIORITY_HIGH, 2, lambda: self.loop.quit() or False)
        sources = [UdevSource(cfg.settings.udev_filters, announce_present=self.present),
                   UdisksSource(announce_present=self.present, include_system=False)]
        for s in sources:
            s.start(self._emit)
        print("Scanning" + (" (including devices already present)" if self.present else "")
              + ". Plug or unplug a device, or mount/unmount a filesystem."
              + (" Ctrl+C to quit." if not self.prompt else " After a pause you'll be asked to name it."))
        try:
            self.loop.run()
        finally:
            for s in sources:
                s.stop()
        return 0
