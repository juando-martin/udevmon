"""Match events against devices, debounce, and dispatch actions."""

from __future__ import annotations

import logging
import time

from .config import Action, Config
from .events import Event
from .matching import matches

log = logging.getLogger("udevmon.engine")


class Engine:
    def __init__(self, config: Config, runner, clock=time.monotonic):
        self.config = config
        self.runner = runner
        self._clock = clock
        self._last: dict[tuple[str, str], float] = {}

    def set_config(self, config: Config) -> None:
        self.config = config

    def handle(self, event: Event) -> list[tuple[Action, str]]:
        props = event.props
        jobs: list[tuple[Action, str]] = []
        matched = False
        for device in self.config.devices.values():
            if not matches(device.match, props):
                continue
            matched = True
            actions = [a for a in self.config.actions
                       if a.device == device.name and event.kind in a.on]
            if not actions:
                log.debug("%s matched device %r but no action for it", event.kind, device.name)
                continue
            key = (device.name, event.kind)
            now = self._clock()
            last = self._last.get(key)
            if last is not None and now - last < self.config.settings.debounce:
                log.debug("debounced %s for device %r", event.kind, device.name)
                continue
            self._last[key] = now
            log.info("%s: device %r matched, %d action(s)", event.kind, device.name, len(actions))
            jobs.extend((a, device.name) for a in actions)
        if not matched:
            log.debug("%s: no device matched (%s)", event.kind, event.endpoint.key)
        if jobs:
            self.runner.submit(event, jobs)
        return jobs
