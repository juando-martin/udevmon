"""Scan mode: watch events live and let the user name things as devices."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from . import config as cfgmod
from . import configedit
from .events import STARTUP, VOLATILE_KEYS, Endpoint, Event, describe
from .matching import (STRONG, build_match, default_match_keys, device_label, lacks_identity,
                       level_label, match_choices, resolve, strength, strength_note)
from .sources.udev import UdevSource
from .sources.udisks import UdisksSource

QUIET_MS = 2000
SKIP_KEYS = ("syspath", "subsystem", "devtype", "instance")


def status_text(cfg: cfgmod.Config, endpoint: Endpoint) -> str:
    """'device: NAME  tags: a, b' for named things, 'unnamed  tags: ...' otherwise."""
    res = resolve(cfg, endpoint.props)
    who = f"device: {', '.join(device_label(d) for d in res.named)}" if res.named else "unnamed"
    return (who + (f"  tags: {', '.join(res.tags)}" if res.tags else "")
            + (f"  excluded from: {', '.join(res.excluded)}" if res.excluded else ""))


def format_all_props(endpoint: Endpoint) -> list[str]:
    """Every property, one per line and sorted, marking those that change between plugs
    (they cannot be used in tag rules). Includes the raw ``udev_*`` ones."""
    return [f"{k} = {v}" + ("   [changes per plug]" if k in VOLATILE_KEYS else "")
            for k, v in sorted(endpoint.props.items())]


def format_props(endpoint: Endpoint) -> str:
    return "  ".join(f"{k}={v}" for k, v in endpoint.props.items()
                     if not k.startswith("udev_") and k not in SKIP_KEYS)


class Scanner:
    def __init__(self, config_path: Path, prompt: bool, present: bool = False,
                 all_props: bool = False):
        self.config_path = config_path
        self.all_props = all_props
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
        level = level_label(d, keys) if keys else None
        status = status_text(self._config(), d)
        if event.kind == STARTUP:                 # already-present thing: keep it compact
            print(f"[{stamp}] present      {describe(d)}" + (f"  ({level})" if level else "")
                  + f"   {status}")
            self._print_all(d)
        else:
            print(f"[{stamp}] {event.kind:<12} {describe(d)}   {status}")
            if self.all_props:
                self._print_all(d)
            else:
                print(f"           {format_props(d)}")
            if keys and lacks_identity(d):
                print("           no serial number: nothing identifies this particular unit "
                      "(you will be asked what to match on)")
            elif keys:
                print(f"           suggested match: {', '.join(keys)}  ({level})")
        if keys:
            self.pending[f"{d.source}:{d.key}"] = d
        sys.stdout.flush()
        if self._timer is not None:
            GLib.source_remove(self._timer)
        if self.prompt and self.pending:
            self._timer = GLib.timeout_add(QUIET_MS, self._quiet)

    def _print_all(self, endpoint: Endpoint) -> None:
        if self.all_props:
            for line in format_all_props(endpoint):
                print(f"           {line}")

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
            ans = input("Number(s) to name, iN to ignore, "
                        "Enter = scan on, q = quit: ").strip().lower()
            if ans == "q":
                self.loop.quit()
                return
            if not ans:
                return
            ignore = ans.startswith("i")            # "i3" or "i1,3": ignore instead of name
            if ignore:
                ans = ans[1:].strip()
            try:
                picks = list(dict.fromkeys(int(x) for x in ans.replace(" ", "").split(",")))
            except ValueError:
                picks = []
            if not picks or not all(1 <= n <= len(endpoints) for n in picks):
                print("Not a valid choice.")
                continue
            named = [n for n in picks
                     if self._register_endpoint(endpoints[n - 1], ignore=ignore)]
            endpoints = [e for i, e in enumerate(endpoints, 1) if i not in named]
        print("Waiting for more events (Ctrl+C to quit)...")

    def _choose_keys(self, endpoint: Endpoint) -> list[str] | None:
        """The properties to match on, or None to skip. A thing with no serial number gets no
        pre-selected answer: nothing identifies the unit, so the user chooses what to tie it to.
        Used to build a device's match and a category's rule alike."""
        available = [k for k in endpoint.props if not k.startswith("udev_")
                     and k not in ("syspath", "subsystem", "devtype", "mountpoint", "instance")]
        print(f"Properties you can match on: {', '.join(available)}")
        choices = match_choices(endpoint) if lacks_identity(endpoint) else []
        if choices:
            print("This thing has no serial number, so nothing identifies\n"
                  "this particular unit. Choose what to match on:")
            for i, (meaning, keys) in enumerate(choices, 1):
                print(f"  {i}) {meaning:<32} ({', '.join(keys)})")
        defaults = [] if choices else default_match_keys(endpoint)
        while True:
            if choices:
                raw = input("Number, or a list of properties: ").strip()
            else:
                raw = input(f"Match on [{', '.join(defaults)}] "
                            "(Enter to accept, or type a list): ").strip()
            if choices and raw.isdigit() and 1 <= int(raw) <= len(choices):
                return list(choices[int(raw) - 1][1])
            keys = [k.strip() for k in raw.split(",") if k.strip()] if raw else defaults
            if not keys:
                print("Nothing chosen; skipped." if choices else "Nothing to match on; skipped.")
                return None
            unknown = [k for k in keys if not endpoint.props.get(k)]
            if not unknown:
                return keys
            print(f"Not a property of this thing: {', '.join(unknown)}. "
                  "Choose from the list above.")

    def _choose_kind(self, endpoint: Endpoint) -> str | None:
        """"device" (one particular, trusted thing) or "category" (a tag rule matching every
        thing like it), or None to skip this thing entirely. A thing with no serial number is
        steered towards "category", since no device match can single it out anyway."""
        no_id = lacks_identity(endpoint)
        if no_id:
            print("This thing has no serial number, so nothing tells one unit from another;\n"
                  "a category usually fits it better than a device, which claims to be one\n"
                  "exact, trusted thing.")
        print("Register it as a [d]evice (one particular thing) or a [c]ategory\n"
              "(a tag rule matching every thing like this; an action then\n"
              "needs allow_unnamed = true to run for things matched only\n"
              "this way)?")
        default = "category" if no_id else "device"
        while True:
            raw = input(f"Choice [{default[0]}] (q to skip this thing): ").strip().lower()
            if raw == "q":
                return None
            if not raw:
                return default
            if raw in ("d", "device"):
                return "device"
            if raw in ("c", "category"):
                return "category"
            print("Not a valid choice.")

    def _ask_name(self, prompt: str, default: str | None = None) -> str | None:
        """A name, or ``default`` on Enter (None means Enter cancels this step)."""
        while True:
            name = input(prompt).strip()
            if not name:
                return default
            if cfgmod.NAME_RE.match(name):
                return name
            print("Names may contain only letters, digits, _ . -")

    def _ask_tags(self, current: list[str] | None = None) -> list[str]:
        """Explicit tags, comma-separated. Enter keeps ``current`` (or means none, the first
        time); "-" clears them."""
        existing = sorted(self._config().all_tag_names())
        hint = f" [existing: {', '.join(existing)}]" if existing else ""
        tail = f" (Enter to keep: {', '.join(current)})" if current else " (Enter for none)"
        while True:
            raw = input(f"Tags, comma-separated{hint}{tail}: ").strip()
            if not raw:
                return list(current) if current else []
            if raw == "-":
                return []
            tags = [t.strip() for t in raw.split(",") if t.strip()]
            if all(cfgmod.NAME_RE.match(t) for t in tags):
                return tags
            print("Tag names may contain only letters, digits, _ . -")

    def _describe_match(self, match: dict) -> str:
        level = strength(match)
        pairs = "  ".join(f"{k}={v}" for k, v in match.items())
        return f"{pairs}  ({level} — {strength_note(level)})"

    def _register_endpoint(self, endpoint: Endpoint, ignore: bool = False) -> bool:
        if ignore:
            name = self._ask_name("Device name to ignore (Enter to skip): ")
            if name is None:
                print("Skipped.")
                return False
            keys = self._choose_keys(endpoint)
            if keys is None:
                return False
            return self._finish_device(endpoint, name, keys, [], ignore=True)

        kind = self._choose_kind(endpoint)
        if kind is None:
            print("Skipped.")
            return False
        name = self._ask_name("Category (tag) name (Enter to skip): " if kind == "category"
                              else "Device name (Enter to skip): ")
        if name is None:
            print("Skipped.")
            return False
        keys = self._choose_keys(endpoint)
        if keys is None:
            return False
        if kind == "category":
            return self._finish_category(endpoint, name, keys)
        tags = self._ask_tags()
        return self._finish_device(endpoint, name, keys, tags, ignore=False)

    def _finish_device(self, endpoint: Endpoint, name: str, keys: list[str], tags: list[str],
                       ignore: bool) -> bool:
        """The confirm-and-edit loop for a device: shows what would be saved and lets the
        user redo the name, the match or the tags before committing (or cancel)."""
        while True:
            match = build_match(endpoint, keys)
            level = strength(match)
            print("\nAbout to save:")
            print(f"  device {name!r}" + ("  [ignored]" if ignore else ""))
            print(f"  match: {self._describe_match(match)}")
            if not ignore:
                print(f"  tags: {', '.join(tags) if tags else '(none)'}")
            opts = ["[Enter] save" if level == STRONG else "[y] save", "[r] rename",
                   "[m] change match"]
            if not ignore:
                opts.append("[t] change tags")
            opts.append("[c] cancel")
            print("   ".join(opts))
            ans = input("> ").strip().lower()
            if ans in ("c", "cancel", "q", "n", "no"):
                print("Cancelled.")
                return False
            if ans in ("y", "yes", "save") or (ans == "" and level == STRONG):
                try:
                    configedit.add_device(self.config_path, name, match, describe(endpoint),
                                          tags, status="ignored" if ignore else None)
                except cfgmod.ConfigError as e:
                    print(f"Could not save: {e}")
                    continue
                self._after_save(endpoint, name, is_tag=False, ignore=ignore)
                return True
            if ans == "" and level != STRONG:
                print(f"This match is {level}: {strength_note(level)}. Type y to save anyway.")
                continue
            if ans in ("r", "rename"):
                name = self._ask_name(f"New name (Enter to keep {name!r}): ", default=name)
                continue
            if ans in ("m", "match"):
                new_keys = self._choose_keys(endpoint)
                if new_keys:
                    keys = new_keys
                else:
                    print("Kept the previous match.")
                continue
            if not ignore and ans in ("t", "tags"):
                tags = self._ask_tags(current=tags)
                continue
            print("Not a valid choice.")

    def _finish_category(self, endpoint: Endpoint, name: str, keys: list[str]) -> bool:
        """The confirm-and-edit loop for a category (a declared tag with a rule)."""
        while True:
            match = build_match(endpoint, keys)
            print("\nAbout to save:")
            print(f"  category (tag) {name!r}")
            print(f"  rule: {self._describe_match(match)}")
            print("[Enter] save   [r] rename   [m] change rule   [c] cancel")
            ans = input("> ").strip().lower()
            if ans in ("c", "cancel", "q", "n", "no"):
                print("Cancelled.")
                return False
            if ans in ("", "y", "yes", "save"):
                try:
                    configedit.add_tag(self.config_path, name, match, describe(endpoint))
                except cfgmod.ConfigError as e:
                    print(f"Could not save: {e}")
                    continue
                self._after_save(endpoint, name, is_tag=True, ignore=False)
                return True
            if ans in ("r", "rename"):
                name = self._ask_name(f"New name (Enter to keep {name!r}): ", default=name)
                continue
            if ans in ("m", "match", "rule"):
                new_keys = self._choose_keys(endpoint)
                if new_keys:
                    keys = new_keys
                else:
                    print("Kept the previous rule.")
                continue
            print("Not a valid choice.")

    def _after_save(self, endpoint: Endpoint, name: str, is_tag: bool, ignore: bool) -> None:
        label = "tag" if is_tag else "device"
        print(f"Saved {label} {name!r} to {self.config_path}" + (" (ignored)" if ignore else ""))
        from . import pause
        from .daemon import reload_running
        if reload_running(self.config_path):
            print("Reloaded the running daemon.")
        paused, until = pause.status()
        if paused:
            print(f"Note: automation is paused ({pause.describe_until(until)}): nothing runs.")
        if ignore:
            return
        event = "mounted" if endpoint.is_filesystem else "connected"
        target = f"--tag {name}" if is_tag else f"--device {name}"
        extra = " --allow-unnamed" if is_tag else ""
        print(f"Add an action with e.g.:\n  udevmon actions add {target} {event}{extra} "
              "-- notify-send 'hello'")

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
