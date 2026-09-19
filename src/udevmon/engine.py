"""Match events against assets, debounce, and dispatch actions."""

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
        for asset in self.config.assets.values():
            if not matches(asset.match, props):
                continue
            matched = True
            actions = [a for a in self.config.actions
                       if a.asset == asset.name and event.kind in a.on]
            if not actions:
                log.debug("%s matched asset %r but no action for it", event.kind, asset.name)
                continue
            key = (asset.name, event.kind)
            now = self._clock()
            last = self._last.get(key)
            if last is not None and now - last < self.config.settings.debounce:
                log.debug("debounced %s for asset %r", event.kind, asset.name)
                continue
            self._last[key] = now
            log.info("%s: asset %r matched, %d action(s)", event.kind, asset.name, len(actions))
            jobs.extend((a, asset.name) for a in actions)
        if not matched:
            log.debug("%s: no asset matched (%s)", event.kind, event.device.key)
        if jobs:
            self.runner.submit(event, jobs)
        return jobs
