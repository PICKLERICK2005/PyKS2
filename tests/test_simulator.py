"""The shipped simulator, driven by the REAL pyks2 client over a real socket.

Every other test module fakes the transport out. These don't: they stand up
``pyks2.testing``'s simulator on loopback and point an actual ``K_S2_WiFi`` at
it, so the requests/httpx/websockets transports and the MJPEG and event parsers
are all genuinely exercised — the coverage the fakes structurally cannot give.

Deliberately self-contained: no shared helper module, so nothing here can
reintroduce the collection failure that made ``tests`` a package. Async tests
call ``asyncio.run`` directly rather than pulling in a pytest-asyncio plugin,
matching ``test_async.py``.
"""
import asyncio
import importlib
import json
import os
import tempfile
import time

import pytest

from pyks2.errors import KS2APIError, KS2ConnectionError, KS2UnsupportedError

pytest.importorskip("starlette")
pytest.importorskip("uvicorn")
pytest.importorskip("websockets")
pytest.importorskip("httpx")

from pyks2.testing import CameraSimulator, SimulatorServer  # noqa: E402
from pyks2.testing.simulator import (  # noqa: E402
    CAMERA_HEADERS,
    ENDPOINTS,
    FAST,
    Timing,
    fixture_bytes,
    paths,
)


@pytest.fixture(autouse=True)
def _real_requests():
    """Rebind ``pyks2.client`` to the real ``requests``.

    conftest's ``cam`` fixture swaps a fake ``requests`` into ``sys.modules``
    and reloads ``pyks2.client``. monkeypatch restores ``sys.modules`` afterwards
    but cannot un-reload the module, so the fake stays bound and would break
    these tests, which need real sockets. Reloading here with the real
    ``requests`` in place undoes that, whatever order the modules ran in.
    """
    import pyks2.client
    importlib.reload(pyks2.client)
    yield


@pytest.fixture
def server():
    """Latency off, so the suite stays quick. Timing itself is covered by the
    handful of tests that ask for it explicitly."""
    with SimulatorServer(timing=FAST) as s:
        yield s


# --- shipped-data plumbing -------------------------------------------------

def test_fixtures_load_from_package_data():
    """The serving data must be reachable as package data, not via examples/.

    This is the trap that would only surface after a real `pip install`: the
    simulator reads through importlib.resources, so if the data stopped
    shipping, this fails.
    """
    raw = fixture_bytes("liveview-frame-raw.bin")
    assert raw.startswith(b"--boundarydonotcross\r\n")
    assert b"\xff\xd8" in raw and b"\xff\xd9" in raw
    props = json.loads(fixture_bytes("props.json"))
    assert props["model"] == "PENTAX K-S2"


def test_fixtures_load_independently_of_cwd(tmp_path, monkeypatch):
    """The shipping invariant: fixtures resolve through package data, never a
    repo-relative examples/ path. Running from an unrelated directory would
    break the latter, so this is the guard.
    """
    import pyks2.testing.simulator as mod
    monkeypatch.chdir(tmp_path)
    mod._cache.clear()
    assert json.loads(mod.fixture_bytes("props.json"))["model"] == "PENTAX K-S2"
    with SimulatorServer() as s:
        assert s.client().ping() is True


def test_camera_json_encoder_reproduces_captured_bytes():
    """The generated-JSON encoder must match the firmware's house style exactly.

    Re-encoding a parsed capture has to give back the original bytes, or dynamic
    responses would look visibly unlike the static ones on the wire.
    """
    from pyks2.testing.simulator import camera_json
    for name in ("photos-listing.json", "photo-info.json",
                 "photos-latest-info.json"):
        raw = fixture_bytes(name)
        assert camera_json(json.loads(raw)) == raw, name


def test_camera_json_matches_the_observed_no_latest_body():
    """This one response is generated rather than replayed (re-capturing it
    needs a power cycle), so pin it against what the camera actually sent."""
    from pyks2.testing.simulator import camera_json
    assert camera_json({"errCode": 200, "errMsg": "OK", "captured": False}) == (
        b'{"errCode": 200,\n "errMsg": "OK",\n "captured": false}\n')


def test_missing_extra_names_the_extra(monkeypatch):
    import pyks2.testing.simulator as mod
    monkeypatch.setattr(mod, "Starlette", None)
    monkeypatch.setattr(mod, "uvicorn", None)
    with pytest.raises(ImportError) as e:
        mod._require()
    assert "pyks2[testing]" in str(e.value)


# --- basic reads ------------------------------------------------------------

def test_ping_and_props(server):
    cam = server.client()
    assert cam.ping() is True
    assert cam.props()["model"] == "PENTAX K-S2"
    assert cam.props()["firmwareVersion"] == "01.10"


def test_host_port_is_split_for_the_websocket(server):
    """The regression this simulator caught: an address carrying a port must
    still produce a valid ws:// URI, not host:PORT:80."""
    cam = server.client()
    assert cam.host == "127.0.0.1"
    assert cam.port == server.port
    assert cam.ip == server.host_port


# --- photos: ordering + head-limit -----------------------------------------

def test_photos_are_oldest_first(server):
    """Ordered by ascending shot number — which is not the same as sorting the
    filenames: the real card holds a RAW+JPEG pair sharing shot number 2224, and
    the camera lists the .JPG before the .DNG.
    """
    import re
    entries = list(server.client().list_photos())
    numbers = [int(re.search(r"(\d+)", e.file).group(1)) for e in entries]
    assert numbers == sorted(numbers), "listing must be oldest-first"
    assert len(entries) >= 2


def test_listing_contains_a_raw_plus_jpeg_pair(server):
    """A wrinkle worth keeping: two entries can share a shot number with
    different extensions, so paths must not be keyed on the number alone."""
    entries = list(server.client().list_photos())
    stems = [e.file.rsplit(".", 1)[0] for e in entries]
    dupes = {s for s in stems if stems.count(s) > 1}
    assert dupes, "expected at least one RAW+JPEG pair in the real listing"
    for stem in dupes:
        exts = {e.file.rsplit(".", 1)[1] for e in entries
                if e.file.startswith(stem + ".")}
        assert exts == {"DNG", "JPG"}


def test_limit_is_a_head_limit_so_newest_needs_a_tail_slice(server):
    cam = server.client()
    full = list(cam.list_photos())
    head = list(cam.list_photos(limit=2))
    assert [e.path for e in head] == [e.path for e in full[:2]]
    # the newest file is NOT reachable via ?limit — it's a head-limit only
    assert full[-1].path not in [e.path for e in head]
    assert full[-1].file == max(e.file for e in full)


def test_limit_keeps_every_directory_even_when_empty(server):
    """Measured: the camera always returns all dirs, giving the ones past the
    limit an empty file list rather than dropping them."""
    cam = server.client()
    full_dirs = cam.list_photos().raw["dirs"]
    limited = cam.list_photos(limit=2).raw["dirs"]
    assert len(limited) == len(full_dirs) > 1
    assert [d["name"] for d in limited] == [d["name"] for d in full_dirs]
    assert sum(len(d["files"]) for d in limited) == 2
    assert limited[-1]["files"] == []


def test_limit_zero_means_no_limit(server):
    """Measured: limit=0 returns everything, not nothing."""
    cam = server.client()
    assert len(list(cam.list_photos(limit=0))) == len(list(cam.list_photos()))


def test_spans_multiple_directories(server):
    """The shipped listing is a real full card, so cross-dir flattening and
    ordering actually get exercised."""
    cam = server.client()
    entries = list(cam.list_photos())
    assert len({e.dir for e in entries}) > 1
    assert len(entries) > 100


def test_no_latest_until_something_is_captured(server):
    """Measured: until the camera captures in this power session it reports
    captured:false with no dir/file, however many files are on the card."""
    cam = server.client()
    assert list(cam.list_photos()), "card is not empty"
    info = cam.latest_info()
    assert info.captured is False
    assert not info.raw.get("dir") and not info.raw.get("file")


def test_latest_info_populates_after_a_capture(server):
    cam = server.client()
    captured = cam.capture(af="auto", timeout=15)
    info = cam.latest_info()
    assert info.captured is True
    assert info.path == captured.path == list(cam.list_photos())[-1].path


# --- params: the list-emptiness writability signal --------------------------

def test_params_put_round_trip(server):
    cam = server.client()
    assert cam.set_camera_params(av="8.0").av == "8.0"
    assert cam.get_camera_params().av == "8.0"


def test_put_params_echoes_a_variables_shaped_body(server):
    """Measured: a PUT returns more than a GET does — the capability lists,
    `state` and `exposureModeOption` come back too."""
    cam = server.client()
    got = cam._request("GET", "/v1/params/camera")
    put = cam._request("PUT", "/v1/params/camera", body="av=8.0")
    assert set(put) - set(got) == {
        "avList", "tvList", "svList", "xvList", "exposureModeOption", "state"}


def test_illegal_value_raises(server):
    with pytest.raises(KS2APIError) as e:
        server.client().set_camera_params(av="99")
    assert e.value.err_code == 400


def test_empty_list_means_camera_controlled_write_is_silently_ignored():
    """PROTOCOL.md 6.5: an empty list means the camera owns that value — the
    PUT still returns 200 and the value does not change."""
    sim = CameraSimulator(timing=FAST)
    sim.set_camera_controlled("sv")          # public API, not private poking
    with SimulatorServer(sim) as s:
        cam = s.client()
        before = cam.get_camera_params().sv
        cam.set_camera_params(sv="6400")          # raw escape hatch: 200, ignored
        assert cam.get_camera_params().sv == before
        # the typed accessor consults the signal first and refuses instead
        with pytest.raises(KS2UnsupportedError):
            cam.set_iso(6400)


def test_typed_setter_works_when_list_is_non_empty(server):
    cam = server.client()
    assert cam.set_iso(400).sv == "400"


# --- capture: the one stateful path ----------------------------------------

def test_capture_makes_a_new_file_appear_then_downloads_it(server):
    cam = server.client()
    before = cam.latest_info().path
    n_before = len(list(cam.list_photos()))

    info = cam.capture(af="auto", timeout=15)

    assert info.path != before
    assert len(list(cam.list_photos())) == n_before + 1
    assert list(cam.list_photos())[-1].path == info.path

    out = os.path.join(tempfile.mkdtemp(), "shot.jpg")
    written = cam.download(info.path, out, size="view")
    assert written > 1000
    with open(out, "rb") as f:
        assert f.read(2) == b"\xff\xd8"          # real JPEG magic


def test_shoot_response_reports_captured_false(server):
    """Capture is asynchronous on this camera; the immediate response never
    says captured."""
    res = server.client().shoot(af="auto")
    assert res.captured is False
    assert res.focused is True


def test_download_thumb_is_unsupported(server):
    cam = server.client()
    newest = list(cam.list_photos())[-1].path
    out = os.path.join(tempfile.mkdtemp(), "x.jpg")
    with pytest.raises(KS2UnsupportedError):
        cam.download(newest, out, size="thumb")


# --- events: sync and async -----------------------------------------------

def test_sync_capture_with_events_gets_exactly_one_storage_event(server):
    cam = server.client()
    info = cam.capture_with_events(af="auto", timeout=15)
    assert info.path
    assert server.simulator.emitted == [
        server.simulator.event_payload("storage")], "a capture emits ONE storage"


def test_async_events_receive_the_capture_event(server):
    cam = server.client()

    async def go():
        seen = []
        async with cam.events_async() as ev:
            async def fire():
                await asyncio.sleep(0.2)
                await asyncio.to_thread(cam.shoot, af="auto")

            task = asyncio.create_task(fire())

            async def collect():
                async for change in ev:
                    seen.append(change)
                    if change.is_storage:
                        return

            await asyncio.wait_for(collect(), timeout=15)
            await task
        return seen

    seen = asyncio.run(go())
    assert [c.changed for c in seen] == ["storage"]
    assert seen[0].is_storage and not seen[0].is_camera
    assert seen[0].raw["errCode"] == 200


def test_async_and_sync_events_agree_on_the_payload(server):
    """The two transports must decode the same captured bytes identically."""
    cam = server.client()

    with cam.events() as ev:
        cam.shoot(af="auto")
        sync_ev = ev.next_event(timeout=15)

    async def go():
        async with cam.events_async() as ev2:
            async def fire():
                await asyncio.sleep(0.2)
                await asyncio.to_thread(cam.shoot, af="auto")

            task = asyncio.create_task(fire())

            async def first():
                async for change in ev2:
                    return change

            got = await asyncio.wait_for(first(), timeout=15)
            await task
            return got

    async_ev = asyncio.run(go())
    assert sync_ev is not None and async_ev is not None
    assert sync_ev.changed == async_ev.changed == "storage"
    assert sync_ev.raw == async_ev.raw


def test_settings_write_emits_a_camera_event(server):
    cam = server.client()
    with cam.events() as ev:
        cam.set_camera_params(av="8.0")
        change = ev.next_event(timeout=15)
    assert change is not None and change.is_camera


# --- live view: MJPEG framing --------------------------------------------

def test_sync_liveview_frames_are_real_jpegs(server):
    frames = list(server.client().iter_liveview_frames(max_frames=3))
    assert len(frames) == 3
    for f in frames:
        assert f[:2] == b"\xff\xd8" and f[-2:] == b"\xff\xd9"
        assert len(f) > 10000


def test_async_liveview_matches_sync(server):
    cam = server.client()
    sync_frames = list(cam.iter_liveview_frames(max_frames=3))

    async def go():
        out = []
        agen = cam.iter_liveview_frames_async(max_frames=3)
        try:
            async for fr in agen:
                out.append(fr)
        finally:
            await agen.aclose()
        return out

    async_frames = asyncio.run(go())
    assert len(async_frames) == 3
    # same captured frame replayed, so the two transports must agree byte-exactly
    assert async_frames == sync_frames


def test_served_framing_is_the_captured_bytes(server):
    """The stream must replay the camera's real part framing, not a
    reconstruction: '--boundarydonotcross' + 'Content-type: image/jpg'."""
    resp = server.client().liveview_stream()
    frames = resp.iter_content(4096)
    try:
        chunk = next(frames)
    finally:
        resp.close()
    assert chunk.startswith(b"--boundarydonotcross\r\n")
    assert b"Content-type: image/jpg" in chunk
    assert resp.headers["Content-Type"] == (
        "multipart/x-mixed-replace; boundary=--boundarydonotcross")


def test_liveview_zoom_is_gated_on_an_active_stream(server):
    """Measured: 412 unless live view is streaming, 200 while it is."""
    cam = server.client()
    with pytest.raises(KS2APIError) as e:
        cam.liveview_zoom(zoom=1)
    assert e.value.err_code == 412

    resp = cam.liveview_stream()
    # Keep the iterator alive: letting it be garbage-collected releases the
    # connection and closes the stream, which would un-gate the endpoint again.
    frames = resp.iter_content(4096)
    try:
        next(frames)                               # stream is now open
        assert cam.liveview_zoom(zoom=1)["errCode"] == 200
    finally:
        resp.close()


def test_zoom_gate_ignores_the_body_even_when_empty(server):
    """Measured, and it corrected the write-up: the gate is purely about whether
    a stream is running. PROTOCOL.md used to claim an empty body returned 200.
    """
    cam = server.client()
    with pytest.raises(KS2APIError) as e:
        cam.liveview_zoom()                       # no params at all
    assert e.value.err_code == 412

    resp = cam.liveview_stream()
    frames = resp.iter_content(4096)
    try:
        next(frames)
        assert cam.liveview_zoom()["errCode"] == 200
        assert cam.liveview_zoom(zoom=1)["errCode"] == 200
    finally:
        resp.close()


# --- Law 1 and the one place the camera breaks it -------------------------

def test_missing_photo_is_404_in_the_body_with_http_200(server):
    cam = server.client()
    for path in ("/v1/photos/999_9999/IMGP9999.DNG/info",
                 "/v1/photos/999_9999/IMGP9999.DNG"):
        with pytest.raises(KS2APIError) as e:
            cam._request("GET", path)
        assert e.value.err_code == 404, path


def test_unknown_path_is_errcode_400_with_http_200(server):
    import requests
    raw = requests.get(f"{server.base_url}/v1/no-such-endpoint", timeout=10)
    assert raw.status_code == 200          # Law 1: HTTP stays 200
    assert raw.json()["errCode"] == 400


def test_unhandled_method_returns_real_400_and_html(server):
    """The single documented exception to Law 1."""
    import requests
    raw = requests.delete(f"{server.base_url}/v1/props", timeout=10)
    assert raw.status_code == 400
    assert "text/html" in raw.headers["Content-Type"]
    assert "<html>" in raw.text and "Bad Request" in raw.text
    # and the client surfaces it as an API error, not a crash
    with pytest.raises(KS2APIError):
        server.client()._request("DELETE", "/v1/props")


def test_camera_response_headers_are_present(server):
    import requests
    h = requests.get(f"{server.base_url}/v1/props", timeout=10).headers
    assert h["Server"] == "server"
    assert h["Pragma"] == "no-cache"
    assert h["Expires"] == "0"
    assert h["Max-Age"] == "0"
    assert h["Accept-Ranges"] == "bytes"
    assert "no-store" in h["Cache-Control"]
    for key in CAMERA_HEADERS:
        assert key in h


def test_event_payload_keeps_the_cameras_trailing_newline(server):
    """The storage frame measures 53 bytes on the wire — the JSON plus a
    newline. Dropping it would make the simulator a byte off."""
    payload = server.simulator.event_payload("storage")
    assert payload.endswith("\n")
    assert len(payload.encode()) == 53
    assert json.loads(payload)["changed"] == "storage"


def test_view_download_is_the_real_preview_not_a_liveview_frame(server):
    """size=view serves a genuine ~53 KB camera preview JPEG."""
    cam = server.client()
    info = cam.capture(af="auto", timeout=15)
    out = os.path.join(tempfile.mkdtemp(), "p.jpg")
    n = cam.download(info.path, out, size="view")
    assert 40_000 < n < 70_000, n
    with open(out, "rb") as f:
        assert f.read(4).hex() == "ffd8ffdb"
    # distinctly larger than a live view frame (~27 KB)
    assert n > len(fixture_bytes("liveview-frame-raw.bin"))


# --- the measured latency model -------------------------------------------

def test_fast_timing_really_is_instant(server):
    started = time.time()
    server.client().props()
    assert time.time() - started < 0.25


def test_realistic_timing_reproduces_measured_latency():
    """Realistic is the default, and it is meant to be slow: a mock that answers
    instantly hides the timeout and ordering bugs this exists to catch."""
    with SimulatorServer(timing=Timing()) as s:
        cam = s.client()
        started = time.time()
        cam.props()
        props_s = time.time() - started
        assert 0.05 < props_s < 0.5, props_s

        # /v1/photos scales with the number of files returned, which is the
        # entire reason ?limit exists.
        started = time.time()
        cam.list_photos()
        full_s = time.time() - started
        started = time.time()
        cam.list_photos(limit=2)
        limited_s = time.time() - started
        assert full_s > limited_s * 2, (full_s, limited_s)
        assert full_s > 1.0, full_s


def test_realistic_capture_reports_no_file_until_it_lands():
    """The ordering that matters: right after the shoot the camera has not
    written anything yet, so `latest` must still be empty."""
    with SimulatorServer(timing=Timing()) as s:
        cam = s.client()
        cam.shoot(af="auto")
        assert cam.latest_info().captured is False, "file appeared too early"
        info = cam.wait_for_capture(since=None, timeout=15)
        assert info.captured and info.path


def test_realistic_liveview_waits_for_the_mirror():
    """First frame only arrives once the mirror is up (~0.8 s measured)."""
    with SimulatorServer(timing=Timing()) as s:
        cam = s.client()
        started = time.time()
        frames = list(cam.iter_liveview_frames(max_frames=2))
        elapsed = time.time() - started
        assert len(frames) == 2
        assert elapsed > 0.5, f"first frame too soon ({elapsed:.2f}s)"


def test_shutdown_is_prompt_after_a_websocket_session():
    """Regression: the /v1/changes handler must notice the peer disconnecting.

    The camera never expects input on that socket, so a handler that only waits
    on its outbound queue never learns the client left. The task leaks and
    server shutdown stalls until it force-times-out — which cost 10s per
    WebSocket test before it was fixed.
    """
    s = SimulatorServer().start()
    cam = s.client()
    with cam.events() as ev:
        cam.shoot(af="auto")
        assert ev.next_event(timeout=15) is not None
    started = time.time()
    s.stop()
    assert time.time() - started < 5.0, "shutdown stalled on a live WebSocket"


# --- the whole public client surface ---------------------------------------

def test_every_public_client_call_works_against_the_simulator(server):
    """The guard that matters: drive the real client's entire camera-facing
    surface against the simulator and assert nothing fails unexpectedly.

    A missing fixture is only discoverable here, and the camera is gone — so
    this iterates the surface rather than trusting a hand-written list. The two
    412s are correct answers, not failures: both are real preconditions.
    """
    from fractions import Fraction

    cam = server.client()
    first = list(cam.list_photos())[0].path

    def liveview_ctx():
        with cam.liveview(max_frames=2) as stream:
            return len(list(stream))

    must_work = {
        "ping": lambda: cam.ping(),
        "apis": lambda: cam.apis(),
        "props": lambda: cam.props(),
        "get_camera_params": lambda: cam.get_camera_params(),
        "get_camera_constants": lambda: cam.get_camera_constants(),
        "get_device_info": lambda: cam.get_device_info(),
        "get_lens_state": lambda: cam.get_lens_state(),
        "get_focus_mode": lambda: cam.get_focus_mode(),
        "get_state": lambda: cam.get_state(),
        "is_idle": lambda: cam.is_idle(),
        "set_camera_params": lambda: cam.set_camera_params(av="8.0"),
        "set_iso": lambda: cam.set_iso(400),
        "set_aperture": lambda: cam.set_aperture(8.0),
        "set_shutter_speed": lambda: cam.set_shutter_speed(Fraction(1, 100)),
        "set_exposure_comp": lambda: cam.set_exposure_comp(0.3),
        "set_wb": lambda: cam.set_wb("daylight"),
        "focus": lambda: cam.focus(0.5, 0.5),
        "shoot": lambda: cam.shoot(af="off"),
        "capture": lambda: cam.capture(af="off", timeout=20),
        "capture_with_events": lambda: cam.capture_with_events(af="off",
                                                              timeout=20),
        "list_photos": lambda: list(cam.list_photos()),
        "list_photos(limit)": lambda: list(cam.list_photos(limit=3)),
        "latest_info": lambda: cam.latest_info(),
        "photo_info": lambda: cam.photo_info(first),
        "preview_bytes": lambda: cam.preview_bytes(first),
        "iter_liveview_frames": lambda: list(
            cam.iter_liveview_frames(max_frames=2)),
        "liveview": liveview_ctx,
    }
    # every read group x subsystem, built from the library's own constants
    for group in ("props", "constants", "params", "variables", "status"):
        must_work[f"{group}()"] = (lambda g=group: getattr(cam, g)())
        for sub in ("camera", "lens", "liveview", "device"):
            must_work[f"{group}({sub})"] = (
                lambda g=group, s=sub: getattr(cam, g)(s))

    failures = []
    for name, call in must_work.items():
        try:
            call()
        except Exception as e:  # noqa: BLE001 - reporting every failure at once
            failures.append(f"{name}: {type(e).__name__}: {e}")
    assert not failures, "these client calls failed:\n  " + "\n  ".join(failures)

    # correct 412s: a precondition the camera really enforces
    for name, call in (("liveview_zoom", lambda: cam.liveview_zoom(zoom=1)),
                       ("bulb_start", lambda: cam.bulb_start()),
                       ("bulb_finish", lambda: cam.bulb_finish())):
        with pytest.raises(KS2APIError) as e:
            call()
        assert e.value.err_code == 412, name


# --- the public configuration API ------------------------------------------

def test_writability_config_replaces_private_poking(server):
    cam, sim = server.client(), server.simulator
    assert sim.writable("sv") is True
    sim.set_camera_controlled("sv")
    assert sim.writable("sv") is False
    before = cam.get_camera_params().sv
    cam.set_camera_params(sv="6400")          # 200, silently ignored
    assert cam.get_camera_params().sv == before
    with pytest.raises(KS2UnsupportedError):
        cam.set_iso(6400)
    sim.set_user_controlled("sv")
    assert sim.writable("sv") is True
    assert cam.set_iso(400).sv == "400"


def test_writability_config_rejects_unknown_fields(server):
    for bad in ("WBMode", "nonsense"):
        with pytest.raises(ValueError):
            server.simulator.set_camera_controlled(bad)


def test_set_exposure_mode_loads_the_real_bulb_capture(server):
    """Bulb is the mode where the camera genuinely owns tv and xv, so this
    empty-list state comes from a capture rather than being synthesised."""
    cam, sim = server.client(), server.simulator
    sim.set_exposure_mode("B")
    assert sim.writable("tv") is False
    assert sim.writable("xv") is False
    assert sim.writable("av") is True
    assert cam.get_camera_params().raw["exposureMode"] == "B"


def test_set_exposure_mode_refuses_modes_with_no_capture(server):
    """Faithfulness guard: inventing a dial position would make the writability
    signal fiction, so only captured modes are accepted.

    The message text is pinned deliberately — the guard's job is not just to
    block but to point at the tool that does what the caller wanted.
    """
    with pytest.raises(ValueError) as e:
        server.simulator.set_exposure_mode("TAV")
    msg = str(e.value)
    assert "'TAV' has no captured capability set" in msg
    assert "set_camera_controlled()" in msg
    assert "set_user_controlled()" in msg
    assert "['B', 'M']" in msg, "should name the modes that do work"


def test_bulb_sequence_writes_a_file_and_fires_one_event(server):
    cam, sim = server.client(), server.simulator
    sim.set_exposure_mode("B")
    before = len(list(cam.list_photos()))
    info = cam.bulb_exposure(0.2)
    assert info.path
    assert len(list(cam.list_photos())) == before + 1
    assert sim.emitted == [sim.event_payload("storage")]


def test_bulb_endpoints_are_gated_on_the_dial(server):
    cam = server.client()
    for call in (cam.bulb_start, cam.bulb_finish):
        with pytest.raises(KS2APIError) as e:
            call()
        assert e.value.err_code == 412


def test_plain_shoot_is_refused_in_bulb_mode(server):
    cam, sim = server.client(), server.simulator
    sim.set_exposure_mode("B")
    with pytest.raises(KS2APIError) as e:
        cam.shoot(af="off")
    assert e.value.err_code == 412


def test_mf_refuses_af_auto_but_allows_af_off(server):
    """Measured: in MF the camera answers af=auto with a 412 — it is not a
    silent no-op. af=off is the MF-safe form."""
    cam, sim = server.client(), server.simulator
    sim.set_focus_mode("mf")
    assert cam.get_focus_mode() == "mf"
    with pytest.raises(KS2APIError) as e:
        cam.shoot(af="auto")
    assert e.value.err_code == 412
    assert cam.shoot(af="off").focused is True


def test_focus_mode_write_is_refused(server):
    """Measured: PUT /v1/params/lens focusMode=... returns 400 and changes
    nothing."""
    cam = server.client()
    with pytest.raises(KS2APIError) as e:
        cam._request("PUT", "/v1/params/lens", body="focusMode=mf")
    assert e.value.err_code == 400
    assert cam.get_focus_mode() == "af"


def test_seed_photos_replaces_the_card(server):
    cam, sim = server.client(), server.simulator
    sim.seed_photos({"100_0101": ["IMGP0001.DNG", "IMGP0002.JPG"],
                     "101_0102": ["IMGP0003.DNG"]})
    assert [e.path for e in cam.list_photos()] == [
        "100_0101/IMGP0001.DNG", "100_0101/IMGP0002.JPG",
        "101_0102/IMGP0003.DNG"]
    assert cam.latest_info().captured is False, "seeding resets the session"
    sim.add_photo("101_0102", "IMGP0004.DNG")
    assert len(list(cam.list_photos())) == 4


# --- the symbolic endpoint constants ---------------------------------------

def test_endpoint_constants_cover_the_whole_route_table():
    """The anti-drift guarantee, checked against the real route table rather
    than a hand-written list."""
    from pyks2.testing.simulator import _UNFAULTABLE, create_app
    app = create_app(CameraSimulator(timing=FAST))
    routed = {r.path for r in app.app.routes}
    faultable = routed - set(_UNFAULTABLE)
    assert faultable == set(ENDPOINTS.values()), (
        f"unnamed routes: {sorted(faultable - set(ENDPOINTS.values()))}; "
        f"stale constants: {sorted(set(ENDPOINTS.values()) - faultable)}")
    assert set(_UNFAULTABLE) == {"/v1/changes", "/{path:path}"}


def test_drift_guard_fires_when_a_route_has_no_constant(monkeypatch):
    """Prove the guard actually bites, rather than trusting that it would."""
    import pyks2.testing.simulator as mod
    shrunk = dict(ENDPOINTS)
    shrunk.pop("PING")
    monkeypatch.setattr(mod, "ENDPOINTS", shrunk)
    with pytest.raises(RuntimeError) as e:
        mod.create_app(CameraSimulator(timing=FAST))
    assert "drifted" in str(e.value)
    assert "/v1/ping" in str(e.value)


def test_constants_match_the_paths_they_name():
    assert paths.SHOOT == "/v1/camera/shoot"
    assert paths.SHOOT_START == "/v1/camera/shoot/start"
    assert paths.SHOOT_FINISH == "/v1/camera/shoot/finish"
    assert paths.LENS_FOCUS == "/v1/lens/focus"
    assert paths.LIVEVIEW_ZOOM == "/v1/liveview/zoom"
    assert set(paths.all()) == set(ENDPOINTS.values())
    assert set(paths.names()) == set(ENDPOINTS)
    assert "/v1/changes" not in paths.all(), "the WebSocket is not fault-able"


def test_faults_accept_a_constant_and_a_raw_path(server):
    """Constants are the documented form; raw strings keep working."""
    cam, sim = server.client(), server.simulator
    sim.fail(paths.SHOOT)
    with pytest.raises(KS2APIError) as e:
        cam.shoot(af="off")
    assert e.value.err_code == 412

    sim.fail("/v1/camera/shoot")            # back-compat
    with pytest.raises(KS2APIError) as e:
        cam.shoot(af="off")
    assert e.value.err_code == 412


def test_templated_constant_matches_any_photo(server):
    """paths.PHOTO_FILE covers every download; a concrete path covers one."""
    cam, sim = server.client(), server.simulator
    entries = [e.path for e in cam.list_photos()]
    first, second = entries[0], entries[1]
    out = os.path.join(tempfile.mkdtemp(), "x.jpg")

    sim.fail(paths.PHOTO_FILE, "not_found", times=None)
    for path in (first, second):
        with pytest.raises(KS2APIError) as e:
            cam.download(path, out, size="view")
        assert e.value.err_code == 404
    sim.clear_faults()

    sim.fail(f"/v1/photos/{first}", "not_found", times=None)
    with pytest.raises(KS2APIError):
        cam.download(first, out, size="view")
    assert cam.download(second, out, size="view") > 1000, "only one photo"


def test_every_constant_is_actually_fault_able(server):
    """Parametrised over the whole surface: injecting on each constant must
    change what that endpoint returns, so no constant is decorative."""
    cam, sim = server.client(), server.simulator
    unaffected = []
    for name, path in ENDPOINTS.items():
        sim.clear_faults()
        sim.fail(path, "not_found", times=None)
        probe = path
        if "{" in path:
            probe = "/v1/photos/100_1507/IMGP1971.DNG" + (
                "/info" if path.endswith("/info") else "")
        try:
            data = cam._request("GET", probe)
        except KS2APIError as e:
            if e.err_code != 404:
                unaffected.append(f"{name}: errCode {e.err_code}")
        except Exception as e:  # noqa: BLE001
            unaffected.append(f"{name}: {type(e).__name__}")
        else:
            unaffected.append(f"{name}: fault ignored, got {list(data)[:3]}")
    sim.clear_faults()
    assert not unaffected, "these constants did not take a fault:\n  " + \
        "\n  ".join(unaffected)


# --- fault injection -------------------------------------------------------

def test_fail_returns_a_real_captured_error_body(server):
    cam, sim = server.client(), server.simulator
    sim.fail("/v1/camera/shoot")                       # 412 once
    with pytest.raises(KS2APIError) as e:
        cam.shoot(af="off")
    assert e.value.err_code == 412
    assert cam.shoot(af="off").focused is True, "fault should be spent"


def test_injected_faults_drain_request_bodies(server):
    """Regression: the fault path must consume the request body before replying.

    Answering a POST/PUT while the client is still sending races the send, and
    the client gets a transport error instead of the injected response — only
    sometimes, which made it a flake rather than a failure. Repeated because
    once-through would not have caught it.
    """
    cam, sim = server.client(), server.simulator
    for _ in range(12):
        sim.fail("/v1/camera/shoot")                   # POST with a body
        with pytest.raises(KS2APIError) as e:
            cam.shoot(af="off")
        assert e.value.err_code == 412
        sim.fail("/v1/params/camera", "bad_request")   # PUT with a body
        with pytest.raises(KS2APIError) as e:
            cam.set_camera_params(av="8.0")
        assert e.value.err_code == 400


def test_delay_still_delivers_the_request_body(server):
    """A delayed request must reach the app intact — the body is drained by the
    fault layer, so it has to be replayed."""
    cam, sim = server.client(), server.simulator
    sim.delay("/v1/params/camera", 0.05)
    assert cam.set_camera_params(av="8.0").av == "8.0"


def test_fail_counts_down_and_then_recovers(server):
    cam, sim = server.client(), server.simulator
    sim.fail("/v1/photos", "bad_request", times=2)
    for _ in range(2):
        with pytest.raises(KS2APIError) as e:
            cam.list_photos()
        assert e.value.err_code == 400
    assert len(list(cam.list_photos())) > 0


def test_fail_forever_until_cleared(server):
    cam, sim = server.client(), server.simulator
    sim.fail("/v1/props", "not_found", times=None)
    for _ in range(3):
        with pytest.raises(KS2APIError) as e:
            cam.props()
        assert e.value.err_code == 404
    sim.clear_faults("/v1/props")
    assert cam.props()["model"] == "PENTAX K-S2"


def test_fail_rejects_uninvented_errors(server):
    """Only captured error bodies are on offer — notably there is no card-full,
    because that response was never captured.

    The message text is pinned so the redirection to the near-full *state*
    survives refactoring.
    """
    sim = server.simulator
    for alias in ("card_full", "cardfull", "card-full"):
        with pytest.raises(ValueError) as e:
            sim.fail(paths.PROPS, alias)
        msg = str(e.value)
        assert "never captured on hardware" in msg, alias
        assert "status-device-cardfull.json" in msg, alias
        assert "remain: 1" in msg, alias

    # anything else still gets the generic listing
    with pytest.raises(ValueError) as e:
        sim.fail(paths.PROPS, "teapot")
    assert "captured errors" in str(e.value)


def test_every_advertised_error_body_is_a_real_fixture():
    from pyks2.testing.simulator import ERROR_BODIES
    for alias, name in ERROR_BODIES.items():
        assert fixture_bytes(name), f"{alias} -> {name} missing"


def test_drop_breaks_the_connection(server):
    cam, sim = server.client(), server.simulator
    sim.drop("/v1/ping")
    with pytest.raises(KS2ConnectionError):
        cam._request("GET", "/v1/ping")
    assert cam.ping() is True, "only the one call should break"


def test_delay_trips_a_client_timeout(server):
    server.simulator.delay("/v1/props", 1.0)
    impatient = server.client(timeout=0.3)
    with pytest.raises(KS2ConnectionError):
        impatient.props()


def test_drop_stream_after_kills_live_view_mid_stream(server):
    cam, sim = server.client(), server.simulator
    sim.drop_stream_after(2)
    seen = 0
    with pytest.raises(Exception):
        for _ in cam.iter_liveview_frames(max_frames=10):
            seen += 1
    assert seen == 2, f"expected the drop after 2 frames, got {seen}"
    sim.clear_faults()
    assert len(list(cam.iter_liveview_frames(max_frames=3))) == 3


def test_a_second_live_view_stream_displaces_the_first(server):
    """Measured over two trials: the camera serves one stream, handing it to the
    newest requester and dropping the previous connection."""
    cam = server.client()
    first = cam.liveview_stream()
    first_chunks = first.iter_content(4096)
    next(first_chunks)                              # first is streaming
    second = cam.liveview_stream()
    second_chunks = second.iter_content(4096)
    next(second_chunks)                             # second takes over
    try:
        with pytest.raises(Exception):
            for _ in range(500):
                next(first_chunks)
    finally:
        first.close()
        second.close()


# --- the pytest fixture we ship -------------------------------------------

def test_fixture_exposes_the_control_object(ks2_simulator):
    """A fixture consumer must be able to reach the config and fault API, not
    just the URL — otherwise the fixture can point a client at the simulator but
    not make it misbehave, which is most of the value.
    """
    sim = ks2_simulator.simulator
    cam = ks2_simulator.client()

    # config through the fixture
    sim.set_focus_mode("mf")
    assert cam.get_focus_mode() == "mf"
    sim.set_focus_mode("af")

    # a fault through the fixture, keyed by constant
    sim.fail(paths.SHOOT)
    with pytest.raises(KS2APIError) as e:
        cam.shoot(af="off")
    assert e.value.err_code == 412
    assert cam.shoot(af="off").focused is True

    # and the addressing a consumer needs
    assert ks2_simulator.base_url.startswith("http://127.0.0.1:")
    assert ks2_simulator.host_port.endswith(str(ks2_simulator.port))


def test_realistic_fixture_also_exposes_control(ks2_simulator_realistic):
    sim = ks2_simulator_realistic.simulator
    assert sim.writable("sv") is True
    sim.set_camera_controlled("sv")
    assert sim.writable("sv") is False


def test_shipped_pytest_fixture_works(ks2_simulator):
    """``ks2_simulator`` comes from our pytest11 entry point, so downstream
    suites get it just by installing pyks2[testing]."""
    assert ks2_simulator.base_url.startswith("http://127.0.0.1:")
    assert ks2_simulator.port > 0
    cam = ks2_simulator.client()
    assert cam.ping() is True
