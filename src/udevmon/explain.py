"""``udevmon explain``: what would run for a thing, without plugging anything in.

Uses the engine's ``plan()``, so it applies exactly the rules the daemon applies
(named or unnamed, tags, allow_unnamed), and expands the commands with the
thing's properties.
"""

from __future__ import annotations

import shlex

from . import config as cfgmod
from .engine import Engine
from .matching import device_label
from .events import (CONNECTED, DISCONNECTED, MOUNTED, REMOVED, SHUTDOWN, STARTUP,
                     UNMOUNTED, Endpoint, Event, describe)

USB_KINDS = (CONNECTED, DISCONNECTED, STARTUP, SHUTDOWN)
FS_KINDS = (MOUNTED, UNMOUNTED, REMOVED, STARTUP, SHUTDOWN)
_FS_PROPS = {"fs_uuid", "fs_label", "fs_type", "part_uuid", "mountpoint", "surprise",
             "drive_vendor", "drive_model", "drive_serial"}


def kinds_for(endpoint: Endpoint) -> tuple[str, ...]:
    """The event kinds that can happen to ``endpoint``."""
    return FS_KINDS if endpoint.is_filesystem else USB_KINDS


def simulated(cfg: cfgmod.Config, device: str | None, props: dict[str, str],
              event: str | None = None) -> Endpoint:
    """A thing made of a named device's match keys plus extra properties.

    Whether it is a USB device or a filesystem follows the event when that says so
    (mounted/unmounted vs connected/disconnected), else the properties.
    """
    merged: dict[str, str] = {}
    if device is not None:
        merged.update(cfg.devices[device].match)
    merged.update(props)
    if event in (MOUNTED, UNMOUNTED, REMOVED):
        source = "udisks"
    elif event in (CONNECTED, DISCONNECTED):
        source = "udev"
    else:
        source = "udisks" if _FS_PROPS & set(merged) else "udev"
    return Endpoint(source, "(simulated)", merged)


def _status(cfg: cfgmod.Config, plan) -> str:
    res = plan.resolution
    who = f"device: {', '.join(device_label(d) for d in res.named)}" if res.named else "unnamed"
    return (who + (f"   tags: {', '.join(res.tags)}" if res.tags else "")
            + (f"   excluded from: {', '.join(res.excluded)}" if res.excluded else ""))


def _command(job, event) -> str:
    props = dict(event.props)
    props.update({f"var_{k}": v for k, v in job.vars.items()})
    props.setdefault("instance", "<instance>")            # only real events have them
    props["state_dir"] = "<state folder>"
    if job.device:
        props["device"] = job.device
    if job.tag:
        props["tag"] = job.tag
    try:
        return shlex.join(cfgmod.expand(job.action.run, props))
    except KeyError as missing:
        return f"(cannot run here: {{{missing.args[0]}}} is not available)"


def explain_endpoint(cfg: cfgmod.Config, endpoint: Endpoint, kinds=None,
                     *, header: str | None = None) -> list[str]:
    """Report lines for one thing: its status, then what each event would do."""
    engine = Engine(cfg, runner=None)
    kinds = tuple(kinds) if kinds else kinds_for(endpoint)
    lines, quiet = [], []
    first_plan = engine.plan(Event(kinds[0], endpoint))
    lines.append(f"{header or describe(endpoint)}   {_status(cfg, first_plan)}")
    if first_plan.resolution.ignored:
        names = ", ".join(d.name for d in first_plan.resolution.ignored)
        lines.append(f"  ignored ({names}): nothing runs for this thing")
        return lines
    for kind in kinds:
        event = Event(kind, endpoint)
        plan = engine.plan(event)
        if not plan.jobs and not plan.skipped:
            quiet.append(kind)
            continue
        for job in plan.jobs:
            if job.action.devices:
                via = f"device {job.device}"
            elif job.action.tag_mode == "all":
                via = f"tags {' + '.join(job.action.tags)}"
            else:
                via = f"tag {job.tag}"
            unnamed = (("" if job.device else ", unnamed")
                       + (", detached" if job.action.detach else "")
                       + (f", lock {job.action.lock}" if job.action.lock else "")
                       + (f", up to {job.action.retries} retries" if job.action.retries else "")
                       + (f", order {job.action.order}" if job.action.order != cfgmod.DEFAULT_ORDER
                          else "")
                       + (f", from {label}" if (label := cfg.label("action", job.action.name))
                          else ""))
            lines.append(f"  {kind:<13} runs {job.action.name!r} ({via}{unnamed})")
            lines.append(f"                  {_command(job, event)}")
        for skip in plan.skipped:
            lines.append(f"  {kind:<13} skips {skip.action.name!r}: {skip.reason}")
    if quiet:
        lines.append(f"  nothing to run for: {', '.join(quiet)}")
    return lines
