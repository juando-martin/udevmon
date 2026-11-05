"""The daemon's control socket: the CLI asks the running daemon to do something now.

A Unix stream socket in the user's runtime folder (private to the user; the peer's uid
is checked too). One request per connection: a JSON object on one line, answered by a
JSON object on one line. The request is handled on the daemon's main loop, like an
event, so it is ordered with the events around it; reading and answering happen in a
short-lived thread, so a slow client never holds the loop up.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import struct
import threading
from typing import Callable

from .runtime import socket_path

log = logging.getLogger("udevmon.control")

READ_TIMEOUT = 5.0               # seconds a client has to send its request
HANDLE_TIMEOUT = 30.0            # seconds the main loop has to answer it
MAX_REQUEST = 64 * 1024


class NotRunning(Exception):
    """No daemon answers on the control socket."""


def _peer_uid(conn: socket.socket) -> int | None:
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[1]
    except OSError:
        return None


def _read_line(conn: socket.socket) -> bytes:
    data = b""
    while b"\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data += chunk
        if len(data) > MAX_REQUEST:
            raise ValueError("request too long")
    return data.split(b"\n", 1)[0]


def _send(conn: socket.socket, reply: dict) -> None:
    try:
        conn.sendall(json.dumps(reply).encode() + b"\n")
    except OSError:
        pass                                          # the client went away: nothing to do


class ControlServer:
    """Accepts requests and hands each to ``handler`` on the GLib main loop.

    ``handler(request)`` returns ``(reply, wait)``: the reply to send, and, when the
    client asked to wait, a callable that blocks until the work is done and returns
    extra fields for the reply (it runs in the connection's thread, not the loop).
    """

    def __init__(self, handler: Callable[[dict], tuple[dict, Callable[[], dict] | None]],
                 path=None):
        self.handler = handler
        self.path = path or socket_path()
        self._sock: socket.socket | None = None
        self._watch = None

    def start(self) -> None:
        from gi.repository import GLib
        try:
            os.unlink(self.path)                      # left by a crash; we hold the lock
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        old = os.umask(0o177)
        try:
            sock.bind(str(self.path))
        finally:
            os.umask(old)
        sock.listen(8)
        sock.setblocking(False)
        self._sock = sock
        self._watch = GLib.io_add_watch(sock.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN,
                                        self._accept)

    def stop(self) -> None:
        from gi.repository import GLib
        if self._watch is not None:
            GLib.source_remove(self._watch)
            self._watch = None
        if self._sock is not None:
            self._sock.close()
            self._sock = None
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass

    def _accept(self, *_args) -> bool:
        try:
            conn, _ = self._sock.accept()
        except (BlockingIOError, OSError):
            return True
        threading.Thread(target=self._serve, args=(conn,), daemon=True,
                         name="control").start()
        return True

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            if _peer_uid(conn) != os.getuid():
                _send(conn, {"ok": False, "message": "not your daemon"})
                return
            conn.settimeout(READ_TIMEOUT)
            try:
                request = json.loads(_read_line(conn) or b"null")
                if not isinstance(request, dict):
                    raise ValueError("not a JSON object")
            except (OSError, ValueError) as e:
                _send(conn, {"ok": False, "message": f"bad request: {e}"})
                return
            reply, wait = self.on_main_loop(request)
            if wait is not None:
                try:
                    reply.update(wait())
                except Exception as e:                # never leave the client hanging
                    log.exception("control request failed while waiting")
                    reply = {"ok": False, "message": f"failed while waiting: {e}"}
            conn.settimeout(None)
            _send(conn, reply)

    def on_main_loop(self, request: dict):
        """Run the handler on the main loop and wait for its answer."""
        from gi.repository import GLib
        done = threading.Event()
        result: list = []

        def call():
            try:
                result.append(self.handler(request))
            except Exception as e:
                log.exception("control request failed")
                result.append(({"ok": False, "message": f"the daemon failed: {e}"}, None))
            done.set()
            return False
        GLib.idle_add(call, priority=GLib.PRIORITY_DEFAULT)
        if not done.wait(HANDLE_TIMEOUT):
            return {"ok": False, "message": "the daemon did not answer in time"}, None
        return result[0]


def request(payload: dict, timeout: float | None = 10.0, path=None) -> dict:
    """Send one request to the running daemon and return its reply.

    NotRunning when no daemon listens. ``timeout`` None waits as long as it takes (for a
    request that waits for actions to finish).
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
    with sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(path or socket_path()))
        except (FileNotFoundError, ConnectionRefusedError) as e:
            raise NotRunning(str(e)) from None
        sock.sendall(json.dumps(payload).encode() + b"\n")
        line = _read_line(sock)
    if not line:
        raise NotRunning("the daemon closed the connection")
    return json.loads(line)
