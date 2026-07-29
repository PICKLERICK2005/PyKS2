"""Pure MJPEG multipart frame parsing.

Shared by the sync (``requests``-backed) and async (``httpx``-backed) live
view iterators so the SOI/EOI boundary-scanning logic exists in exactly one
place. This module does no I/O of its own — feed it raw stream bytes as they
arrive, get back complete JPEG frames.
"""

from __future__ import annotations

from typing import List

_SOI = b"\xff\xd8"
_EOI = b"\xff\xd9"


class MjpegFrameParser:
    """Incremental parser for a ``multipart/x-mixed-replace`` MJPEG stream.

    Call ``feed(chunk)`` with each raw chunk read from the stream; it returns
    the list of complete JPEG frames (SOI..EOI) that became available, and
    buffers any trailing partial frame internally.
    """

    def __init__(self) -> None:
        self._buf = b""

    def feed(self, chunk: bytes) -> List[bytes]:
        if not chunk:
            return []
        self._buf += chunk
        frames: List[bytes] = []
        while True:
            soi = self._buf.find(_SOI)
            eoi = self._buf.find(_EOI, soi + 2) if soi >= 0 else -1
            if soi >= 0 and eoi > soi:
                frames.append(self._buf[soi:eoi + 2])
                self._buf = self._buf[eoi + 2:]
            else:
                break
        return frames
