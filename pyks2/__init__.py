"""pyks2 — a Python client for the Pentax K-S2 built-in WiFi HTTP API.

A clean, camera-only library built on a complete, hardware-verified map of the
K-S2's undocumented WiFi API. See the docs/ directory for the full protocol
dissection and the reverse-engineering methodology.

Quick start:
    >>> from pyks2 import K_S2_WiFi
    >>> cam = K_S2_WiFi()            # defaults to 192.168.0.1
    >>> cam.ping()
    True
    >>> info = cam.capture(af="off")  # baseline+shoot+wait in one, race-free
    >>> cam.download(info.path, "shot.dng")

Event-driven (no polling):
    >>> with cam.events() as ev:
    ...     for change in ev:
    ...         if change.is_storage:
    ...             print("captured:", cam.latest_info().path)
"""

from __future__ import annotations

from .client import K_S2_WiFi, LiveviewSession
from .errors import (
    KS2APIError,
    KS2ConnectionError,
    KS2Error,
    KS2NotFoundError,
    KS2UnsupportedError,
)
from .events import ChangesClient
from .models import (
    CameraConstants,
    CameraParams,
    ChangeEvent,
    DeviceInfo,
    LensState,
    PhotoEntry,
    PhotoInfo,
    PhotoListing,
    ShootResult,
)

__version__ = "1.0.0"
__author__ = "Jamal El Siblany (pickle)"
__email__ = "jamalsiblani@gmail.com"
__url__ = "https://github.com/PICKLERICK2005/pyks2"

__all__ = [
    "K_S2_WiFi",
    "LiveviewSession",
    "ChangesClient",
    # models
    "CameraParams",
    "CameraConstants",
    "LensState",
    "DeviceInfo",
    "PhotoInfo",
    "PhotoEntry",
    "PhotoListing",
    "ShootResult",
    "ChangeEvent",
    # errors
    "KS2Error",
    "KS2ConnectionError",
    "KS2APIError",
    "KS2UnsupportedError",
    "KS2NotFoundError",
]
