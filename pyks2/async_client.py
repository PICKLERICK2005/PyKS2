"""Async streaming: AsyncChangesClient (/v1/changes) and async live view frames.

Requires the optional dependencies pulled in by the ``pyks2[async]`` extra
(``websockets`` for the event stream, ``httpx`` for live view). This module
is never imported by ``pyks2/__init__.py`` and does not import those
dependencies until a class/function here is actually constructed or called —
so ``import pyks2`` (and even ``import pyks2.async_client``) stays clean with
neither dependency installed, and the base install stays dependency-light.

NOT yet verified against physical hardware. The sync ``ChangesClient``
handshake and ``MjpegFrameParser`` framing this reuses ARE hardware-verified;
what's untested here is the async transport layer (websockets/httpx) driving
them against the real camera. Treat this module as inferred-correct pending
that verification.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Optional

from . import constants as C
from ._mjpeg import MjpegFrameParser
from .errors import KS2ConnectionError
from .events import _payload_to_event
from .models import ChangeEvent

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore[assignment]

try:
    import websockets
except ImportError:
    websockets = None  # type: ignore[assignment]


def _require(*, need_httpx: bool = False, need_websockets: bool = False) -> None:
    missing: list = []
    if need_httpx and httpx is None:
        missing.append("httpx")
    if need_websockets and websockets is None:
        missing.append("websockets")
    if missing:
        raise ImportError(
            f"pyks2 async support requires: {', '.join(missing)}. "
            "Install with: pip install pyks2[async]"
        )


class AsyncChangesClient:
    """Async counterpart to ``events.ChangesClient``, for ``/v1/changes``.

    Usage:
        >>> async with cam.events_async() as ev:
        ...     async for change in ev:
        ...         if change.is_storage: ...

    Event parsing (JSON decode + ``ChangeEvent`` construction) is the exact
    same code path as the sync client (``events._payload_to_event``) — only
    the transport differs.
    """

    def __init__(self, ip: str, port: int = 80, connect_timeout: float = 5.0):
        _require(need_websockets=True)
        self.ip = ip
        self.port = port
        self.connect_timeout = connect_timeout
        self._ws: Optional[Any] = None

    async def connect(self) -> None:
        """Perform the WebSocket handshake. Raises KS2ConnectionError on
        failure (including if the server refuses to upgrade)."""
        import asyncio

        uri = f"ws://{self.ip}:{self.port}{C.EP.CHANGES}"
        try:
            self._ws = await asyncio.wait_for(
                websockets.connect(uri), timeout=self.connect_timeout)
        except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as e:
            raise KS2ConnectionError(f"/v1/changes connect failed: {e}") from e

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    async def __aenter__(self) -> "AsyncChangesClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def __aiter__(self) -> AsyncIterator[ChangeEvent]:
        if self._ws is None:
            await self.connect()
        ws = self._ws
        assert ws is not None
        async for payload in ws:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8", "replace")
            ev = _payload_to_event(payload)
            if ev is not None:
                yield ev


async def iter_liveview_frames_async(ip: str, max_frames: Optional[int] = None,
                                      timeout: float = C.DOWNLOAD_TIMEOUT
                                      ) -> AsyncIterator[bytes]:
    """Async counterpart to ``K_S2_WiFi.iter_liveview_frames()``.

    Drives the exact same ``MjpegFrameParser`` boundary-scanning logic as the
    sync path — only the transport (``httpx`` instead of ``requests``)
    differs, so there is no duplicated frame-parsing code between them.
    """
    _require(need_httpx=True)
    parser = MjpegFrameParser()
    count = 0
    url = f"http://{ip}{C.EP.LIVEVIEW}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("GET", url) as resp:
                async for chunk in resp.aiter_bytes():
                    for frame in parser.feed(chunk):
                        yield frame
                        count += 1
                        if max_frames and count >= max_frames:
                            return
    except httpx.HTTPError as e:
        raise KS2ConnectionError(f"liveview stream failed: {e}") from e
