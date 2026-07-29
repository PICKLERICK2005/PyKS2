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

from pyks2.errors import KS2APIError, KS2UnsupportedError

pytest.importorskip("starlette")
pytest.importorskip("uvicorn")
pytest.importorskip("websockets")
pytest.importorskip("httpx")

from pyks2.testing import CameraSimulator, SimulatorServer  # noqa: E402
from pyks2.testing.simulator import fixture_bytes  # noqa: E402


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
    with SimulatorServer() as s:
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
    entries = list(server.client().list_photos())
    names = [e.file for e in entries]
    assert names == sorted(names), "listing must be oldest-first"
    assert len(entries) >= 2


def test_limit_is_a_head_limit_so_newest_needs_a_tail_slice(server):
    cam = server.client()
    full = list(cam.list_photos())
    head = list(cam.list_photos(limit=2))
    assert [e.path for e in head] == [e.path for e in full[:2]]
    # the newest file is NOT reachable via ?limit — it's a head-limit only
    assert full[-1].path not in [e.path for e in head]
    assert full[-1].file == max(e.file for e in full)


def test_latest_info_tracks_the_newest_listed_file(server):
    cam = server.client()
    newest = list(cam.list_photos())[-1].path
    assert cam.latest_info().path == newest


# --- params: the list-emptiness writability signal --------------------------

def test_params_put_round_trip(server):
    cam = server.client()
    assert cam.set_camera_params(av="8.0").av == "8.0"
    assert cam.get_camera_params().av == "8.0"


def test_illegal_value_raises(server):
    with pytest.raises(KS2APIError) as e:
        server.client().set_camera_params(av="99")
    assert e.value.err_code == 400


def test_empty_list_means_camera_controlled_write_is_silently_ignored():
    """PROTOCOL.md 6.5: an empty list means the camera owns that value — the
    PUT still returns 200 and the value does not change."""
    sim = CameraSimulator()
    sim._variables["svList"] = []
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
    """PROTOCOL.md 9: parameters return 412 unless live view is streaming."""
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


# --- the pytest fixture we ship -------------------------------------------

def test_shipped_pytest_fixture_works(ks2_simulator):
    """``ks2_simulator`` comes from our pytest11 entry point, so downstream
    suites get it just by installing pyks2[testing]."""
    assert ks2_simulator.base_url.startswith("http://127.0.0.1:")
    assert ks2_simulator.port > 0
    cam = ks2_simulator.client()
    assert cam.ping() is True
