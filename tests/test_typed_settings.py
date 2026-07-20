"""Typed exposure-value accessors: writability gating + native-type encoding."""
from fractions import Fraction

import pytest

import pyks2
from pyks2.models import CameraConstants


def _record(cam, monkeypatch):
    calls = []
    orig = cam.set_camera_params

    def fake(**kw):
        calls.append(kw)
        return orig(**kw)

    monkeypatch.setattr(cam, "set_camera_params", fake)
    return calls


# --- ISO (sv) -------------------------------------------------------------

def test_set_iso_int(cam, monkeypatch):
    calls = _record(cam, monkeypatch)
    cam.set_iso(400)
    assert calls == [{"sv": "400"}]


def test_set_iso_auto_case_insensitive(cam, monkeypatch):
    calls = _record(cam, monkeypatch)
    cam.set_iso("Auto")
    assert calls == [{"sv": "auto"}]


def test_set_iso_camera_controlled_raises():
    cam = pyks2.K_S2_WiFi()
    constants = CameraConstants.from_dict({"svList": []})
    with pytest.raises(pyks2.KS2UnsupportedError):
        cam.set_iso(400, constants=constants)


# --- aperture (av) ---------------------------------------------------------

def test_set_aperture_matches_live_list_string_form(cam, monkeypatch):
    calls = _record(cam, monkeypatch)
    constants = CameraConstants.from_dict({"avList": ["8.0", "10", "11"]})
    cam.set_aperture(10, constants=constants)
    cam.set_aperture(8.0, constants=constants)
    assert calls == [{"av": "10"}, {"av": "8.0"}]


def test_set_aperture_falls_back_when_not_in_list(cam, monkeypatch):
    calls = _record(cam, monkeypatch)
    constants = CameraConstants.from_dict({"avList": ["8.0", "10", "11"]})
    cam.set_aperture(12.5, constants=constants)
    assert calls == [{"av": "12.5"}]


def test_set_aperture_camera_controlled_raises():
    cam = pyks2.K_S2_WiFi()
    constants = CameraConstants.from_dict({"avList": []})
    with pytest.raises(pyks2.KS2UnsupportedError):
        cam.set_aperture(8.0, constants=constants)


# --- shutter speed (tv) ------------------------------------------------------

def test_set_shutter_speed_fraction(cam, monkeypatch):
    calls = _record(cam, monkeypatch)
    constants = CameraConstants.from_dict({"tvList": ["1.100", "30.1"]})
    cam.set_shutter_speed(Fraction(1, 100), constants=constants)
    assert calls == [{"tv": "1.100"}]


def test_set_shutter_speed_whole_seconds_from_int(cam, monkeypatch):
    calls = _record(cam, monkeypatch)
    constants = CameraConstants.from_dict({"tvList": ["1.100", "30.1"]})
    cam.set_shutter_speed(30, constants=constants)
    assert calls == [{"tv": "30.1"}]


def test_set_shutter_speed_camera_controlled_raises():
    """tvList is empty in Av mode (PROTOCOL.md §6.5) — shutter is
    camera-controlled there."""
    cam = pyks2.K_S2_WiFi()
    constants = CameraConstants.from_dict({"tvList": [], "avList": ["4.0"]})
    with pytest.raises(pyks2.KS2UnsupportedError):
        cam.set_shutter_speed(Fraction(1, 100), constants=constants)


# --- exposure comp (xv) ------------------------------------------------------

def test_set_exposure_comp_formats_sign(cam, monkeypatch):
    calls = _record(cam, monkeypatch)
    constants = CameraConstants.from_dict({"xvList": ["0.0", "+0.3", "-0.3"]})
    cam.set_exposure_comp(0, constants=constants)
    cam.set_exposure_comp(0.3, constants=constants)
    cam.set_exposure_comp(-0.7, constants=constants)
    assert calls == [{"xv": "0.0"}, {"xv": "+0.3"}, {"xv": "-0.7"}]


def test_set_exposure_comp_camera_controlled_raises():
    """xvList is observed empty only in Bulb mode (PROTOCOL.md §6.5)."""
    cam = pyks2.K_S2_WiFi()
    constants = CameraConstants.from_dict({"xvList": [], "avList": ["4.0"]})
    with pytest.raises(pyks2.KS2UnsupportedError):
        cam.set_exposure_comp(0.3, constants=constants)


# --- white balance -----------------------------------------------------------

def test_set_wb_passthrough(cam, monkeypatch):
    calls = _record(cam, monkeypatch)
    cam.set_wb("daylight")
    assert calls == [{"WBMode": "daylight"}]


# --- CameraConstants writability signals (models-level) ----------------------

def test_sv_xv_writable_signal():
    m_mode = CameraConstants.from_dict({"svList": ["100", "200"], "xvList": ["0.0"]})
    assert m_mode.sv_writable is True
    assert m_mode.xv_writable is True
    bulb_mode = CameraConstants.from_dict({"svList": ["100"], "xvList": []})
    assert bulb_mode.sv_writable is True
    assert bulb_mode.xv_writable is False
