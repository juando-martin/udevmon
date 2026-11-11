# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""``udevmon explain``: what would run for a thing, without plugging anything in.

Uses the engine's ``plan()``, so it applies exactly the rules the daemon applies
(named or unnamed, tags, allow_unnamed), and expands the commands with the
thing's properties.
"""

from __future__ import annotations

import shlex

from . import config as cfgmod
from .engine import Engine
from .jsonout import where
from .matching import device_label
from .events import (CONNECTED, DISCONNECTED, MOUNTED, REMOVED, SHUTDOWN, STARTUP,
                     UNMOUNTED, Endpoint, Event, describe)

USB_KINDS = (CONNECTED, DISCONNECTED, STARTUP, SHUTDOWN)    #: event kinds of a USB device
FS_KINDS = (MOUNTED, UNMOUNTED, REMOVED, STARTUP, SHUTDOWN)  #: event kinds of a filesystem
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


def _command(job, event) -> tuple[list[str] | None, str | None]:
    """(argv with the placeholders filled in, None), or (None, the missing placeholder)."""
    props = dict(event.props)
    props.update({f"var_{k}": v for k, v in job.vars.items()})
    props.setdefault("instance", "<instance>")            # only real events have them
    props["state_dir"] = "<state folder>"
    if job.device:
        props["device"] = job.device
    if job.tag:
        props["tag"] = job.tag
    try:
        return list(cfgmod.expand(job.action.run, props)), None
    except KeyError as missing:
        return None, missing.args[0]


def explain_data(cfg: cfgmod.Config, endpoint: Endpoint, kinds=None) -> dict:
    """What each event would do for one thing, as data (``explain --json``).

    ``events`` maps each event kind to the actions it would run (``runs``) and those it
    would skip with the reason (``skips``); an ignored thing has none.
    """
    engine = Engine(cfg, runner=None)
    kinds = tuple(kinds) if kinds else kinds_for(endpoint)
    res = engine.plan(Event(kinds[0], endpoint)).resolution
    out = {"description": describe(endpoint),
           "kind": "filesystem" if endpoint.is_filesystem else "usb",
           "props": dict(endpoint.props),
           "devices": [d.name for d in res.named], "tags": list(res.tags),
           "excluded_tags": list(res.excluded),
           "ignored": [d.name for d in res.ignored], "events": {}}
    if res.ignored:
        return out
    for kind in kinds:
        event = Event(kind, endpoint)
        plan = engine.plan(event)
        runs = []
        for job in plan.jobs:
            if job.action.devices:
                via = {"device": job.device}
            elif job.action.tag_mode == "all":
                via = {"tags": list(job.action.tags)}
            else:
                via = {"tag": job.tag}
            command, missing = _command(job, event)
            runs.append({"action": job.action.name, "via": via, "unnamed": not job.device,
                         "detach": job.action.detach, "lock": job.action.lock,
                         "retries": job.action.retries, "order": job.action.order,
                         **where(cfg, "action", job.action.name),
                         "command": command, "missing": missing})
        out["events"][kind] = {"runs": runs,
                               "skips": [{"action": s.action.name, "reason": s.reason}
                                         for s in plan.skipped]}
    return out


def explain_endpoint(cfg: cfgmod.Config, endpoint: Endpoint, kinds=None,
                     *, header: str | None = None) -> list[str]:
    """Report lines for one thing: its status, then what each event would do."""
    data = explain_data(cfg, endpoint, kinds)
    who = (f"device: {', '.join(device_label(cfg.devices[n]) for n in data['devices'])}"
           if data["devices"] else "unnamed")
    status = (who + (f"   tags: {', '.join(data['tags'])}" if data["tags"] else "")
              + (f"   excluded from: {', '.join(data['excluded_tags'])}"
                 if data["excluded_tags"] else ""))
    lines, quiet = [f"{header or data['description']}   {status}"], []
    if data["ignored"]:
        lines.append(f"  ignored ({', '.join(data['ignored'])}): nothing runs for this thing")
        return lines
    for kind, what in data["events"].items():
        if not what["runs"] and not what["skips"]:
            quiet.append(kind)
            continue
        for run in what["runs"]:
            via = run["via"]
            via = (f"device {via['device']}" if "device" in via
                   else f"tags {' + '.join(via['tags'])}" if "tags" in via
                   else f"tag {via['tag']}")
            extra = (("" if not run["unnamed"] else ", unnamed")
                     + (", detached" if run["detach"] else "")
                     + (f", lock {run['lock']}" if run["lock"] else "")
                     + (f", up to {run['retries']} retries" if run["retries"] else "")
                     + (f", order {run['order']}" if run["order"] != cfgmod.DEFAULT_ORDER
                        else "")
                     + (f", from {label}" if (label := cfg.label("action", run["action"]))
                        else ""))
            command = (shlex.join(run["command"]) if run["command"] is not None
                       else f"(cannot run here: {{{run['missing']}}} is not available)")
            lines.append(f"  {kind:<13} runs {run['action']!r} ({via}{extra})")
            lines.append(f"                  {command}")
        for skip in what["skips"]:
            lines.append(f"  {kind:<13} skips {skip['action']!r}: {skip['reason']}")
    if quiet:
        lines.append(f"  nothing to run for: {', '.join(quiet)}")
    return lines
