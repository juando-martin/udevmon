# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Juan Domingo Martín
"""The daemon's control socket: JSON-RPC 2.0, one compact JSON object per line.

A Unix stream socket in the user's runtime folder (private to the user; the peer's uid
is checked too). A connection stays open for as many requests as the client sends;
they are answered in order, a batch (a JSON array) with an array. Each request is
handled on the daemon's main loop, like an event, so it is ordered with the events
around it; reading and answering happen in the connection's own thread, so a slow
client never holds the loop up. A request whose answer waits (``trigger`` with
``wait``) holds up the later requests of its connection, not those of others.

A request line is at most :data:`MAX_REQUEST` bytes, a line from the daemon
:data:`MAX_REPLY`. The loop has :data:`HANDLE_TIMEOUT` seconds to answer: a request
it has not started by then is cancelled (``not-run``), one it has started is reported
``still-running``. Errors carry a string code in ``data.error`` (:class:`Refused`).

A handler may start notifications on the connection (:class:`Subscription`); the
daemon's ``subscribe`` sends ``event``, ``handled`` and ``result`` notifications
(``daemon.Daemon``), each from a queue of :data:`QUEUE_SIZE`; a client that does not
keep up loses some, and is told how many by a ``lost`` notification once there is
room again. The daemon never waits for it. SPECIFICATIONS §8.9 has the protocol.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import socket
import struct
import threading
from typing import Callable

from .runtime import socket_path

log = logging.getLogger("udevmon.control")

PROTOCOL = 1                     #: the protocol version that ``hello`` reports
HANDLE_TIMEOUT = 30.0            #: seconds the main loop has to answer a request
MAX_REQUEST = 64 * 1024          #: bytes; a longer request line is refused
MAX_REPLY = 64 * 1024 * 1024     #: bytes; a longer line from the daemon is not read
MAX_CONNECTIONS = 32             #: connections open at once; more are refused
QUEUE_SIZE = 1000                #: notifications waiting for a slow subscriber

PARSE_ERROR = -32700             #: JSON-RPC error code for a line that is not JSON
INVALID_REQUEST = -32600         #: JSON-RPC error code for what is not a request
METHOD_NOT_FOUND = -32601        #: JSON-RPC error code for a method that does not exist
INVALID_PARAMS = -32602          #: JSON-RPC error code for params that do not fit
INTERNAL_ERROR = -32603          #: JSON-RPC error code for a failure in the daemon
REFUSED = 1                      #: the daemon refused; ``data.error`` says why


_KEEP = object()                 # Connection.call: keep the connection's timeout


class NotRunning(Exception):
    """No daemon answers on the control socket."""


class ProtocolError(ValueError):
    """An answer that is not a JSON-RPC 2.0 response to the request."""


class Refused(Exception):
    """An error answer: ``error`` is a fixed string code (``unknown-device``,
    ``bad-request``, ...), ``message`` the text, ``data`` any further fields.

    A handler raises it to refuse a request; :func:`call` raises it for an error answer.
    """

    def __init__(self, error: str, message: str, code: int = REFUSED, **data):
        """``code`` is the JSON-RPC error code: :data:`REFUSED` for a refusal, else one
        of the standard ones."""
        super().__init__(message)
        self.error, self.message, self.code, self.data = error, message, code, data


class BadParams(Refused):
    """A handler's refusal because the params do not fit the method."""

    def __init__(self, message: str):
        """``message`` names the param and what is wrong with it."""
        super().__init__("bad-request", message, INVALID_PARAMS)


class UnknownMethod(Refused):
    """A handler's refusal of a method it does not have."""

    def __init__(self, method: str):
        """``method`` is the name the client asked for."""
        super().__init__("unknown-method", f"no method {method!r}", METHOD_NOT_FOUND)


class Subscription:
    """What a handler returns to start notifications on the connection (see the module).

    ``on_close`` is called (from the connection's thread) when the client goes away or
    the server stops.
    """

    def __init__(self, on_close: Callable[["Subscription"], None] | None = None):
        """``filters`` may be set on it by the handler for its own use (the daemon's
        ``subscribe`` keeps what was asked for there)."""
        self._queue: queue.Queue = queue.Queue(QUEUE_SIZE)
        self._lost = 0
        self._mutex = threading.Lock()
        self.on_close = on_close
        self.closed = False

    def publish(self, method: str, params: dict) -> None:
        """Queue a notification; never blocks (a full queue counts it as lost)."""
        if self.closed:
            return
        try:
            self._queue.put_nowait((method, params))
        except queue.Full:
            with self._mutex:
                self._lost += 1

    def close(self) -> None:
        """Stop the notifications (the sending thread ends)."""
        if self.closed:
            return
        self.closed = True
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            pass
        if self.on_close is not None:
            self.on_close(self)

    def _next(self) -> list[tuple[str, dict]] | None:
        """The next notifications to send (waiting for one), or None once closed: the next
        event, after a ``lost`` notice when events were dropped before it."""
        item = self._queue.get()
        if item is None or self.closed:
            return None
        with self._mutex:
            lost, self._lost = self._lost, 0
        return [("lost", {"count": lost}), item] if lost else [item]


def _peer_uid(conn: socket.socket) -> int | None:
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return struct.unpack("3i", creds)[1]
    except OSError:
        return None


class _Lines:
    """Reads newline-terminated lines from a socket, each at most ``limit`` bytes."""

    def __init__(self, conn: socket.socket, limit: int = MAX_REQUEST):
        """A longer line raises ValueError; the client reads with :data:`MAX_REPLY`."""
        self.conn, self.buf, self.limit = conn, b"", limit

    def next(self) -> bytes | None:
        """The next line without its newline; None at the end of the stream."""
        while b"\n" not in self.buf:
            chunk = self.conn.recv(4096)
            if not chunk:
                return None
            self.buf += chunk
            if len(self.buf) > self.limit and b"\n" not in self.buf:
                raise ValueError(f"line longer than {self.limit} bytes")
        line, _, self.buf = self.buf.partition(b"\n")
        return line


def _error(id_, code: int, error: str, message: str, **data) -> dict:
    """A JSON-RPC error response for request ``id_``, with ``error`` in ``data``."""
    return {"jsonrpc": "2.0", "id": id_,
            "error": {"code": code, "message": message, "data": {"error": error, **data}}}


class ControlServer:
    """Accepts connections and hands each request to ``handler`` on the GLib main loop.

    ``handler(method, params)`` returns ``(result, extra)``: the result to answer with,
    and ``extra``: None, a callable that blocks until the work is done and returns extra
    fields for the result (it runs in the connection's thread, not the loop), or a
    :class:`Subscription`. It raises :class:`Refused` to refuse (:class:`BadParams`,
    :class:`UnknownMethod` for those cases).
    """

    def __init__(self, handler: Callable[[str, dict], tuple[dict, object]], path=None):
        """``path`` is the socket's (default ``runtime.socket_path()``)."""
        self.handler = handler
        self.path = path or socket_path()
        self._sock: socket.socket | None = None
        self._watch = None
        self._mutex = threading.Lock()
        self._connections: set[socket.socket] = set()
        self._subscriptions: set[Subscription] = set()

    def start(self) -> None:
        """Listen on the socket (mode 0600, replacing a stale one) from the GLib main loop."""
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
        """Stop listening, end the subscriptions, close the connections, remove the socket."""
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
        with self._mutex:
            subscriptions, connections = list(self._subscriptions), list(self._connections)
        for sub in subscriptions:
            sub.close()
        for conn in connections:
            try:
                conn.shutdown(socket.SHUT_RDWR)       # wakes the connection's reader
            except OSError:
                pass

    def _accept(self, *_args) -> bool:
        """A new connection (on the main loop): serve it in a thread of its own, or
        refuse it ``busy`` beyond :data:`MAX_CONNECTIONS`."""
        try:
            conn, _ = self._sock.accept()
        except (BlockingIOError, OSError):
            return True
        conn.setblocking(True)
        with self._mutex:
            full = len(self._connections) >= MAX_CONNECTIONS
            if not full:
                self._connections.add(conn)
        if full:
            threading.Thread(target=self._refuse, daemon=True, name="control-busy",
                             args=(conn, _error(None, INTERNAL_ERROR, "busy",
                                                "too many connections"))).start()
            return True
        threading.Thread(target=self._serve, args=(conn,), daemon=True,
                         name="control").start()
        return True

    @staticmethod
    def _send(conn: socket.socket, mutex: threading.Lock, message) -> bool:
        """Write one message as a line (``mutex`` keeps replies and notifications from
        interleaving); False when the client has gone."""
        data = json.dumps(message, separators=(",", ":")).encode() + b"\n"
        try:
            with mutex:
                conn.sendall(data)
            return True
        except OSError:
            return False                              # the client went away

    def _refuse(self, conn: socket.socket, reply: dict) -> None:
        """Send ``reply`` and close. What the client sent is read first (for a second at
        most): closing with it unread would reset the connection, losing the reply."""
        with conn:
            self._send(conn, threading.Lock(), reply)
            try:
                conn.shutdown(socket.SHUT_WR)
                conn.settimeout(1.0)
                while conn.recv(4096):
                    pass
            except OSError:
                pass

    def _serve(self, conn: socket.socket) -> None:
        """A connection's thread: check the peer, then read and answer lines until the
        client closes; on the way out, end its subscriptions."""
        mutex = threading.Lock()                      # one writer at a time: replies, events
        subs: list[Subscription] = []
        try:
            if _peer_uid(conn) != os.getuid():
                self._refuse(conn, _error(None, INVALID_REQUEST, "bad-request",
                                          "not your daemon"))
                return
            lines = _Lines(conn)
            while True:
                try:
                    line = lines.next()
                except ValueError as e:
                    self._refuse(conn, _error(None, INVALID_REQUEST, "bad-request", str(e)))
                    return
                except OSError:
                    return
                if line is None:
                    return
                if not line.strip():
                    continue
                reply = self._answer(line, conn, mutex, subs)
                if reply is not None and not self._send(conn, mutex, reply):
                    return
        finally:
            for sub in subs:
                sub.close()
            with self._mutex:
                self._connections.discard(conn)
            conn.close()

    def _answer(self, line: bytes, conn, mutex, subs):
        """The reply to one line (a request, a notification or a batch), or None."""
        try:
            message = json.loads(line)
        except ValueError as e:
            return _error(None, PARSE_ERROR, "bad-request", f"not JSON: {e}")
        if isinstance(message, list):
            if not message:
                return _error(None, INVALID_REQUEST, "bad-request", "an empty batch")
            replies = [r for r in (self._one(m, conn, mutex, subs) for m in message)
                       if r is not None]
            return replies or None
        return self._one(message, conn, mutex, subs)

    def _one(self, request, conn, mutex, subs):
        """The response to one request object (None for a notification): run it on the
        main loop, wait for a waiting answer, or start a subscription's sender."""
        if not (isinstance(request, dict) and request.get("jsonrpc") == "2.0"
                and isinstance(request.get("method"), str)):
            return _error(request.get("id") if isinstance(request, dict) else None,
                          INVALID_REQUEST, "bad-request", "not a JSON-RPC 2.0 request")
        notification = "id" not in request
        id_ = request.get("id")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return None if notification else _error(id_, INVALID_PARAMS, "bad-request",
                                                    "params must be an object")
        try:
            result, extra = self.on_main_loop(request["method"], params)
            if callable(extra):
                result = {**result, **extra()}
            elif isinstance(extra, Subscription):
                subs.append(extra)
                with self._mutex:
                    self._subscriptions.add(extra)
                closing = extra.on_close

                def closed(sub, closing=closing):
                    with self._mutex:
                        self._subscriptions.discard(sub)
                    if closing is not None:
                        closing(sub)
                extra.on_close = closed
                threading.Thread(target=self._notify, args=(conn, mutex, extra),
                                 daemon=True, name="control-events").start()
        except Refused as e:
            return None if notification else _error(id_, e.code, e.error, e.message, **e.data)
        except Exception as e:                        # never leave the client hanging
            log.exception("control request %r failed", request["method"])
            return None if notification else _error(id_, INTERNAL_ERROR, "internal",
                                                    f"the daemon failed: {e}")
        return None if notification else {"jsonrpc": "2.0", "id": id_, "result": result}

    def _notify(self, conn, mutex, sub: Subscription) -> None:
        """A subscription's sender thread: send its notifications until it is closed or
        the client goes away."""
        while True:
            items = sub._next()
            if items is None:
                return
            for method, params in items:
                if not self._send(conn, mutex, {"jsonrpc": "2.0", "method": method,
                                                "params": params}):
                    sub.close()
                    return

    def on_main_loop(self, method: str, params: dict):
        """Run the handler on the main loop and wait for its answer.

        After :data:`HANDLE_TIMEOUT` a request the loop has not started is cancelled
        (refused ``not-run``: it never runs, so it can be sent again); one that has started
        is refused ``still-running``: it may still complete, so it must not be sent again
        blindly.
        """
        from gi.repository import GLib
        done = threading.Event()
        result: list = []
        mutex = threading.Lock()
        phase = ["queued"]                            # queued -> running, or cancelled

        def call():
            with mutex:
                if phase[0] == "cancelled":
                    return False
                phase[0] = "running"
            try:
                result.append(("ok", self.handler(method, params)))
            except BaseException as e:                # re-raised in the connection's thread
                result.append(("raise", e))
            done.set()
            return False
        GLib.idle_add(call, priority=GLib.PRIORITY_DEFAULT)
        if not done.wait(HANDLE_TIMEOUT):
            with mutex:
                started = phase[0] == "running"
                if not started:
                    phase[0] = "cancelled"
            if started:
                raise Refused("still-running", "the daemon did not answer in time; the "
                              "request is still running and may yet take effect",
                              INTERNAL_ERROR)
            raise Refused("not-run", "the daemon did not answer in time; the request was "
                          "not run", INTERNAL_ERROR)
        kind, value = result[0]
        if kind == "raise":
            raise value
        return value


class Connection:
    """A client connection to the running daemon: :meth:`call` methods on it, and after
    ``subscribe``, read notifications with :meth:`notification`.

    Raises :class:`NotRunning` when no daemon listens.
    """

    def __init__(self, path=None, timeout: float | None = 10.0):
        """``path`` is the socket (default ``runtime.socket_path()``); ``timeout`` the
        seconds a call may take (None: no limit), which :meth:`call` can change."""
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM | socket.SOCK_CLOEXEC)
        self.sock.settimeout(timeout)
        try:
            self.sock.connect(str(path or socket_path()))
        except (FileNotFoundError, ConnectionRefusedError) as e:
            self.sock.close()
            raise NotRunning(str(e)) from None
        self._lines = _Lines(self.sock, MAX_REPLY)
        self._next_id = 0
        self.pending: list[dict] = []                 # notifications read while waiting

    def close(self) -> None:
        """Close the connection (ends a subscription)."""
        self.sock.close()

    def __enter__(self):
        """The connection itself, closed at the end of the ``with``."""
        return self

    def __exit__(self, *exc):
        """Close the connection."""
        self.close()

    def fileno(self) -> int:
        """The socket's file descriptor, to watch for notifications in a main loop."""
        return self.sock.fileno()

    def _read(self) -> dict:
        """The next message from the daemon; :class:`ProtocolError` for one that is not
        JSON-RPC 2.0 (an older daemon's answer says to restart it)."""
        line = self._lines.next()
        if line is None:
            raise NotRunning("the daemon closed the connection")
        try:
            message = json.loads(line)
        except ValueError as e:
            raise ProtocolError(f"not JSON: {e}") from None
        if not isinstance(message, dict):
            raise ProtocolError("not a JSON object")
        if message.get("jsonrpc") != "2.0":
            if "ok" in message:
                raise ProtocolError("the daemon speaks an older protocol; restart it "
                                    "(`systemctl --user restart udevmon`)")
            raise ProtocolError("not a JSON-RPC 2.0 message")
        return message

    def call(self, method: str, params: dict | None = None, timeout=_KEEP):
        """Call ``method`` and return its result; raises :class:`Refused` for an error
        answer, :class:`ProtocolError` for an unusable one. ``timeout`` (seconds, None for
        none) replaces the connection's for this call."""
        self._next_id += 1
        request = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params:
            request["params"] = params
        old = self.sock.gettimeout()
        if timeout is not _KEEP:
            self.sock.settimeout(timeout)
        try:
            self.sock.sendall(json.dumps(request, separators=(",", ":")).encode() + b"\n")
            while True:
                message = self._read()
                if "id" not in message and "method" in message:
                    self.pending.append(message)      # a notification: keep it for later
                    continue
                break
        finally:
            self.sock.settimeout(old)
        if message.get("id") != self._next_id and not (message.get("id") is None
                                                       and "error" in message):
            raise ProtocolError(f"an answer to another request ({message.get('id')!r})")
        if "error" in message:
            error = message["error"] if isinstance(message["error"], dict) else {}
            data = error.get("data") if isinstance(error.get("data"), dict) else {}
            data = dict(data)
            code = data.pop("error", None) or "refused"
            raise Refused(code, str(error.get("message", "the daemon refused")),
                          error.get("code", REFUSED), **data)
        if "result" not in message:
            raise ProtocolError("an answer without a result")
        return message["result"]

    def has_buffered(self) -> bool:
        """Whether a whole message has already been read, so :meth:`notification` returns
        without waiting."""
        return bool(self.pending) or b"\n" in self._lines.buf

    def notification(self) -> tuple[str, dict]:
        """The next notification (method, params), waiting for it; raises
        :class:`NotRunning` when the daemon closes the connection."""
        if self.pending:
            message = self.pending.pop(0)
        else:
            message = self._read()
        return message.get("method"), message.get("params") or {}


def call(method: str, params: dict | None = None, timeout: float | None = 10.0, path=None):
    """Call one method on the running daemon (a connection of its own) and return its result.

    Raises :class:`NotRunning` when no daemon listens, :class:`Refused` for an error
    answer, :class:`ProtocolError` for an unusable one. ``timeout`` None waits as long as
    it takes (for a request that waits for actions to finish).
    """
    with Connection(path, timeout) as conn:
        return conn.call(method, params)
