"""Typed models for K-S2 API responses.

Light dataclasses over the raw JSON, with defensive parsing for the format
quirks documented in PROTOCOL.md (Law 2: inconsistent datetime and numeric
formats). Every model keeps the original ``raw`` dict so nothing is lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def _parse_ks2_datetime(s: str | None) -> datetime | None:
    """Parse either datetime format the camera emits.

    - ISO-8601 from /v1/ping:            2026-07-15T11:43:15
    - colon-packed from photo /info:     26:07:15:11:43:15  (YY:MM:DD:HH:MM:SS)
    Returns None if unparseable/empty.
    """
    if not s:
        return None
    s = s.strip()
    # ISO first
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        pass
    # colon-packed YY:MM:DD:HH:MM:SS
    parts = s.split(":")
    if len(parts) == 6:
        try:
            yy, mm, dd, h, mi, sec = (int(p) for p in parts)
            year = 2000 + yy if yy < 100 else yy
            return datetime(year, mm, dd, h, mi, sec)
        except (ValueError, TypeError):
            return None
    return None


def _f(v: Any) -> float | None:
    """Tolerant float parse for wobbly numeric strings ('0', '0.0', '-0.7')."""
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


@dataclass
class DeviceInfo:
    """Identity + hardware state (from constants/device + status/device)."""
    model: str | None = None
    firmware_version: str | None = None
    mac_address: str | None = None
    serial_no: str | None = None
    battery: int | None = None
    ssid: str | None = None
    channel: str | None = None
    storages: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> DeviceInfo:
        return cls(
            model=d.get("model"),
            firmware_version=d.get("firmwareVersion"),
            mac_address=d.get("macAddress"),
            serial_no=d.get("serialNo"),
            battery=d.get("battery"),
            ssid=d.get("ssid"),
            channel=d.get("channel"),
            storages=d.get("storages", []) or [],
            raw=d,
        )


@dataclass
class CameraParams:
    """Current camera settings (from params/camera)."""
    av: str | None = None
    tv: str | None = None
    sv: str | None = None
    xv: str | None = None
    wb_mode: str | None = None
    shoot_mode: str | None = None
    exposure_mode: str | None = None
    still_size: str | None = None
    movie_size: str | None = None
    effect: str | None = None
    filter: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CameraParams:
        return cls(
            av=d.get("av"), tv=d.get("tv"), sv=d.get("sv"), xv=d.get("xv"),
            wb_mode=d.get("WBMode"), shoot_mode=d.get("shootMode"),
            exposure_mode=d.get("exposureMode"), still_size=d.get("stillSize"),
            movie_size=d.get("movieSize"), effect=d.get("effect"),
            filter=d.get("filter"), raw=d,
        )

    @property
    def xv_value(self) -> float | None:
        """Exposure compensation as a float (tolerant of '0'/'0.0'/'-0.7')."""
        return _f(self.xv)


@dataclass
class CameraConstants:
    """Static capability lists (from constants/camera).

    NOTE: ``av_list`` is dynamic on the live camera — re-fetch after changes.
    """
    av_list: list[str] = field(default_factory=list)
    tv_list: list[str] = field(default_factory=list)
    sv_list: list[str] = field(default_factory=list)
    xv_list: list[str] = field(default_factory=list)
    wb_mode_list: list[str] = field(default_factory=list)
    shoot_mode_list: list[str] = field(default_factory=list)
    exposure_mode_list: list[str] = field(default_factory=list)
    still_size_list: list[str] = field(default_factory=list)
    reso_list: list[str] = field(default_factory=list)
    movie_reso_list: list[str] = field(default_factory=list)
    movie_size_list: list[str] = field(default_factory=list)
    effect_list: list[str] = field(default_factory=list)
    filter_list: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CameraConstants:
        return cls(
            av_list=d.get("avList", []) or [],
            tv_list=d.get("tvList", []) or [],
            sv_list=d.get("svList", []) or [],
            xv_list=d.get("xvList", []) or [],
            wb_mode_list=d.get("WBModeList", []) or [],
            shoot_mode_list=d.get("shootModeList", []) or [],
            exposure_mode_list=d.get("exposureModeList", []) or [],
            still_size_list=d.get("stillSizeList", []) or [],
            reso_list=d.get("resoList", []) or [],
            movie_reso_list=d.get("movieResoList", []) or [],
            movie_size_list=d.get("movieSizeList", []) or [],
            effect_list=d.get("effectList", []) or [],
            filter_list=d.get("filterList", []) or [],
            raw=d,
        )

    @property
    def tv_writable(self) -> bool:
        """Whether shutter (tv) is user-settable in the current mode.

        The camera reports a non-empty ``tvList`` only in modes where the user
        controls shutter (M/Tv/P/TAv). In Av and auto/scene modes it's empty and
        tv writes are silently ignored — so list emptiness is the writability
        signal.
        """
        return len(self.tv_list) > 0

    @property
    def av_writable(self) -> bool:
        """Whether aperture (av) is user-settable in the current mode (same
        list-emptiness signal as tv)."""
        return len(self.av_list) > 0

    @property
    def sv_writable(self) -> bool:
        """Whether ISO (sv) is user-settable in the current mode (same
        list-emptiness signal as tv/av)."""
        return len(self.sv_list) > 0

    @property
    def xv_writable(self) -> bool:
        """Whether exposure comp (xv) is user-settable in the current mode
        (same list-emptiness signal as tv/av); observed empty only in Bulb."""
        return len(self.xv_list) > 0


@dataclass
class LensState:
    """Focus state (from params/lens or status/lens)."""
    focused: bool | None = None
    focus_centers: list[Any] = field(default_factory=list)
    focus_mode: str | None = None  # 'af' or 'mf' — read-only (physical lever)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> LensState:
        return cls(
            focused=d.get("focused"),
            focus_centers=d.get("focusCenters", []) or [],
            focus_mode=d.get("focusMode"),
            raw=d,
        )


@dataclass
class PhotoInfo:
    """Per-image metadata (from photos/.../info or latest/info)."""
    dir: str | None = None
    file: str | None = None
    captured: bool | None = None
    av: str | None = None
    tv: str | None = None
    sv: str | None = None
    xv: str | None = None
    orientation: int | None = None
    camera_model: str | None = None
    latlng: str | None = None
    datetime_raw: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PhotoInfo:
        return cls(
            dir=d.get("dir"), file=d.get("file"), captured=d.get("captured"),
            av=d.get("av"), tv=d.get("tv"), sv=d.get("sv"), xv=d.get("xv"),
            orientation=d.get("orientation"), camera_model=d.get("cameraModel"),
            latlng=d.get("latlng"), datetime_raw=d.get("datetime"), raw=d,
        )

    @property
    def path(self) -> str | None:
        if self.dir and self.file:
            return f"{self.dir}/{self.file}"
        return None

    @property
    def datetime(self) -> datetime | None:
        return _parse_ks2_datetime(self.datetime_raw)


@dataclass
class PhotoEntry:
    """One file in a directory listing."""
    dir: str
    file: str

    @property
    def path(self) -> str:
        return f"{self.dir}/{self.file}"


@dataclass
class PhotoListing:
    """Result of GET /v1/photos — directories and their files."""
    entries: list[PhotoEntry] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PhotoListing:
        entries: list[PhotoEntry] = []
        for dobj in d.get("dirs", []) or []:
            name = dobj.get("name", "")
            for f in dobj.get("files", []) or []:
                entries.append(PhotoEntry(dir=name, file=f))
        return cls(entries=entries, raw=d)

    def __len__(self) -> int:
        return len(self.entries)

    def __iter__(self):
        return iter(self.entries)


@dataclass
class ShootResult:
    """Immediate response to POST /v1/camera/shoot.

    Remember: ``captured`` is always False here — capture is async. Use the
    client's wait_for_capture()/events to detect the written file.
    """
    focused: bool | None = None
    focus_centers: list[Any] = field(default_factory=list)
    captured: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ShootResult:
        return cls(
            focused=d.get("focused"),
            focus_centers=d.get("focusCenters", []) or [],
            captured=bool(d.get("captured", False)),
            raw=d,
        )


@dataclass
class ChangeEvent:
    """A /v1/changes WebSocket event."""
    changed: str  # 'camera' or 'storage'
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ChangeEvent:
        return cls(changed=d.get("changed", ""), raw=d)

    @property
    def is_storage(self) -> bool:
        return self.changed == "storage"

    @property
    def is_camera(self) -> bool:
        return self.changed == "camera"
