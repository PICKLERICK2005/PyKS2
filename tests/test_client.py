"""Behavioural tests against the mock camera (captured examples)."""
import pyks2


def test_ping(cam):
    assert cam.ping() is True


def test_apis_count(cam):
    assert len(cam.apis()) == 40


def test_camera_params(cam):
    p = cam.get_camera_params()
    assert p.av == "4.0"
    assert p.exposure_mode == "M"


def test_merged_constants_include_dynamic_av_list(cam):
    c = cam.get_camera_constants()
    assert len(c.av_list) == 16          # from variables/camera
    assert len(c.wb_mode_list) == 14     # from constants/camera
    assert "M" in c.exposure_mode_list


def test_shoot_captured_is_async(cam):
    r = cam.shoot(af="off")
    assert r.focused is True
    assert r.captured is False           # always async


def test_focus(cam):
    assert cam.focus(52, 52).focused is True


def test_list_photos(cam):
    listing = cam.list_photos()
    assert len(listing) == 4
    assert listing.entries[0].path.startswith("100_1507/")


def test_latest_info(cam):
    info = cam.latest_info()
    assert info.path is not None
    # datetime parses the colon-packed format without error
    assert info.datetime is not None


def test_download_view(cam, tmp_path):
    out = tmp_path / "v.jpg"
    n = cam.download("100_1507/IMGP1971.DNG", str(out), size="view")
    assert n > 1000
    assert out.read_bytes()[:2] == b"\xff\xd8"


def test_download_full(cam, tmp_path):
    out = tmp_path / "f.dng"
    n = cam.download("100_1507/IMGP1971.DNG", str(out))
    assert n > 1000


def test_preview_bytes(cam):
    assert cam.preview_bytes("100_1507/IMGP1971.DNG")[:2] == b"\xff\xd8"


def test_set_params_ok(cam):
    cam.set_camera_params(av="8.0", sv="400")   # no raise


def test_illegal_value_rejected(cam):
    import pytest
    with pytest.raises(pyks2.KS2APIError) as ei:
        cam.set_camera_params(av="99")
    assert ei.value.err_code == 400


def test_thumb_unsupported(cam):
    import pytest
    with pytest.raises(pyks2.KS2UnsupportedError):
        cam.download("x/y.DNG", "/tmp/t.jpg", size="thumb")


def test_focus_mode_write_blocked(cam):
    import pytest
    with pytest.raises(pyks2.KS2UnsupportedError):
        cam.set_lens_params(focusMode="mf")


def test_bulb_methods_exist(cam):
    # bulb_start/finish just POST; the mock returns 200, so no raise
    cam.bulb_start()
    cam.bulb_finish()


def test_liveview_zoom_sends_params(cam):
    # mock returns 200 for the zoom endpoint; just verify it round-trips
    r = cam.liveview_zoom(zoom=2)
    assert r.get("errCode") == 200
