"""Model parsing / datetime-format tolerance tests."""
from datetime import datetime
from pyks2.models import _parse_ks2_datetime, PhotoInfo, CameraParams


def test_iso_datetime():
    assert _parse_ks2_datetime("2026-07-15T11:43:15") == datetime(2026, 7, 15, 11, 43, 15)


def test_colon_packed_datetime():
    assert _parse_ks2_datetime("26:07:15:11:43:15") == datetime(2026, 7, 15, 11, 43, 15)


def test_bad_datetime_is_none():
    assert _parse_ks2_datetime("") is None
    assert _parse_ks2_datetime("garbage") is None


def test_xv_tolerant_parse():
    for v in ("0", "0.0", "-0.7", "+1.3"):
        cp = CameraParams.from_dict({"xv": v})
        assert cp.xv_value is not None


def test_photo_info_path():
    pi = PhotoInfo.from_dict({"dir": "100_1507", "file": "IMGP1.DNG"})
    assert pi.path == "100_1507/IMGP1.DNG"
