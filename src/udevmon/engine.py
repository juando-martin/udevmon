"""Resolve events to devices/tags, select actions, debounce, and dispatch (spec 5.5, 7)."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import replace
from typing import NamedTuple

from . import pause as pause_flag
from .config import Action, Config, resolve_program, uses_script_dir
from .events import DISCONNECTED, REMOVED, UNMOUNTED, Event
from .matching import resolve

log = logging.getLogger("udevmon.engine")


class Job(NamedTuple):
    action: Action
    device: str | None      # name of the named device that applies (None: unnamed thing)
    tag: str | None         # for tag actions: the tag that selected it
    vars: Mapping = {}      # that device's variables, for {var_NAME} (empty when unnamed)


class Skip(NamedTuple):
    action: Action
    reason: str


class Plan(NamedTuple):
    """What an event would do, worked out without side effects (no debounce, no run)."""
    resolution: object
    jobs: list
    skipped: list


def presence_ends(event: Event) -> str | None:
    """The instance whose presence this event ends, else None: ``disconnected`` for a USB
    device; for a filesystem ``removed``, or ``unmounted`` unless it was a pull
    (``surprise=1``, when ``removed`` follows)."""
    instance = event.props.get("instance")
    if not instance:
        return None
    if event.kind in (DISCONNECTED, REMOVED):
        return instance
    if event.kind == UNMOUNTED and event.props.get("surprise") != "1":
        return instance
    return None


class Engine:
    def __init__(self, config: Config, runner, clock=time.monotonic, paused=None):
        self.config = config
        self.runner = runner
        self._clock = clock
        self._paused = paused or pause_flag.is_paused        # a callable: injectable for tests
        self._was_paused = False
        # endpoint key -> (kind, time) of its last event that was not dropped as a repeat
        self._last: dict[str, tuple[str, float]] = {}

    def set_config(self, config: Config) -> None:
        self.config = config

    def plan(self, event: Event) -> Plan:
        res = resolve(self.config, event.props)
        skipped: list[Skip] = []
        return Plan(res, self.select(event, res, skipped), skipped)

    def select(self, event: Event, res, skipped: list | None = None) -> list[Job]:
        """The actions that apply to this event (each at most once), in the order they run:
        by ``order``, then config order."""
        named = {d.name for d in res.named}
        jobs: list[Job] = []

        def skip(action, reason):
            log.debug("%s: %r skipped: %s", event.kind, action.name, reason)
            if skipped is not None:
                skipped.append(Skip(action, reason))

        def found(action):
            """The action with its program resolved through script_dir; None (and a skip)
            when it is set but the program is nowhere to be found."""
            settings = self.config.settings
            if not uses_script_dir(settings, action.run[0]):
                return action
            path = resolve_program(settings, action.run[0])
            if path is None:
                reason = (f"program {action.run[0]!r} not found in script_dir or the "
                          "standard system paths")
                skip(action, reason)
                if skipped is None:                   # a real event, not `explain`: say so
                    log.warning("%s: action %r skipped: %s", event.kind, action.name, reason)
                return None
            return replace(action, run=(path, *action.run[1:]))

        if res.ignored:                               # an ignored device silences everything
            log.debug("%s: ignored (%s)", event.kind, ", ".join(d.name for d in res.ignored))
            return jobs
        for action in self.config.actions:
            if event.kind not in action.on:
                continue
            if action.devices:
                listed = [d for d in res.named if d.name in action.devices]
                if not listed:
                    continue
                if not action.enabled:
                    skip(action, "the action is disabled")
                    continue
                device = next((d.name for d in listed if d.status == "active"), None)
                if device is None:                    # its own actions are off for a disabled device
                    skip(action, f"device {listed[0].name!r} is disabled")
                    continue
                resolved = found(action)
                if resolved is not None:
                    jobs.append(Job(resolved, device, None, self._vars(device)))
                continue
            if action.tag_mode == "all":                  # every listed tag is needed
                tag = action.tags[0] if all(t in res.tags for t in action.tags) else None
            else:
                tag = next((t for t in action.tags if t in res.tags), None)
            if tag is None:
                continue
            if not action.enabled:
                skip(action, "the action is disabled")
                continue
            if not named and not action.allow_unnamed:
                skip(action, "an unnamed thing, and the action does not "
                             "set allow_unnamed = true")
                continue
            device = res.named[0].name if named else None
            resolved = found(action)
            if resolved is not None:
                jobs.append(Job(resolved, device, tag, self._vars(device)))
        jobs.sort(key=lambda job: job.action.order)          # stable: ties keep config order
        if skipped is not None:
            skipped.sort(key=lambda s: s.action.order)
        return jobs

    def _vars(self, device: str | None) -> Mapping:
        d = self.config.devices.get(device) if device else None
        return dict(d.vars) if d else {}

    def trigger(self, event: Event) -> tuple[list[Job], list[Skip]]:
        """The jobs for an event asked for with ``udevmon trigger``, logged like a real one.

        Unlike a real event it is never dropped as a repeat (and does not count as one),
        and it ends nothing: no state folder is removed after it. The caller has checked
        the pause. Nothing is submitted here.
        """
        res = resolve(self.config, event.props)
        skipped: list[Skip] = []
        if res.ignored:
            return [], skipped
        jobs = self.select(event, res, skipped)
        subject = res.named[0].name if res.named else "unnamed thing"
        log.info("%s (udevmon trigger): %s%s, %d action(s)", event.kind, subject,
                 f" [{', '.join(res.tags)}]" if res.tags else "", len(jobs))
        for s in skipped:
            log.info("%s (udevmon trigger): action %r skipped: %s", event.kind, s.action.name,
                     s.reason)
        return jobs, skipped

    def _pause_check(self) -> bool:
        """True while automation is paused; logs when that starts and ends."""
        paused = bool(self._paused())
        if paused != self._was_paused:
            log.info("automation paused: no action runs until `udevmon resume`" if paused
                     else "automation resumed")
            self._was_paused = paused
        return paused

    def handle(self, event: Event) -> list[Job]:
        jobs = self._dispatch(event)
        ended = presence_ends(event)
        if ended:                        # tidy up after the actions, even when there are none
            self.runner.submit(event, jobs, ended)
        elif jobs:
            self.runner.submit(event, jobs)
        return jobs

    def _repeat(self, event: Event) -> bool:
        """Whether the event repeats its thing's last one within ``debounce`` (then dropped).

        Keyed on the thing (its endpoint), not on the device it matches: two keys matching
        one device are two things. Any other event of the thing in between, with actions or
        not, ends the repeat (mounted, unmounted, mounted again is three events).
        """
        key, now = event.endpoint.key, self._clock()
        last = self._last.get(key)
        if last is not None and last[0] == event.kind \
                and now - last[1] < self.config.settings.debounce:
            log.info("%s for %s dropped: a repeat within %gs (debounce)",
                     event.kind, key, self.config.settings.debounce)
            return True
        self._last[key] = (event.kind, now)
        return False

    def _dispatch(self, event: Event) -> list[Job]:
        """Work out the jobs for an event (debounce and pause included); submits nothing."""
        if self._pause_check():                       # startup and shutdown are held back too
            log.debug("%s: paused, skipped (%s)", event.kind, event.endpoint.key)
            return []
        if self._repeat(event):
            return []
        props = event.props
        res = resolve(self.config, props)
        if not res.named and not res.tags:
            log.debug("%s: nothing matched (%s)", event.kind, event.endpoint.key)
            return []
        if len(res.named) > 1:
            log.info("%s matches several devices: %s", event.kind,
                     ", ".join(d.name for d in res.named))
        jobs = self.select(event, res)
        if not jobs:
            return []
        subject = res.named[0].name if res.named else f"unnamed:{event.endpoint.key}"
        log.info("%s: %s%s, %d action(s)", event.kind, subject if res.named else "unnamed thing",
                 f" [{', '.join(res.tags)}]" if res.tags else "", len(jobs))
        return jobs
