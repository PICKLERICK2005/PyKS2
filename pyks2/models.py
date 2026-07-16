"""Typed models for K-S2 API responses.

Light dataclasses over the raw JSON, with defensive parsing for the format
quirks documented in PROTOCOL.md (Law 2: inconsistent datetime and numeric
formats). Every model keeps the original ``raw`` dict so nothing is lost.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional


def _parse_ks2_datetime(s: Optional[str]) -> Optional[datetime]:
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


def _f(v: Any) -> Optional[float]:
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
    model: Optional[str] = None
    firmware_version: Optional[str] = None
    mac_address: Optional[str] = None
    serial_no: Optional[str] = None
    battery: Optional[int] = None
    ssid: Optional[str] = None
    channel: Optional[str] = None
    storages: List[Dict[str, Any]] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DeviceInfo":
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
    av: Optional[str] = None
    tv: Optional[str] = None
    sv: Optional[str] = None
    xv: Optional[str] = None
    wb_mode: Optional[str] = None
    shoot_mode: Optional[str] = None
    exposure_mode: Optional[str] = None
    still_size: Optional[str] = None
    movie_size: Optional[str] = None
    effect: Optional[str] = None
    filter: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CameraParams":
        return cls(
            av=d.get("av"), tv=d.get("tv"), sv=d.get("sv"), xv=d.get("xv"),
            wb_mode=d.get("WBMode"), shoot_mode=d.get("shootMode"),
            exposure_mode=d.get("exposureMode"), still_size=d.get("stillSize"),
            movie_size=d.get("movieSize"), effect=d.get("effect"),
            filter=d.get("filter"), raw=d,
        )

    @property
    def xv_value(self) -> Optional[float]:
        """Exposure compensation as a float (tolerant of '0'/'0.0'/'-0.7')."""
        return _f(self.xv)


@dataclass
class CameraConstants:
    """Static capability lists (from constants/camera).

    NOTE: ``av_list`` is dynamic on the live camera — re-fetch after changes.
    """
    av_list: List[str] = field(default_factory=list)
    tv_list: List[str] = field(default_factory=list)
    sv_list: List[str] = field(default_factory=list)
    xv_list: List[str] = field(default_factory=list)
    wb_mode_list: List[str] = field(default_factory=list)
    shoot_mode_list: List[str] = field(default_factory=list)
    exposure_mode_list: List[str] = field(default_factory=list)
    still_size_list: List[str] = field(default_factory=list)
    reso_list: List[str] = field(default_factory=list)
    movie_reso_list: List[str] = field(default_factory=list)
    movie_size_list: List[str] = field(default_factory=list)
    effect_list: List[str] = field(default_factory=list)
    filter_list: List[str] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CameraConstants":
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


@dataclass
class LensState:
    """Focus state (from params/lens or status/lens)."""
    focused: Optional[bool] = None
    focus_centers: List[Any] = field(default_factory=list)
    focus_mode: Optional[str] = None  # 'af' or 'mf' — read-only (physical lever)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "LensState":
        return cls(
            focused=d.get("focused"),
            focus_centers=d.get("focusCenters", []) or [],
            focus_mode=d.get("focusMode"),
            raw=d,
        )


@dataclass
class PhotoInfo:
    """Per-image metadata (from photos/.../info or latest/info)."""
    dir: Optional[str] = None
    file: Optional[str] = None
    captured: Optional[bool] = None
    av: Optional[str] = None
    tv: Optional[str] = None
    sv: Optional[str] = None
    xv: Optional[str] = None
    orientation: Optional[int] = None
    camera_model: Optional[str] = None
    latlng: Optional[str] = None
    datetime_raw: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PhotoInfo":
        return cls(
            dir=d.get("dir"), file=d.get("file"), captured=d.get("captured"),
            av=d.get("av"), tv=d.get("tv"), sv=d.get("sv"), xv=d.get("xv"),
            orientation=d.get("orientation"), camera_model=d.get("cameraModel"),
            latlng=d.get("latlng"), datetime_raw=d.get("datetime"), raw=d,
        )

    @property
    def path(self) -> Optional[str]:
        if self.dir and self.file:
            return f"{self.dir}/{self.file}"
        return None

    @property
    def datetime(self) -> Optional[datetime]:
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
    entries: List[PhotoEntry] = field(default_factory=list)
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "PhotoListing":
        entries: List[PhotoEntry] = []
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
    focused: Optional[bool] = None
    focus_centers: List[Any] = field(default_factory=list)
    captured: bool = False
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ShootResult":
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
    raw: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "ChangeEvent":
        return cls(changed=d.get("changed", ""), raw=d)

    @property
    def is_storage(self) -> bool:
        return self.changed == "storage"

    @property
    def is_camera(self) -> bool:
        return self.changed == "camera"
