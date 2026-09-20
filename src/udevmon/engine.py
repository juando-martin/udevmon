"""Resolve events to devices/tags, select actions, debounce, and dispatch (spec 5.5, 7)."""

from __future__ import annotations

import logging
import time
from typing import NamedTuple

from .config import Action, Config
from .events import Event
from .matching import resolve

log = logging.getLogger("udevmon.engine")


class Job(NamedTuple):
    action: Action
    device: str | None      # name of the named device that applies (None: unnamed thing)
    tag: str | None         # for tag actions: the tag that selected it


class Skip(NamedTuple):
    action: Action
    reason: str


class Plan(NamedTuple):
    """What an event would do, worked out without side effects (no debounce, no run)."""
    resolution: object
    jobs: list
    skipped: list


class Engine:
    def __init__(self, config: Config, runner, clock=time.monotonic):
        self.config = config
        self.runner = runner
        self._clock = clock
        self._last: dict[tuple[str, str], float] = {}

    def set_config(self, config: Config) -> None:
        self.config = config

    def plan(self, event: Event) -> Plan:
        res = resolve(self.config, event.props)
        skipped: list[Skip] = []
        return Plan(res, self.select(event, res, skipped), skipped)

    def select(self, event: Event, res, skipped: list | None = None) -> list[Job]:
        """The actions that apply to this event, in config order (each at most once)."""
        named = {d.name for d in res.named}
        jobs: list[Job] = []
        for action in self.config.actions:
            if event.kind not in action.on:
                continue
            if action.devices:
                device = next((d.name for d in res.named if d.name in action.devices), None)
                if device is not None:
                    jobs.append(Job(action, device, None))
                continue
            tag = next((t for t in action.tags if t in res.tags), None)
            if tag is None:
                continue
            if not named and not action.allow_unnamed:
                log.debug("%s: unnamed thing has tag %r but action %r does not allow unnamed",
                          event.kind, tag, action.name)
                if skipped is not None:
                    skipped.append(Skip(action, "an unnamed thing, and the action does not "
                                                "set allow_unnamed = true"))
                continue
            jobs.append(Job(action, res.named[0].name if named else None, tag))
        return jobs

    def handle(self, event: Event) -> list[Job]:
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
        key, now = (subject, event.kind), self._clock()
        last = self._last.get(key)
        if last is not None and now - last < self.config.settings.debounce:
            log.debug("debounced %s for %s", event.kind, subject)
            return []
        self._last[key] = now
        log.info("%s: %s%s, %d action(s)", event.kind, subject if res.named else "unnamed thing",
                 f" [{', '.join(res.tags)}]" if res.tags else "", len(jobs))
        self.runner.submit(event, jobs)
        return jobs
