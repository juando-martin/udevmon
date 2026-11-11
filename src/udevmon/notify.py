# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""Desktop notifications for failed actions (``notify_on_failure``).

Sent with the freedesktop Notifications D-Bus interface on the session bus, which needs
no extra program. The bus is the one of the graphical session the actions run in
(found as for DISPLAY, ``session.update``), else the user's standard bus,
``$XDG_RUNTIME_DIR/bus``. With neither, nothing is sent (logged).
"""

from __future__ import annotations

import logging
import os

from . import session

log = logging.getLogger("udevmon.notify")

APP = "udevmon"                  #: the application name notifications carry
TIMEOUT_MS = 5000                #: how long a notification stays, in milliseconds


def session_bus_address(env: dict[str, str] | None = None) -> str | None:
    """The address of the user's session bus, from ``env`` (default: the process
    environment, completed from the graphical session): ``DBUS_SESSION_BUS_ADDRESS``, else
    the ``bus`` socket in ``XDG_RUNTIME_DIR``. None when there is neither."""
    env = dict(os.environ if env is None else env)
    session.update(env)
    address = env.get("DBUS_SESSION_BUS_ADDRESS")
    if address:
        return address
    runtime = env.get("XDG_RUNTIME_DIR")
    if runtime and os.path.exists(os.path.join(runtime, "bus")):
        return f"unix:path={os.path.join(runtime, 'bus')}"
    return None


def send(summary: str, body: str, address: str) -> None:
    """Show one notification on the session bus at ``address``; raises on failure."""
    from gi.repository import Gio, GLib
    flags = (Gio.DBusConnectionFlags.AUTHENTICATION_CLIENT
             | Gio.DBusConnectionFlags.MESSAGE_BUS_CONNECTION)
    conn = Gio.DBusConnection.new_for_address_sync(address, flags, None, None)
    try:
        conn.call_sync(
            "org.freedesktop.Notifications", "/org/freedesktop/Notifications",
            "org.freedesktop.Notifications", "Notify",
            GLib.Variant("(susssasa{sv}i)", (APP, 0, "dialog-error", summary, body, [],
                                             {"urgency": GLib.Variant("y", 2)}, -1)),
            GLib.VariantType("(u)"), Gio.DBusCallFlags.NONE, TIMEOUT_MS, None)
    finally:
        conn.close_sync(None)


def action_failed(action: str, device: str | None, what: str) -> bool:
    """Tell the user that ``action`` failed (``what``: how). True if it was shown."""
    summary = f"udevmon: action {action} failed" + (f" ({device})" if device else "")
    address = session_bus_address()
    if address is None:
        log.info("action %r failed; no session bus, so no notification", action)
        return False
    try:
        send(summary, what, address)
    except Exception as e:                                  # never let this break the runner
        log.warning("could not show a notification for action %r: %s", action, e)
        return False
    log.info("notified that action %r failed", action)
    return True
