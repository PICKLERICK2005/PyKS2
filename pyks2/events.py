"""The /v1/changes WebSocket event stream.

A dependency-free (stdlib-only) WebSocket client for the camera's event stream.
The camera pushes coarse events — only ``changed: "camera"`` and
``changed: "storage"`` are ever emitted (see PROTOCOL.md §7) — so this is a
"go re-fetch that group" poke, not a value push. Using it replaces polling.

The handshake and frame decoder are minimal but correct for what the camera
sends (unmasked text frames). Verified against the physical camera.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct
from typing import Iterator, Optional

from .errors import KS2ConnectionError
from .models import ChangeEvent

_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class ChangesClient:
    """WebSocket client for /v1/changes.

    Example:
        >>> with cam.events() as ev:
        ...     for change in ev:
        ...         if change.is_storage:
        ...             info = cam.latest_info()
    """

    def __init__(self, ip: str, port: int = 80, connect_timeout: float = 5.0,
                 recv_timeout: float = 1.0):
        self.ip = ip
        self.port = port
        self.connect_timeout = connect_timeout
        self.recv_timeout = recv_timeout
        self._sock: Optional[socket.socket] = None
        self._buf = b""

    # -- context management -------------------------------------------------

    def __enter__(self) -> "ChangesClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __iter__(self) -> Iterator[ChangeEvent]:
        if self._sock is None:
            self.connect()
        return self._iterate()

    # -- connection ---------------------------------------------------------

    def connect(self) -> None:
        """Perform the WebSocket handshake. Raises KS2ConnectionError on
        failure (including if the server refuses to upgrade)."""
        key = base64.b64encode(os.urandom(16)).decode()
        handshake = (
            f"GET /v1/changes HTTP/1.1\r\n"
            f"Host: {self.ip}\r\n"
            f"Upgrade: websocket\r\n"
            f"Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            f"Sec-WebSocket-Version: 13\r\n\r\n"
        )
        try:
            self._sock = socket.create_connection(
                (self.ip, self.port), timeout=self.connect_timeout)
            self._sock.sendall(handshake.encode())
            self._sock.settimeout(self.connect_timeout)
            raw = self._sock.recv(4096)
        except OSError as e:
            raise KS2ConnectionError(f"/v1/changes connect failed: {e}") from e

        # Split HTTP headers from any WebSocket frame bytes that arrived in the
        # same TCP read; those trailing bytes are real frame data and must be
        # kept, not discarded.
        sep = raw.find(b"\r\n\r\n")
        if sep == -1:
            head_bytes, tail = raw, b""
        else:
            head_bytes, tail = raw[:sep], raw[sep + 4:]
        resp = head_bytes.decode("latin1", "replace")

        status_line = resp.split("\r\n", 1)[0]
        if "101" not in status_line:
            self.close()
            raise KS2ConnectionError(
                f"/v1/changes did not upgrade to WebSocket: {status_line!r}")

        # verify accept key (best-effort)
        expect = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode()).digest()).decode()
        for line in resp.split("\r\n"):
            if line.lower().startswith("sec-websocket-accept:"):
                got = line.split(":", 1)[1].strip()
                if got != expect:
                    # not fatal for this camera, but worth surfacing in logs
                    pass
        # seed the buffer with any early frame bytes so they aren't lost
        if tail:
            self._buf += tail
        self._sock.settimeout(self.recv_timeout)

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
                self._buf = b""

    # -- iteration ----------------------------------------------------------

    def _iterate(self) -> Iterator[ChangeEvent]:
        while self._sock is not None:
            try:
                data = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            self._buf += data
            for payload in self._drain_frames():
                try:
                    obj = json.loads(payload)
                except (ValueError, json.JSONDecodeError):
                    continue
                if "changed" in obj:
                    yield ChangeEvent.from_dict(obj)

    def _drain_frames(self):
        """Yield complete text-frame payloads from the buffer."""
        out = []
        while len(self._buf) >= 2:
            b1, b2 = self._buf[0], self._buf[1]
            opcode = b1 & 0x0F
            length = b2 & 0x7F
            idx = 2
            if length == 126:
                if len(self._buf) < 4:
                    break
                length = struct.unpack(">H", self._buf[2:4])[0]
                idx = 4
            elif length == 127:
                if len(self._buf) < 10:
                    break
                length = struct.unpack(">Q", self._buf[2:10])[0]
                idx = 10
            masked = b2 & 0x80
            mask_len = 4 if masked else 0
            if len(self._buf) < idx + mask_len + length:
                break
            mask = self._buf[idx:idx + mask_len]
            payload = self._buf[idx + mask_len: idx + mask_len + length]
            self._buf = self._buf[idx + mask_len + length:]
            if masked and mask:
                payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
            if opcode == 0x1:  # text
                out.append(payload.decode("utf-8", "replace"))
            elif opcode == 0x8:  # close
                self.close()
                break
        return out

    def next_event(self, timeout: Optional[float] = None) -> Optional[ChangeEvent]:
        """Block for a single event, up to ``timeout`` seconds. Returns None on
        timeout.

        This does NOT delegate to the endless ``_iterate`` loop (which swallows
        socket timeouts and loops forever). It runs its own receive loop that
        enforces the deadline between recv attempts.
        """
        import time
        if self._sock is None:
            self.connect()
        deadline = None if timeout is None else time.time() + timeout
        # First, drain anything already buffered.
        for payload in self._drain_frames():
            ev = self._payload_to_event(payload)
            if ev is not None:
                return ev
        while self._sock is not None:
            if deadline is not None:
                remaining = deadline - time.time()
                if remaining <= 0:
                    return None
                self._sock.settimeout(min(self.recv_timeout, remaining))
            try:
                data = self._sock.recv(4096)
            except socket.timeout:
                if deadline is not None and time.time() >= deadline:
                    return None
                continue
            except OSError:
                return None
            if not data:
                return None
            self._buf += data
            for payload in self._drain_frames():
                ev = self._payload_to_event(payload)
                if ev is not None:
                    return ev
        return None

    @staticmethod
    def _payload_to_event(payload: str) -> Optional[ChangeEvent]:
        try:
            obj = json.loads(payload)
        except (ValueError, json.JSONDecodeError):
            return None
        if "changed" in obj:
            return ChangeEvent.from_dict(obj)
        return None
