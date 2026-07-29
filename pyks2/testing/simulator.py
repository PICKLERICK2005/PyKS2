"""A protocol-level Pentax K-S2 simulator, served over a real socket.

This exists so the *real* ``pyks2`` client — sync and async — can be exercised
end to end without a camera on the bench, and so downstream libraries can run
their own integration tests against a faithful fake rather than mocking
``pyks2`` out.

Faithfulness is the whole point, so the wire data here is the real thing: every
response body is replayed from bytes captured off a physical K-S2 (firmware
01.10) and shipped inside this package as ``pyks2/testing/data/``. Nothing is
hand-written to look plausible. Two deliberate exceptions are called out in
``data/PROVENANCE.md``.

The behaviours it reproduces, all of them verified against hardware and
documented in ``docs/PROTOCOL.md``:

* **Law 1** — ``errCode`` lives in the JSON body; the HTTP status stays 200.
* ``/v1/photos`` lists directories **oldest-first**, and ``?limit=N`` is a
  **head**-limit (no offset/cursor), so callers wanting the newest file must
  slice from the tail.
* An **empty** ``avList``/``tvList``/``svList``/``xvList`` means the camera owns
  that value in the current exposure mode: a PUT still returns 200 and is
  **silently ignored** (PROTOCOL.md §6.5).
* A capture emits **exactly one** ``storage`` event on ``/v1/changes``; a
  settings write emits ``camera``.
* ``POST /v1/liveview/zoom`` with parameters returns **412** unless live view is
  actively streaming, **200** while it is (PROTOCOL.md §9).

State is deliberately shallow: only capture mutates anything. A shoot makes a
new file appear in ``/v1/photos`` and fires the matching ``storage`` event, so a
shoot → new-file → download sequence works. Everything else is canned. This is
not a full camera model and should not become one.

Requires the ``pyks2[testing]`` extra. ``import pyks2`` and even
``import pyks2.testing.simulator`` stay clean without it; only building or
running the app raises.
"""

from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

try:  # pragma: no cover - exercised by the extra-missing path
    from starlette.applications import Starlette
    from starlette.responses import Response, StreamingResponse
    from starlette.routing import Route, WebSocketRoute
    from starlette.websockets import WebSocket, WebSocketDisconnect
except ImportError:  # pragma: no cover
    Starlette = None  # type: ignore[assignment,misc]
    Response = StreamingResponse = None  # type: ignore[assignment,misc]
    Route = WebSocketRoute = None  # type: ignore[assignment,misc]
    WebSocket = WebSocketDisconnect = None  # type: ignore[assignment,misc]

try:  # pragma: no cover
    import uvicorn
except ImportError:  # pragma: no cover
    uvicorn = None  # type: ignore[assignment]


__all__ = [
    "CameraSimulator",
    "SimulatorServer",
    "create_app",
    "run_simulator",
    "MJPEG_CONTENT_TYPE",
]

MJPEG_BOUNDARY = "--boundarydonotcross"
MJPEG_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}"

_JSON_CT = "application/json"


def _require() -> None:
    """Raise a clear, actionable ImportError if the extra is absent."""
    missing = []
    if Starlette is None:
        missing.append("starlette")
    if uvicorn is None:
        missing.append("uvicorn")
    if missing:
        raise ImportError(
            "the pyks2 camera simulator requires: "
            f"{', '.join(missing)}. Install with: pip install pyks2[testing]"
        )


# --- shipped fixture access ------------------------------------------------
# Reads from package data (pyks2/testing/data), NEVER from the repo's
# examples/ or tests/ — those are not installed, so reading them would work
# from a git checkout and break for anyone who did `pip install pyks2[testing]`.

_cache: Dict[str, bytes] = {}


def fixture_bytes(name: str) -> bytes:
    """Return a shipped fixture's raw bytes, verbatim."""
    if name not in _cache:
        from importlib.resources import files

        _cache[name] = (files(__package__) / "data" / name).read_bytes()
    return _cache[name]


def fixture_text(name: str) -> str:
    return fixture_bytes(name).decode("utf-8")


def fixture_json(name: str) -> Any:
    return json.loads(fixture_text(name))


# Endpoints served verbatim from a captured response. Serving the original
# bytes (rather than a json.dumps round-trip) preserves the camera's own
# odd whitespace, so clients see the real formatting too.
_STATIC: Dict[str, str] = {
    "/v1/ping": "ping.json",
    "/v1/apis": "apis.json",
    "/v1/props": "props.json",
    "/v1/props/camera": "props-camera.json",
    "/v1/props/lens": "props-lens.json",
    "/v1/props/liveview": "props-liveview.json",
    "/v1/props/device": "props-device.json",
    "/v1/params/lens": "params-lens.json",
    "/v1/params/device": "params-device.json",
    "/v1/constants/camera": "constants-camera.json",
    "/v1/constants/device": "constants-device.json",
    "/v1/status/camera": "status-camera.json",
    "/v1/status/lens": "status-lens.json",
    "/v1/status/liveview": "status-liveview.json",
    "/v1/status/device": "status-device.json",
    "/v1/lens/focus": "lens-focus-response.json",
}

# Which capability list gates each exposure value (PROTOCOL.md §6.5).
_LIST_FOR = {"av": "avList", "tv": "tvList", "sv": "svList", "xv": "xvList"}

_OK = {"errCode": 200, "errMsg": "OK"}


def _err(code: int, msg: str) -> Dict[str, Any]:
    return {"errCode": code, "errMsg": msg}


def _next_filename(name: str) -> str:
    """'IMGP1974.DNG' -> 'IMGP1975.DNG', preserving width and extension."""
    m = re.match(r"^(?P<stem>\D*)(?P<num>\d+)(?P<ext>\.\w+)$", name)
    if not m:
        return name
    num = m.group("num")
    return f"{m.group('stem')}{str(int(num) + 1).zfill(len(num))}{m.group('ext')}"


class CameraSimulator:
    """The camera's observable state. Shared by the HTTP and WebSocket handlers.

    Only ``shoot()`` mutates anything meaningful. Construct one per test for
    isolation; the pytest fixture does that for you.
    """

    def __init__(self, capture_delay: float = 0.05,
                 frame_interval: float = 0.02) -> None:
        #: seconds between the shoot response and the file/event appearing
        #: (the real camera took ~3.4 s; kept small so tests stay fast)
        self.capture_delay = capture_delay
        #: seconds between MJPEG frames
        self.frame_interval = frame_interval

        self._params: Dict[str, Any] = fixture_json("params-camera.json")
        self._variables: Dict[str, Any] = fixture_json("variables-camera.json")
        self._dirs: List[Dict[str, Any]] = [
            {"name": d["name"], "files": list(d["files"])}
            for d in fixture_json("photos-listing.json").get("dirs", [])
        ]
        self._info_template: Dict[str, Any] = fixture_json(
            "photos-latest-info.json")

        # Real event payloads, keyed by their `changed` value, so broadcasts
        # replay captured bytes rather than re-serialised JSON.
        self._events: Dict[str, str] = {}
        for fname in ("changes-events.jsonl", "changes-capture-sequence.jsonl"):
            for line in fixture_text(fname).splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    kind = json.loads(line).get("changed")
                except ValueError:
                    continue
                if kind:
                    self._events[kind] = line

        self._mjpeg_part: bytes = fixture_bytes("liveview-frame-raw.bin")

        #: number of live view streams currently open (gates /v1/liveview/zoom)
        self.active_streams = 0
        #: every payload broadcast, in order — handy for assertions
        self.emitted: List[str] = []
        self._queues: List[Any] = []
        self._lock = threading.Lock()

    # -- events -----------------------------------------------------------

    def event_payload(self, kind: str) -> str:
        """The captured raw payload for a ``changed`` value."""
        try:
            return self._events[kind]
        except KeyError:
            raise KeyError(
                f"no captured /v1/changes payload for changed={kind!r}; "
                f"have {sorted(self._events)}"
            ) from None

    async def broadcast(self, kind: str) -> None:
        payload = self.event_payload(kind)
        self.emitted.append(payload)
        for q in list(self._queues):
            q.put_nowait(payload)

    # -- photos -----------------------------------------------------------

    def listing(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """``/v1/photos`` payload. Dirs oldest-first; ``limit`` is a head-limit
        across the flattened file order, matching the camera."""
        dirs: List[Dict[str, Any]] = []
        remaining = limit
        for d in self._dirs:
            if remaining is not None and remaining <= 0:
                break
            files = d["files"]
            if remaining is not None:
                files = files[:remaining]
                remaining -= len(files)
            dirs.append({"name": d["name"], "files": list(files)})
        return {**_OK, "dirs": dirs}

    @property
    def latest_path(self) -> Optional[str]:
        for d in reversed(self._dirs):
            if d["files"]:
                return f"{d['name']}/{d['files'][-1]}"
        return None

    def has_file(self, dirname: str, filename: str) -> bool:
        return any(d["name"] == dirname and filename in d["files"]
                   for d in self._dirs)

    def info_for(self, path: str) -> Dict[str, Any]:
        """Photo metadata. Real captured fields, with dir/file made consistent
        with this simulator's listing (the two captures came from different
        cards, so the shipped latest-info names a file the shipped listing
        doesn't contain)."""
        dirname, _, filename = path.partition("/")
        return {**self._info_template, "captured": True,
                "dir": dirname, "file": filename}

    def shoot(self) -> Dict[str, Any]:
        """Append the next file. Returns nothing about the new file — the real
        camera's shoot response reports ``captured: false`` and the client is
        expected to learn about the file via the event or by polling."""
        with self._lock:
            if not self._dirs:
                self._dirs.append({"name": "100_0101", "files": ["IMGP0001.DNG"]})
                return {"dir": "100_0101", "file": "IMGP0001.DNG"}
            target = self._dirs[-1]
            last = target["files"][-1] if target["files"] else "IMGP0000.DNG"
            new = _next_filename(last)
            target["files"].append(new)
            return {"dir": target["name"], "file": new}

    # -- params -----------------------------------------------------------

    def params_camera(self) -> Dict[str, Any]:
        return {**_OK, **self._params}

    def variables_camera(self) -> Dict[str, Any]:
        """Lists from the capture, current values from live state, so the
        writability signal and the values never disagree."""
        merged = dict(self._variables)
        merged.update(self._params)
        return {**_OK, **merged}

    def put_params(self, body: str) -> Tuple[Dict[str, Any], bool]:
        """Apply a ``PUT /v1/params/camera`` body.

        Returns ``(response, changed)``. Honours the list-emptiness signal: a
        write to a camera-controlled value returns 200 and is ignored, exactly
        as the hardware behaves.
        """
        changed = False
        for pair in (body or "").split("&"):
            if not pair:
                continue
            key, _, value = pair.partition("=")
            key, value = key.strip(), value.strip()
            if not key:
                continue

            list_key = _LIST_FOR.get(key)
            if list_key is not None:
                allowed = self._variables.get(list_key) or []
                if not allowed:
                    # Camera owns this value in the current mode: 200, ignored.
                    continue
                if value not in allowed and not (key == "sv"
                                                 and value.lower() == "auto"):
                    return _err(400, "Bad Request"), False
            if self._params.get(key) != value:
                self._params[key] = value
                changed = True
        return self.params_camera(), changed


def create_app(sim: Optional[CameraSimulator] = None) -> Any:
    """Build the ASGI app. ``sim`` defaults to a fresh :class:`CameraSimulator`;
    the app exposes it as ``app.state.simulator``."""
    _require()
    import asyncio

    sim = sim if sim is not None else CameraSimulator()

    def _json(payload: Any, name: Optional[str] = None) -> Any:
        if name is not None:  # verbatim captured bytes
            return Response(fixture_bytes(name), media_type=_JSON_CT)
        return Response(json.dumps(payload), media_type=_JSON_CT)

    async def static(request) -> Any:
        return _json(None, _STATIC[request.url.path])

    async def params_camera(request) -> Any:
        if request.method == "PUT":
            body = (await request.body()).decode("utf-8", "replace")
            payload, changed = sim.put_params(body)
            if changed:
                await sim.broadcast("camera")
            return _json(payload)
        return _json(sim.params_camera())

    async def variables_camera(request) -> Any:
        return _json(sim.variables_camera())

    async def photos(request) -> Any:
        raw = request.query_params.get("limit")
        limit: Optional[int] = None
        if raw is not None:
            try:
                limit = int(raw)
            except ValueError:
                return _json(_err(400, "Bad Request"))
        return _json(sim.listing(limit))

    async def latest_info(request) -> Any:
        path = sim.latest_path
        if path is None:
            return _json(_err(400, "Bad Request"))
        return _json(sim.info_for(path))

    async def photo_info(request) -> Any:
        d = request.path_params["dir"]
        f = request.path_params["file"]
        if not sim.has_file(d, f):
            return _json(_err(400, "Bad Request"))
        return _json(sim.info_for(f"{d}/{f}"))

    async def photo_file(request) -> Any:
        d = request.path_params["dir"]
        f = request.path_params["file"]
        size = request.query_params.get("size")
        if size == "thumb":  # genuinely unsupported on the K-S2
            return _json(None, "error-400-bad-request.json")
        if not sim.has_file(d, f):
            return _json(_err(400, "Bad Request"))
        if size == "view":
            return Response(_view_jpeg(sim), media_type="image/jpeg")
        return Response(_dng_stub(), media_type="application/octet-stream")

    async def shoot(request) -> Any:
        await request.body()
        new = sim.shoot()

        async def settle() -> None:
            # The real camera answers the shoot immediately and the file/event
            # land a few seconds later; preserve that ordering.
            await asyncio.sleep(sim.capture_delay)
            await sim.broadcast("storage")

        asyncio.get_event_loop().create_task(settle())
        _ = new
        return _json(None, "camera-shoot-response.json")

    async def liveview(request) -> Any:
        async def frames():
            sim.active_streams += 1
            try:
                while True:
                    yield sim._mjpeg_part
                    await asyncio.sleep(sim.frame_interval)
            finally:
                sim.active_streams -= 1

        return StreamingResponse(frames(), media_type=MJPEG_CONTENT_TYPE)

    async def liveview_zoom(request) -> Any:
        body = (await request.body()).decode("utf-8", "replace").strip()
        if body and sim.active_streams == 0:
            # Gated: parameters need an active stream (PROTOCOL.md §9).
            return _json(None, "error-412-precondition.json")
        return _json(dict(_OK))

    async def changes(ws) -> None:
        await ws.accept()
        queue: Any = asyncio.Queue()
        sim._queues.append(queue)

        async def pump() -> None:
            """Push broadcast events out to this client."""
            while True:
                await ws.send_text(await queue.get())

        async def until_disconnect() -> None:
            """Watch for the peer going away.

            Necessary, not decorative: the camera never expects input on this
            socket, so without an active receive() a disconnect is invisible and
            pump() blocks on the queue forever. That leaks a task per connection
            and stalls server shutdown until it force-times-out.
            """
            while True:
                message = await ws.receive()
                if message.get("type") == "websocket.disconnect":
                    return

        tasks = [asyncio.ensure_future(pump()),
                 asyncio.ensure_future(until_disconnect())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        except WebSocketDisconnect:  # pragma: no cover - defensive
            pass
        finally:
            for task in tasks:
                task.cancel()
            if queue in sim._queues:
                sim._queues.remove(queue)

    routes = [Route(p, static, methods=["GET"]) for p in _STATIC]
    routes += [
        Route("/v1/params/camera", params_camera, methods=["GET", "PUT"]),
        Route("/v1/variables/camera", variables_camera, methods=["GET"]),
        Route("/v1/camera/shoot", shoot, methods=["POST"]),
        Route("/v1/liveview", liveview, methods=["GET"]),
        Route("/v1/liveview/zoom", liveview_zoom, methods=["POST"]),
        Route("/v1/photos", photos, methods=["GET"]),
        # latest/info must precede the {dir}/{file} patterns.
        Route("/v1/photos/latest/info", latest_info, methods=["GET"]),
        Route("/v1/photos/{dir}/{file}/info", photo_info, methods=["GET"]),
        Route("/v1/photos/{dir}/{file}", photo_file, methods=["GET"]),
        WebSocketRoute("/v1/changes", changes),
    ]

    app = Starlette(routes=routes)
    app.state.simulator = sim
    return app


def _view_jpeg(sim: CameraSimulator) -> bytes:
    """``?size=view`` payload: the JPEG out of the captured MJPEG part. Real
    camera JPEG bytes, though a live view frame rather than a photo preview —
    no preview binary was captured. See data/PROVENANCE.md."""
    part = sim._mjpeg_part
    start = part.find(b"\xff\xd8")
    end = part.rfind(b"\xff\xd9")
    return part[start:end + 2] if start >= 0 and end > start else part


def _dng_stub() -> bytes:
    """``?size=full`` payload: synthetic. A real DNG is ~18 MB and none is
    committed (``*.dng`` is gitignored), so this is TIFF magic plus padding —
    enough for the client's magic sniff and its ``min_bytes`` guard. The only
    other fabricated bytes here. See data/PROVENANCE.md."""
    return b"II*\x00" + b"\x00" * 4 + b"D" * 20000


# --- running it ------------------------------------------------------------


class SimulatorServer:
    """The app on a real loopback socket, in a background thread.

    A real socket (not an in-process ASGI transport) is required: the async
    client talks WebSocket and streams MJPEG, and ``httpx``'s ASGITransport
    supports neither properly.

    Usage:
        >>> with SimulatorServer() as server:
        ...     cam = server.client()
        ...     cam.ping()
        True
    """

    def __init__(self, sim: Optional[CameraSimulator] = None,
                 host: str = "127.0.0.1", port: int = 0) -> None:
        _require()
        self.simulator = sim if sim is not None else CameraSimulator()
        self.host = host
        self._requested_port = port
        self._port: Optional[int] = None
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------

    def start(self, timeout: float = 15.0) -> "SimulatorServer":
        app = create_app(self.simulator)
        config = uvicorn.Config(app, host=self.host, port=self._requested_port,
                                log_level="warning", lifespan="off")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True,
                                        name="pyks2-simulator")
        self._thread.start()

        deadline = time.time() + timeout
        while time.time() < deadline:
            if getattr(self._server, "started", False):
                socks = [s for srv in (self._server.servers or [])
                         for s in srv.sockets]
                if socks:
                    self._port = socks[0].getsockname()[1]
                    return self
            if not self._thread.is_alive():
                raise RuntimeError("simulator server thread died during startup")
            time.sleep(0.01)
        self.stop()
        raise RuntimeError(f"simulator did not start within {timeout}s")

    def stop(self, timeout: float = 10.0) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout)
        self._thread = None
        self._server = None

    def __enter__(self) -> "SimulatorServer":
        return self.start()

    def __exit__(self, *exc: Any) -> None:
        self.stop()

    # -- addressing -------------------------------------------------------

    @property
    def port(self) -> int:
        if self._port is None:
            raise RuntimeError("simulator not started")
        return self._port

    @property
    def host_port(self) -> str:
        """``host:port`` — what ``K_S2_WiFi(...)`` wants as its ``ip``."""
        return f"{self.host}:{self.port}"

    @property
    def base_url(self) -> str:
        return f"http://{self.host_port}"

    def client(self, **kwargs: Any) -> Any:
        """A real ``K_S2_WiFi`` pointed at this simulator."""
        from ..client import K_S2_WiFi

        return K_S2_WiFi(self.host_port, **kwargs)


def run_simulator(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Run in the foreground until interrupted (the CLI entry point)."""
    _require()
    uvicorn.run(create_app(), host=host, port=port, log_level="info",
                lifespan="off")


def main(argv: Optional[List[str]] = None) -> int:
    import argparse

    p = argparse.ArgumentParser(
        prog="python -m pyks2.testing.simulator",
        description="Serve a protocol-level Pentax K-S2 simulator over HTTP.")
    p.add_argument("--host", default="127.0.0.1",
                   help="bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080,
                   help="bind port; 0 picks a free one (default: 8080)")
    args = p.parse_args(argv)

    _require()
    print(f"K-S2 simulator on http://{args.host}:{args.port}  "
          f"(point pyks2 at {args.host}:{args.port})")
    run_simulator(host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
