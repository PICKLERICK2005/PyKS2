"""Shared fixtures: a fake `requests` backed by the captured examples/*.json,
so the whole library can be tested without a physical camera."""
import os
import sys
import types
import pytest

EX = os.path.join(os.path.dirname(__file__), "..", "examples")


def _ex(name):
    with open(os.path.join(EX, name), encoding="utf-8") as f:
        return f.read()


class _Headers(dict):
    def get(self, k, d=None):
        for kk, vv in self.items():
            if kk.lower() == k.lower():
                return vv
        return d


class _Resp:
    def __init__(self, body, status=200, ctype="application/json"):
        self._b = body if isinstance(body, bytes) else body.encode()
        self.status_code = status
        self.headers = _Headers({"Content-Type": ctype})
        self.closed = False

    @property
    def text(self):
        return self._b.decode("utf-8", "replace")

    @property
    def content(self):
        return self._b

    def iter_content(self, n):
        for i in range(0, len(self._b), n):
            yield self._b[i:i + n]

    def close(self):
        self.closed = True


# A 3-frame MJPEG multipart body, boundary text included (the parser ignores
# it and only scans for SOI/EOI, same as the real camera's stream).
LIVEVIEW_FRAMES = [b"\xff\xd8" + b"frame1" + b"\xff\xd9",
                   b"\xff\xd8" + b"frame2" + b"\xff\xd9",
                   b"\xff\xd8" + b"frame3" + b"\xff\xd9"]
LIVEVIEW_BODY = b"".join(
    b"--boundarydonotcross\r\nContent-Type: image/jpeg\r\n\r\n" + f + b"\r\n"
    for f in LIVEVIEW_FRAMES)


class _Session:
    def __init__(self):
        self.headers = {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def update(self, *a):
        pass

    def request(self, method, url, data=None, timeout=None, stream=False):
        p = url.split("192.168.0.1", 1)[-1]
        routes = {
            "/v1/ping": "ping.json",
            "/v1/apis": "apis.json",
            "/v1/props": "props.json",
            "/v1/constants/camera": "constants-camera.json",
            "/v1/variables/camera": "variables-camera.json",
            "/v1/status/camera": "status-camera.json",
            "/v1/status/device": "status-device.json",
            "/v1/params/device": "params-device.json",
            "/v1/status/lens": "status-lens.json",
            "/v1/constants/device": "constants-device.json",
            "/v1/params/lens": "params-lens.json",
        }
        if p in routes:
            return _Resp(_ex(routes[p]))
        if p == "/v1/params/camera":
            if method == "PUT" and "av=99" in (data or ""):
                return _Resp('{"errCode":400,"errMsg":"Bad Request"}')
            return _Resp(_ex("params-camera.json"))
        if p == "/v1/camera/shoot":
            return _Resp(_ex("camera-shoot-response.json"))
        if p in ("/v1/camera/shoot/start", "/v1/camera/shoot/finish"):
            return _Resp('{"errCode":200,"errMsg":"OK"}')
        if p == "/v1/liveview/zoom":
            return _Resp('{"errCode":200,"errMsg":"OK"}')
        if p == "/v1/liveview":
            return _Resp(LIVEVIEW_BODY,
                         ctype="multipart/x-mixed-replace; boundary=--boundarydonotcross")
        if p == "/v1/lens/focus":
            return _Resp('{"errCode":200,"errMsg":"OK","focused":true,"focusCenters":[]}')
        if p == "/v1/photos" or p.startswith("/v1/photos?"):
            return _Resp(_ex("photos-listing.json"))
        if p.endswith("/info"):
            return _Resp(_ex("photos-latest-info.json"))
        if "size=view" in p:
            return _Resp(b"\xff\xd8\xff\xe0" + b"J" * 5000, ctype="image/jpeg")
        if "size=thumb" in p:
            return _Resp('{"errCode":400,"errMsg":"Bad Request"}')
        if p.startswith("/v1/photos/"):
            return _Resp(b"II*\x00" + b"D" * 100000, ctype="application/octet-stream")
        return _Resp('{"errCode":200,"errMsg":"OK"}')


@pytest.fixture
def cam(monkeypatch):
    fake = types.ModuleType("requests")
    fake.Session = _Session

    class _Exc:
        class RequestException(Exception):
            pass

        class ReadTimeout(RequestException):
            pass

        class ConnectionError(RequestException):
            pass

    fake.exceptions = _Exc
    fake.request = lambda m, u, **k: _Session().request(m, u, **k)
    monkeypatch.setitem(sys.modules, "requests", fake)
    # reload client so it picks up the fake
    import importlib
    import pyks2.client
    importlib.reload(pyks2.client)
    return pyks2.client.K_S2_WiFi()
