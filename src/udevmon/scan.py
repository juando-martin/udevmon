"""Scan mode: watch events live and let the user name devices as devices."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from . import config as cfgmod
from . import configedit
from .events import STARTUP, Endpoint, Event, describe
from .matching import (MEDIUM, STRONG, build_match, default_match_keys, matches, strength,
                       strength_note)
from .sources.udev import UdevSource
from .sources.udisks import UdisksSource

QUIET_MS = 2000
SKIP_KEYS = ("syspath", "subsystem", "devtype")


def known_devices(cfg: cfgmod.Config, endpoint: Endpoint) -> list[str]:
    return [a.name for a in cfg.devices.values() if matches(a.match, endpoint.props)]


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

    def _emit(self, event: Event) -> None:
        from gi.repository import GLib
        d = event.endpoint
        stamp = time.strftime("%H:%M:%S", time.localtime(event.time))
        keys = default_match_keys(d)
        level = strength(build_match(d, keys)) if keys else None
        if event.kind == STARTUP:                 # already-present device: keep it compact
            print(f"[{stamp}] present      {describe(d)}" + (f"  ({level})" if level else ""))
        else:
            print(f"[{stamp}] {event.kind:<12} {describe(d)}")
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
            cfg = cfgmod.load(self.config_path)
            print("\nDetected:")
            for i, d in enumerate(endpoints, 1):
                known = known_devices(cfg, d)
                note = f"  (already device: {', '.join(known)})" if known else ""
                print(f"  [{i}] {describe(d)}{note}")
            ans = input("Number to name, Enter to keep scanning, q to quit: ").strip().lower()
            if ans == "q":
                self.loop.quit()
                return
            if not ans:
                return
            if not ans.isdigit() or not 1 <= int(ans) <= len(endpoints):
                print("Not a valid choice.")
                continue
            if self._name_endpoint(endpoints[int(ans) - 1]):
                endpoints.pop(int(ans) - 1)
        print("Waiting for more events (Ctrl+C to quit)...")

    def _name_endpoint(self, endpoint: Endpoint) -> bool:
        name = input("Device name: ").strip()
        if not cfgmod.NAME_RE.match(name):
            print("Names may contain only letters, digits, _ . -")
            return False
        defaults = default_match_keys(endpoint)
        available = [k for k in endpoint.props if not k.startswith("udev_")
                     and k not in ("syspath", "subsystem", "devtype", "mountpoint")]
        print(f"Available keys: {', '.join(available)}")
        raw = input(f"Match on [{', '.join(defaults)}] (Enter to accept): ").strip()
        keys = [k.strip() for k in raw.split(",") if k.strip()] if raw else defaults
        match = build_match(endpoint, keys)
        if not match or len(match) != len(keys):
            print("Some chosen keys are missing on this device; nothing saved.")
            return False
        level = strength(match)
        print(f"Match strength: {level} — {strength_note(level)}")
        if level != STRONG:
            if input("Save anyway? [y/N] ").strip().lower() != "y":
                return False
        desc = describe(endpoint)
        try:
            configedit.add_device(self.config_path, name, match, desc)
        except cfgmod.ConfigError as e:
            print(f"Could not save: {e}")
            return False
        print(f"Saved device {name!r} to {self.config_path}")
        print(f"Add an action with e.g.:\n  udevmon actions add {name} "
              f"{'mounted' if endpoint.is_filesystem else 'connected'} -- notify-send 'hello'")
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
