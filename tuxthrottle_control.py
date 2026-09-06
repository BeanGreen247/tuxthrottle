#!/usr/bin/env python3
"""TuxThrottle control plane — a tiny newline-delimited-JSON RPC over a Unix
domain socket. **stdlib only, no GUI deps.**

`tuxthrottle_powerd.py` runs the server (`ControlServer`) so there is one
process that owns the hardware; `tuxthrottlectl` and the GUI call
`call(method, params)` and fall back to writing the hardware directly when the
socket isn't up (daemon not installed / not running).

Wire protocol (one JSON object per line, both directions):

    -> {"method": "status", "params": {}}
    <- {"ok": true, "result": {...}}
    <- {"ok": false, "error": "message"}

Methods are registered by the server owner (see `tuxthrottle_powerd.run`).
The socket is created 0660 root:root — only root (the GUI runs elevated, the
CLI via sudo) may write; an unprivileged reader just can't connect and the
caller falls back to a direct read.
"""
from __future__ import annotations

import json
import os
import socket
import socketserver
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

RUN_DIR = Path("/run/tuxthrottle")
SOCKET_PATH = RUN_DIR / "control.sock"

Handler = Callable[[dict], object]


# --------------------------------------------------------------------------- #
#  client
# --------------------------------------------------------------------------- #

def available(path: Path = SOCKET_PATH) -> bool:
    """True if the control socket exists and accepts a connection from this
    process. A 0660 root:root socket that exists but refuses us (EACCES) means
    the daemon IS up but we're not root — see `presence()`."""
    return presence(path) == "up"


def presence(path: Path = SOCKET_PATH) -> str:
    """'up' (connectable) | 'root-only' (exists, EACCES) | 'down' (no socket)."""
    if not path.exists():
        return "down"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(str(path))
        return "up"
    except PermissionError:
        return "root-only"
    except OSError:
        return "down"


def call(method: str, params: dict | None = None, *, timeout: float = 5.0,
         path: Path = SOCKET_PATH) -> dict | None:
    """Send one RPC and return the parsed response dict, or None if the socket
    could not be reached (caller should fall back to a direct action)."""
    req = json.dumps({"method": method, "params": params or {}}) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(str(path))
            s.sendall(req.encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError:
        return None
    line = buf.split(b"\n", 1)[0].strip()
    if not line:
        return None
    try:
        return json.loads(line)
    except ValueError:
        return {"ok": False, "error": "bad response from daemon"}


# --------------------------------------------------------------------------- #
#  server
# --------------------------------------------------------------------------- #

class _RPCHandler(socketserver.StreamRequestHandler):
    timeout = 10

    def handle(self) -> None:
        server: Any = self.server  # the _Srv instance; carries .methods
        try:
            raw = self.rfile.readline()
        except OSError:
            return
        if not raw:
            return
        try:
            msg = json.loads(raw.decode(errors="replace"))
            method = str(msg["method"])
            params = msg.get("params") or {}
            if not isinstance(params, dict):
                raise ValueError("params must be an object")
        except (ValueError, KeyError, TypeError) as exc:
            self._reply({"ok": False, "error": f"bad request: {exc}"})
            return
        fn = server.methods.get(method)
        if fn is None:
            self._reply({"ok": False, "error": f"unknown method: {method}"})
            return
        try:
            result = fn(params)
            self._reply({"ok": True, "result": result})
        except Exception as exc:  # noqa: BLE001 — report, don't crash the daemon
            self._reply({"ok": False, "error": f"{type(exc).__name__}: {exc}"})

    def _reply(self, obj: dict) -> None:
        try:
            self.wfile.write((json.dumps(obj, default=str) + "\n").encode())
        except OSError:
            pass


class ControlServer:
    """Threaded Unix-socket RPC server. Register handlers, then `start()`."""

    def __init__(self, path: Path = SOCKET_PATH):
        self.path = Path(path)
        self.methods: dict[str, Handler] = {}
        self._srv: socketserver.UnixStreamServer | None = None
        self._thread: threading.Thread | None = None
        self.register("ping", lambda _p: {"pong": time.time()})

    def register(self, name: str, fn: Handler) -> None:
        self.methods[name] = fn

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.path.parent, 0o755)
        except OSError:
            pass
        if self.path.exists():
            self.path.unlink()

        class _Srv(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            allow_reuse_address = True
            methods: dict[str, Handler] = {}

        self._srv = _Srv(str(self.path), _RPCHandler)
        self._srv.methods = self.methods   # the handler reads this off self.server
        try:
            os.chmod(self.path, 0o660)  # root-only writers
        except OSError:
            pass
        self._thread = threading.Thread(
            target=self._srv.serve_forever, name="tt-control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._srv is not None:
            self._srv.shutdown()
            self._srv.server_close()
            self._srv = None
        try:
            self.path.unlink()
        except OSError:
            pass
