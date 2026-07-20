"""Async streaming: AsyncChangesClient and iter_liveview_frames_async.

Uses fake stand-ins for `websockets`/`httpx` (mirroring conftest's fake
`requests`) so these run hermetically, with no real network and no physical
camera. NOT hardware-verified — see CHANGELOG.

Tests call asyncio.run() directly rather than depending on a pytest-asyncio
style plugin, so no extra test-runner dependency is needed.
"""
import asyncio

import pytest

pytest.importorskip("websockets")
pytest.importorskip("httpx")

from pyks2 import async_client
from pyks2.errors import KS2ConnectionError


# --- fakes -------------------------------------------------------------

class _FakeWSExceptions:
    class WebSocketException(Exception):
        pass


class _FakeWebsockets:
    exceptions = _FakeWSExceptions

    def __init__(self, connect):
        self.connect = connect


class _FakeWS:
    def __init__(self, payloads):
        self._payloads = payloads

    async def close(self):
        pass

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for p in self._payloads:
            yield p


class _FakeHTTPXResp:
    def __init__(self, chunks):
        self._chunks = chunks

    async def aiter_bytes(self):
        for c in self._chunks:
            yield c


class _FakeStreamCtx:
    def __init__(self, resp):
        self._resp = resp

    async def __aenter__(self):
        return self._resp

    async def __aexit__(self, *exc):
        return False


class _FakeAsyncClient:
    chunks = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method, url):
        return _FakeStreamCtx(_FakeHTTPXResp(self.chunks))


class _FakeHTTPX:
    class HTTPError(Exception):
        pass

    def __init__(self, async_client_cls):
        self.AsyncClient = async_client_cls


# --- AsyncChangesClient ------------------------------------------------

def test_async_changes_client_yields_events(monkeypatch):
    payloads = ['{"errCode":200,"errMsg":"OK","changed":"storage"}',
                '{"errCode":200,"errMsg":"OK","changed":"camera"}']

    async def fake_connect(uri):
        return _FakeWS(payloads)

    monkeypatch.setattr(async_client, "websockets", _FakeWebsockets(fake_connect))

    async def run():
        seen = []
        async with async_client.AsyncChangesClient("1.2.3.4") as ev:
            async for change in ev:
                seen.append(change)
        return seen

    events = asyncio.run(run())
    assert [e.changed for e in events] == ["storage", "camera"]
    assert events[0].is_storage
    assert events[1].is_camera


def test_async_changes_client_ignores_non_change_payloads(monkeypatch):
    payloads = ['{"errCode":200,"errMsg":"OK"}',  # no "changed" key
                '{"errCode":200,"errMsg":"OK","changed":"storage"}']

    async def fake_connect(uri):
        return _FakeWS(payloads)

    monkeypatch.setattr(async_client, "websockets", _FakeWebsockets(fake_connect))

    async def run():
        async with async_client.AsyncChangesClient("1.2.3.4") as ev:
            return [c async for c in ev]

    events = asyncio.run(run())
    assert len(events) == 1
    assert events[0].is_storage


def test_async_changes_client_connect_failure_raises_ks2(monkeypatch):
    async def fake_connect_fail(uri):
        raise OSError("connection refused")

    monkeypatch.setattr(async_client, "websockets", _FakeWebsockets(fake_connect_fail))

    async def run():
        await async_client.AsyncChangesClient("1.2.3.4").connect()

    with pytest.raises(KS2ConnectionError):
        asyncio.run(run())


def test_async_changes_client_raises_clear_error_without_websockets(monkeypatch):
    monkeypatch.setattr(async_client, "websockets", None)
    with pytest.raises(ImportError) as ei:
        async_client.AsyncChangesClient("1.2.3.4")
    assert "pyks2[async]" in str(ei.value)


# --- async liveview ------------------------------------------------------

def test_iter_liveview_frames_async(monkeypatch):
    chunks = [b"\xff\xd8frame1\xff\xd9", b"\xff\xd8frame2\xff\xd9"]

    class Client(_FakeAsyncClient):
        pass
    Client.chunks = chunks
    monkeypatch.setattr(async_client, "httpx", _FakeHTTPX(Client))

    async def run():
        return [f async for f in async_client.iter_liveview_frames_async("1.2.3.4")]

    frames = asyncio.run(run())
    assert frames == chunks


def test_iter_liveview_frames_async_max_frames(monkeypatch):
    chunks = [b"\xff\xd8frame1\xff\xd9", b"\xff\xd8frame2\xff\xd9"]

    class Client(_FakeAsyncClient):
        pass
    Client.chunks = chunks
    monkeypatch.setattr(async_client, "httpx", _FakeHTTPX(Client))

    async def run():
        out = []
        async for f in async_client.iter_liveview_frames_async("1.2.3.4", max_frames=1):
            out.append(f)
        return out

    frames = asyncio.run(run())
    assert frames == chunks[:1]


def test_iter_liveview_frames_async_raises_clear_error_without_httpx(monkeypatch):
    monkeypatch.setattr(async_client, "httpx", None)

    async def run():
        async for _ in async_client.iter_liveview_frames_async("1.2.3.4"):
            pass

    with pytest.raises(ImportError) as ei:
        asyncio.run(run())
    assert "pyks2[async]" in str(ei.value)


# --- client delegating methods -------------------------------------------

def test_client_events_async_returns_configured_client(cam):
    ev = cam.events_async()
    assert isinstance(ev, async_client.AsyncChangesClient)
    assert ev.ip == cam.ip


def test_client_iter_liveview_frames_async_is_async_generator(cam):
    import inspect
    gen = cam.iter_liveview_frames_async()
    assert inspect.isasyncgen(gen)
