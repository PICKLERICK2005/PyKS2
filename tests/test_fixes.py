"""Regression tests for the four correctness fixes (reviewer P0 issues)."""
import time
import pytest
import pyks2


# --- Issue 1: wait_for_capture must not return the pre-existing photo -------

def test_wait_for_capture_ignores_baseline(cam):
    """With `since` set to the current latest, and nothing new shot, it must
    time out rather than instantly returning the old photo."""
    baseline = cam.latest_info().path
    with pytest.raises(pyks2.KS2ConnectionError):
        cam.wait_for_capture(since=baseline, timeout=0.3, poll_interval=0.1)


def test_wait_for_capture_returns_new_path(cam, monkeypatch):
    """When the latest path changes, it returns the new one."""
    seq = ["100_1507/IMGP0001.DNG", "100_1507/IMGP0001.DNG",
           "100_1507/IMGP0002.DNG"]
    calls = {"i": 0}

    def fake_latest():
        i = min(calls["i"], len(seq) - 1)
        calls["i"] += 1
        return pyks2.PhotoInfo.from_dict(
            {"captured": True, "dir": "100_1507",
             "file": seq[i].split("/")[1]})

    monkeypatch.setattr(cam, "latest_info", fake_latest)
    info = cam.wait_for_capture(since="100_1507/IMGP0001.DNG",
                                timeout=2, poll_interval=0.01)
    assert info.path == "100_1507/IMGP0002.DNG"


def test_capture_records_baseline_before_shoot(cam, monkeypatch):
    """capture() must snapshot the baseline, shoot, then wait for a NEW file."""
    events = []
    monkeypatch.setattr(cam, "shoot",
                        lambda af=None: events.append("shoot") or
                        pyks2.ShootResult.from_dict({"focused": True}))
    latest_seq = ["100_1507/IMGP0341.DNG",   # baseline read
                  "100_1507/IMGP0342.DNG"]    # after shoot
    calls = {"i": 0}

    def fake_latest():
        p = latest_seq[min(calls["i"], len(latest_seq) - 1)]
        calls["i"] += 1
        d, f = p.split("/")
        return pyks2.PhotoInfo.from_dict({"captured": True, "dir": d, "file": f})

    monkeypatch.setattr(cam, "latest_info", fake_latest)
    info = cam.capture(af="off", timeout=2)
    assert "shoot" in events
    assert info.path == "100_1507/IMGP0342.DNG"


# --- Issue 3: convenience methods merge the right endpoints -----------------

def test_get_device_info_merges_params_device(cam):
    """ssid/channel live in params/device and must be populated."""
    dev = cam.get_device_info()
    assert dev.model == "PENTAX K-S2"       # from constants/device
    assert dev.battery == 100               # from status/device
    assert dev.ssid == "PENTAX_XXXXXX"      # from params/device (the fix)
    assert dev.channel == "1"


def test_get_lens_state_merges_status_lens(cam):
    """focused/focusCenters live in status/lens; focusMode in params/lens."""
    lens = cam.get_lens_state()
    assert lens.focus_mode == "af"          # from params/lens
    assert lens.focused is False            # from status/lens (the fix)
    assert lens.focus_centers == []


# --- Issue 4: download is atomic and cleans up ------------------------------

def test_download_atomic_no_partial_on_error(cam, monkeypatch, tmp_path):
    """A JSON error body must NOT leave a file (or a .part) at the target."""
    out = tmp_path / "shot.dng"

    # Force the download endpoint to return a JSON error body.
    orig = cam._request

    def fake_request(method, path, **kw):
        if path.startswith("/v1/photos/") and kw.get("raw"):
            class R:
                status_code = 200
                headers = {"Content-Type": "application/json"}
                def iter_content(self, n):
                    yield b'{"errCode": 404, "errMsg": "Not Found"}'
                def close(self): pass
                @property
                def text(self): return '{"errCode":404}'
            return R()
        return orig(method, path, **kw)

    monkeypatch.setattr(cam, "_request", fake_request)
    with pytest.raises(pyks2.KS2APIError):
        cam.download("100_1507/NOPE.DNG", str(out))
    assert not out.exists()
    assert not (tmp_path / "shot.dng.part").exists()


def test_download_success_atomic_rename(cam, tmp_path):
    """A good download lands at the final path with no .part left behind."""
    out = tmp_path / "ok.jpg"
    n = cam.download("100_1507/IMGP1971.DNG", str(out), size="view")
    assert n > 1000
    assert out.exists()
    assert not (tmp_path / "ok.jpg.part").exists()
    assert out.read_bytes()[:2] == b"\xff\xd8"


# --- Issue 2: next_event actually times out ---------------------------------

def test_next_event_times_out():
    """next_event(timeout) must return within the deadline on an idle socket,
    not loop forever."""
    import socket as _s
    from pyks2.events import ChangesClient

    class FakeSock:
        def __init__(self): self.timeout = None
        def settimeout(self, t): self.timeout = t
        def recv(self, n):
            # simulate an idle connection: always times out
            raise _s.timeout()
        def close(self): pass

    c = ChangesClient("1.2.3.4")
    c._sock = FakeSock()
    start = time.time()
    result = c.next_event(timeout=0.2)
    elapsed = time.time() - start
    assert result is None
    assert elapsed < 1.0, f"next_event ran {elapsed:.2f}s, should be ~0.2s"


# --- Gap-closing findings (post-probe) --------------------------------------

def test_tv_writable_signal(cam, monkeypatch):
    """tv_writable reflects tvList emptiness (M has it, Av doesn't)."""
    from pyks2.models import CameraConstants
    m_mode = CameraConstants.from_dict({"tvList": ["30.1", "1.100"], "avList": []})
    assert m_mode.tv_writable is True
    av_mode = CameraConstants.from_dict({"tvList": [], "avList": ["4.0", "8.0"]})
    assert av_mode.tv_writable is False
    assert av_mode.av_writable is True


def test_exposure_mode_option_ignored():
    """exposureModeOption is empty on this body — parsing it shouldn't break."""
    from pyks2.models import CameraParams
    cp = CameraParams.from_dict({"exposureMode": "M", "exposureModeOption": ""})
    assert cp.exposure_mode == "M"


# --- Full-map findings ------------------------------------------------------

def test_shoot_200_but_not_captured_when_full(cam, monkeypatch):
    """A 200 from shoot with captured:false must NOT be mistaken for success.
    wait_for_capture (baseline) is what actually proves a frame landed."""
    # shoot returns captured:false (mock default) — the ShootResult reflects it
    r = cam.shoot(af="off")
    assert r.captured is False  # never trust this as "saved"


def test_bulb_start_finish_ok(cam):
    """bulb_start/finish issue POSTs without error against the mock."""
    cam.bulb_start()
    cam.bulb_finish()


def test_storage_remain_is_frame_count(cam):
    """remain is a frame count; DeviceInfo surfaces storages raw for callers."""
    dev = cam.get_device_info()
    assert dev.storages, "expected at least one storage entry"
    assert "remain" in dev.storages[0]


# --- Review round 2 fixes ---------------------------------------------------

def test_camera_constants_exposes_movie_and_reso_lists(cam):
    """resoList/movieResoList/movieSizeList must be surfaced, not dropped."""
    cc = cam.get_camera_constants()
    assert cc.reso_list == ["1080x720", "720x480"]
    assert cc.movie_reso_list == ["1280x720", "720x404"]
    assert cc.movie_size_list == ["FHD30p", "FHD25p", "FHD24p", "HD60p", "HD50p"]


def test_ws_handshake_preserves_trailing_frame_bytes(monkeypatch):
    """If a frame arrives in the same TCP read as the 101 headers, it must be
    kept in the buffer, not discarded."""
    import socket as _s
    from pyks2.events import ChangesClient
    import base64
    import hashlib

    # build a valid-looking handshake response + one text frame appended
    key_holder = {}

    class FakeSock:
        def __init__(self):
            self._sent = b""
            self.timeout = None
        def sendall(self, b):
            self._sent += b
            # capture the client's Sec-WebSocket-Key to echo a valid accept
            for line in b.decode("latin1").split("\r\n"):
                if line.lower().startswith("sec-websocket-key:"):
                    key_holder["k"] = line.split(":", 1)[1].strip()
        def settimeout(self, t):
            self.timeout = t
        def recv(self, n):
            accept = base64.b64encode(hashlib.sha1(
                (key_holder["k"] +
                 "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()
            headers = (f"HTTP/1.1 101 Switching Protocols\r\n"
                       f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                       f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
            # a text frame carrying an event, appended to the same read
            payload = b'{"errCode":200,"errMsg":"OK","changed":"storage"}'
            frame = bytes([0x81, len(payload)]) + payload
            return headers + frame
        def close(self):
            pass

    monkeypatch.setattr(_s, "create_connection", lambda *a, **k: FakeSock())
    c = ChangesClient("1.2.3.4")
    c.connect()
    # the trailing frame should already be buffered and decodable immediately
    ev = c.next_event(timeout=0.1)
    assert ev is not None and ev.is_storage
    c.close()
