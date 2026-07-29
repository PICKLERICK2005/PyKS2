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

The behaviours it reproduces were each checked against the physical camera on
2026-07-29 by probing both and diffing (see ``docs/VERIFICATION.md``):

* **Law 1** — ``errCode`` lives in the JSON body; the HTTP status stays 200.
  A bogus path is ``200`` + ``errCode 400``; a missing photo is ``200`` +
  ``errCode 404``. Only an unhandled *method* breaks the pattern, returning a
  real ``400`` with an HTML body.
* ``/v1/photos`` lists directories **oldest-first**, and ``?limit=N`` is a
  **head**-limit (no offset/cursor), so callers wanting the newest file must
  slice from the tail. Every directory stays in the response even when the limit
  leaves it empty, and ``limit=0`` means *no limit*.
* ``/v1/photos/latest/info`` reports ``captured: false`` with **no dir/file**
  until something is captured in the current power session — files already on
  the card do not count.
* An **empty** ``avList``/``tvList``/``svList``/``xvList`` means the camera owns
  that value in the current exposure mode: a PUT still returns 200 and is
  **silently ignored** (PROTOCOL.md §6.5). A ``PUT`` echoes back a
  ``variables``-shaped body — the lists and ``state``, not just the params.
* A capture emits **exactly one** ``storage`` event on ``/v1/changes``; a
  settings write emits ``camera``. Payloads carry the camera's trailing newline.
* ``POST /v1/liveview/zoom`` returns **412** unless live view is actively
  streaming and **200** while it is, *regardless of body* — including an empty
  one. (PROTOCOL.md §9 previously claimed an empty body returned 200; measuring
  it disproved that.)
* Response latency is modelled from measured medians, see :class:`Timing`.

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
from dataclasses import dataclass
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
    "Timing",
    "FAST",
    "create_app",
    "run_simulator",
    "camera_json",
    "MJPEG_CONTENT_TYPE",
    "CAMERA_HEADERS",
]

MJPEG_BOUNDARY = "--boundarydonotcross"
MJPEG_CONTENT_TYPE = f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY}"

_JSON_CT = "application/json"

#: Headers the real camera sends, captured verbatim.
#:
#: Not reproduced, deliberately: exact header order and casing, the camera's
#: unusual ``Content-Length:3262`` with no space after the colon, and its
#: ``Connection: close``. uvicorn owns the response line, the framing headers
#: and the connection lifecycle — ``Connection`` is hop-by-hop, so overriding it
#: fights the server rather than emulating the camera. None of it is
#: functionally observable: header names are case-insensitive and the space
#: after a colon is optional, and pyks2 asks for ``Connection: close`` anyway.
CAMERA_HEADERS = {
    "Server": "server",
    "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
    "Max-Age": "0",
    "Accept-Ranges": "bytes",
}


@dataclass(frozen=True)
class Timing:
    """Response latency, modelled on medians measured from a real K-S2.

    Measured 2026-07-29 over the camera's own 2.4 GHz AP (firmware 01.10); see
    ``docs/VERIFICATION.md`` for the raw numbers. Every delay is multiplied by
    ``scale``, so ``Timing(scale=0.0)`` removes latency entirely — that is what
    :data:`FAST` is, and what the shipped pytest fixture uses so suites stay
    quick. The default is realistic, because a mock that answers instantly hides
    exactly the timeout and ordering bugs this is meant to catch.

    ``/v1/photos`` is the interesting one: it scales with the number of files
    *returned*, ~110 ms + ~3.9 ms/file (1.5 s for a 358-file card), which is why
    ``?limit`` exists at all.
    """

    scale: float = 1.0

    # simple JSON reads (median ms)
    ping_ms: float = 60.0
    props_ms: float = 103.0
    group_ms: float = 70.0          # props/params/variables/status per-facet
    photo_info_ms: float = 80.0

    # photos listing: base + per file returned
    photos_base_ms: float = 110.0
    photos_per_file_ms: float = 3.9

    # writes / actions
    put_params_ms: float = 160.0
    shoot_ms: float = 190.0
    zoom_ms: float = 70.0

    # downloads
    download_view_ms: float = 267.0     # ~53 KB preview
    download_full_ms: float = 55000.0   # ~18 MB DNG; unrealistic to wait for

    #: shoot response -> `storage` event on /v1/changes (1.9-3.4 s observed)
    capture_event_delay_s: float = 1.95
    #: floor for the above, applied even at ``scale=0`` — see capture_delay_s()
    min_capture_delay_s: float = 0.25
    #: stream opened -> first frame; the mirror has to flip up
    liveview_first_frame_s: float = 0.83
    #: between frames (~7.6 fps, jitter 44-200 ms)
    liveview_frame_interval_s: float = 0.103

    def ms(self, value: float) -> float:
        """Scale a millisecond figure to seconds."""
        return (value / 1000.0) * self.scale

    def s(self, value: float) -> float:
        return value * self.scale

    def photos_ms(self, files_returned: int) -> float:
        return self.photos_base_ms + self.photos_per_file_ms * files_returned

    def capture_delay_s(self) -> float:
        """Gap between the shoot response and the file existing. Never zero.

        This one is semantic rather than cosmetic, so ``scale=0`` floors it
        instead of removing it. Clients read "latest" immediately after shooting
        to establish a baseline — ``K_S2_WiFi.capture()`` does exactly that — and
        if the new file is already visible they adopt it as the baseline and then
        wait forever for something newer. The real camera takes ~2 s; the floor
        just has to clear a loopback round trip, which is ~15-30 ms.
        """
        return max(self.s(self.capture_event_delay_s), self.min_capture_delay_s)


#: No latency at all — for fast test suites.
FAST = Timing(scale=0.0)

#: uvicorn would otherwise add ``server: uvicorn`` in front of the camera's own
#: ``Server: server``, and a ``Date`` header the camera never sends.
_UVICORN_HEADER_OPTS = {"server_header": False, "date_header": False}


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


def _encode(value: Any, depth: int = 0) -> str:
    """Serialise one value the way the camera's firmware does."""
    if isinstance(value, dict):
        parts = [f'"{k}": {_encode(v, depth + 1)}' for k, v in value.items()]
        inner = ",\n ".join(parts)
        if depth:
            return "{\n " + inner + "\n }"
        # It breaks before the final brace only when the last value was itself
        # multiline.
        return "{" + inner + ("\n}" if parts and "\n" in parts[-1] else "}")
    if isinstance(value, list):
        if not value:
            return "[]"
        if any(isinstance(x, (dict, list)) for x in value):
            return ("[\n " + ",\n ".join(_encode(x, depth + 1) for x in value)
                    + "\n ]")
        return "[ " + ", ".join(json.dumps(x) for x in value) + "]"
    return json.dumps(value)


def camera_json(obj: Any) -> bytes:
    """Encode a dict as the camera would, down to the whitespace.

    The firmware's JSON has a house style — ``,\\n `` between members, ``[ `` at
    the start of a non-empty array, a trailing newline on the document — and
    responses this simulator computes (the photo listing, params, latest-info)
    have to be built rather than replayed. Building them with ``json.dumps``
    would leave every dynamic response visibly unlike every static one.

    Verified by round-tripping the captured ``photos-listing.json``,
    ``photo-info.json`` and ``photos-latest-info.json``: re-encoding the parsed
    form reproduces the original bytes exactly (see the tests).

    Not universal — ``props.json`` in particular is formatted inconsistently by
    the firmware (``"storages" : [``, with a space before the colon) and is
    served verbatim instead.
    """
    return (_encode(obj) + "\n").encode()


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

    def __init__(self, timing: Optional[Timing] = None) -> None:
        #: latency model; pass ``FAST`` (or ``Timing(scale=0)``) to remove it
        self.timing = timing if timing is not None else Timing()

        self._params: Dict[str, Any] = fixture_json("params-camera.json")
        self._variables: Dict[str, Any] = fixture_json("variables-camera.json")
        self._dirs: List[Dict[str, Any]] = [
            {"name": d["name"], "files": list(d["files"])}
            for d in fixture_json("photos-listing.json").get("dirs", [])
        ]
        self._info_template: Dict[str, Any] = fixture_json("photo-info.json")

        #: ``DIR/FILE`` captured in this "power session", or None. The camera
        #: reports no latest until it takes a picture, however full the card is.
        self.latest_captured: Optional[str] = None

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
                    # The camera terminates each payload with a newline: the
                    # storage frame measures 53 bytes, one more than the JSON.
                    self._events[kind] = line + "\n"

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

    def listing(self, limit: Optional[int] = None) -> Tuple[Dict[str, Any], int]:
        """``/v1/photos`` payload plus the number of files returned (which drives
        the latency model).

        Dirs come oldest-first and ``limit`` is a head-limit across the flattened
        file order. Two details measured off the camera: **every** directory
        stays in the response even when the limit leaves it with no files, and
        ``limit=0`` means *no limit* rather than "nothing".

        (One firmware quirk is deliberately not reproduced: a limit of exactly
        one less than the total returns everything — 357 of 358 gave 358 — which
        looks like an off-by-one in the camera and is not worth imitating.)
        """
        remaining = None if not limit else limit
        dirs: List[Dict[str, Any]] = []
        count = 0
        for d in self._dirs:
            files = d["files"]
            if remaining is not None:
                files = files[:max(remaining, 0)]
                remaining -= len(files)
            count += len(files)
            dirs.append({"name": d["name"], "files": list(files)})
        return {**_OK, "dirs": dirs}, count

    @property
    def newest_on_card(self) -> Optional[str]:
        """Newest file present, regardless of whether we captured it."""
        for d in reversed(self._dirs):
            if d["files"]:
                return f"{d['name']}/{d['files'][-1]}"
        return None

    def has_file(self, dirname: str, filename: str) -> bool:
        return any(d["name"] == dirname and filename in d["files"]
                   for d in self._dirs)

    def info_for(self, path: str) -> Dict[str, Any]:
        """Photo metadata: real captured fields with dir/file substituted."""
        dirname, _, filename = path.partition("/")
        return {**self._info_template, "captured": True,
                "dir": dirname, "file": filename}

    def latest_info(self) -> Dict[str, Any]:
        """``/v1/photos/latest/info``.

        Until this simulator captures something, the camera's answer is
        ``captured: false`` with no ``dir``/``file`` — even with a full card.
        Generated rather than replayed, because re-capturing that state needs a
        power cycle; the shape is exactly what the camera returned.
        """
        if self.latest_captured is None:
            return {**_OK, "captured": False}
        return self.info_for(self.latest_captured)

    def commit_capture(self) -> str:
        """Make the new file exist and become the session's latest.

        Called on a delay after the shoot response, never during it. That
        ordering matters and is not cosmetic: on the real camera the file does
        not exist when the shoot returns, it lands seconds later alongside the
        ``storage`` event. A simulator that created it immediately would let
        ``capture()`` mistake the brand-new file for its own baseline and then
        wait forever for a newer one.
        """
        with self._lock:
            if not self._dirs:
                self._dirs.append({"name": "100_0101", "files": []})
            target = self._dirs[-1]
            last = target["files"][-1] if target["files"] else "IMGP0000.DNG"
            new = _next_filename(last)
            target["files"].append(new)
            self.latest_captured = f"{target['name']}/{new}"
            return self.latest_captured

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

        The success body is ``variables``-shaped, not ``params``-shaped: the
        camera echoes the capability lists, ``state`` and ``exposureModeOption``
        alongside the values.
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
        return self.variables_camera(), changed


def create_app(sim: Optional[CameraSimulator] = None) -> Any:
    """Build the ASGI app. ``sim`` defaults to a fresh :class:`CameraSimulator`;
    the app exposes it as ``app.state.simulator``."""
    _require()
    import asyncio

    sim = sim if sim is not None else CameraSimulator()

    async def _delay(ms: float) -> None:
        seconds = sim.timing.ms(ms)
        if seconds > 0:
            await asyncio.sleep(seconds)

    def _json(payload: Any, name: Optional[str] = None) -> Any:
        body = fixture_bytes(name) if name is not None else camera_json(payload)
        return Response(body, media_type=_JSON_CT, headers=dict(CAMERA_HEADERS))

    def _binary(body: bytes, ctype: str) -> Any:
        return Response(body, media_type=ctype, headers=dict(CAMERA_HEADERS))

    async def static(request) -> Any:
        path = request.url.path
        await _delay(sim.timing.props_ms if path == "/v1/props"
                     else sim.timing.ping_ms if path == "/v1/ping"
                     else sim.timing.group_ms)
        return _json(None, _STATIC[path])

    async def params_camera(request) -> Any:
        if request.method == "PUT":
            body = (await request.body()).decode("utf-8", "replace")
            payload, changed = sim.put_params(body)
            await _delay(sim.timing.put_params_ms)
            if changed:
                await sim.broadcast("camera")
            return _json(payload)
        await _delay(sim.timing.group_ms)
        return _json(sim.params_camera())

    async def variables_camera(request) -> Any:
        await _delay(sim.timing.group_ms)
        return _json(sim.variables_camera())

    async def photos(request) -> Any:
        raw = request.query_params.get("limit")
        limit: Optional[int] = None
        if raw is not None:
            try:
                limit = int(raw)
            except ValueError:
                return _json(_err(400, "Bad Request"))
        payload, returned = sim.listing(limit)
        # Listing cost tracks the number of files actually returned, which is
        # the whole reason ?limit exists.
        await _delay(sim.timing.photos_ms(returned))
        return _json(payload)

    async def latest_info(request) -> Any:
        await _delay(sim.timing.photo_info_ms)
        return _json(sim.latest_info())

    async def photo_info(request) -> Any:
        d = request.path_params["dir"]
        f = request.path_params["file"]
        await _delay(sim.timing.photo_info_ms)
        if not sim.has_file(d, f):
            return _json(None, "error-404-not-found.json")
        return _json(sim.info_for(f"{d}/{f}"))

    async def photo_file(request) -> Any:
        d = request.path_params["dir"]
        f = request.path_params["file"]
        size = request.query_params.get("size")
        if size == "thumb":  # genuinely unsupported on the K-S2
            return _json(None, "error-400-bad-request.json")
        if not sim.has_file(d, f):
            return _json(None, "error-404-not-found.json")
        if size == "view":
            await _delay(sim.timing.download_view_ms)
            return _binary(fixture_bytes("photo-preview-view.jpg"), "image/jpeg")
        await _delay(sim.timing.download_full_ms)
        return _binary(_dng_stub(), "application/octet-stream")

    async def shoot(request) -> Any:
        await request.body()

        async def settle() -> None:
            # The camera answers the shoot at once; the file appears and the
            # storage event fires seconds later (1.9-3.4 s observed).
            await asyncio.sleep(sim.timing.capture_delay_s())
            sim.commit_capture()
            await sim.broadcast("storage")

        asyncio.get_event_loop().create_task(settle())
        await _delay(sim.timing.shoot_ms)
        return _json(None, "camera-shoot-response.json")

    async def liveview(request) -> Any:
        async def frames():
            sim.active_streams += 1
            try:
                # The mirror has to flip up before any frame appears.
                first = sim.timing.s(sim.timing.liveview_first_frame_s)
                if first > 0:
                    await asyncio.sleep(first)
                # Floor the gap even when latency is off. At a true zero this
                # becomes a busy loop that never yields to the event loop,
                # flooding the socket with frames no one asked for.
                interval = max(
                    sim.timing.s(sim.timing.liveview_frame_interval_s), 0.001)
                while True:
                    # The camera streams until the client stops reading, so this
                    # loop is unbounded; without an explicit disconnect check it
                    # would outlive the client and hold up shutdown.
                    if await request.is_disconnected():
                        return
                    yield sim._mjpeg_part
                    await asyncio.sleep(interval)
            finally:
                sim.active_streams -= 1

        return StreamingResponse(
            frames(), media_type=MJPEG_CONTENT_TYPE,
            # The camera sends no Content-Length or Connection on this one.
            headers=dict(CAMERA_HEADERS))

    async def liveview_zoom(request) -> Any:
        await request.body()
        await _delay(sim.timing.zoom_ms)
        # Gated purely on whether live view is streaming — the body is
        # irrelevant, including when empty (measured; PROTOCOL.md §9 had this
        # wrong).
        if sim.active_streams == 0:
            return _json(None, "error-412-precondition.json")
        return _json(dict(_OK))

    async def catch_all(request) -> Any:
        """Unknown paths are 200 + errCode 400, per Law 1."""
        await _delay(sim.timing.group_ms)
        return _json(None, "error-400-bad-request.json")

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

    async def unhandled_method(request, exc) -> Any:
        """The one place the camera abandons Law 1: an unhandled *method* gets a
        genuine HTTP 400 with an HTML body, not a JSON errCode."""
        return Response(fixture_bytes("unhandled-method.html"), status_code=400,
                        media_type="text/html",
                        headers={"Server": "server", "Connection": "close"})

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
        # Anything else the camera answers 200 + errCode 400 for. Restricted to
        # the methods it implements, so an unhandled method still falls through
        # to the 405 handler and gets the HTML treatment.
        Route("/{path:path}", catch_all, methods=["GET", "POST", "PUT"]),
    ]

    app = Starlette(routes=routes,
                    exception_handlers={405: unhandled_method})
    app.state.simulator = sim
    return app


def _dng_stub() -> bytes:
    """``?size=full`` payload: synthetic. A real DNG is ~18 MB and none is
    committed (``*.dng`` is gitignored), so this is TIFF magic plus padding —
    enough for the client's magic sniff and its ``min_bytes`` guard. The only
    fabricated bytes in the simulator. See data/PROVENANCE.md."""
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

    Latency is realistic by default; pass ``timing=FAST`` for none:

        >>> from pyks2.testing.simulator import FAST
        >>> server = SimulatorServer(timing=FAST)
    """

    def __init__(self, sim: Optional[CameraSimulator] = None,
                 host: str = "127.0.0.1", port: int = 0,
                 timing: Optional[Timing] = None) -> None:
        _require()
        if sim is None:
            sim = CameraSimulator(timing=timing)
        elif timing is not None:
            raise ValueError("pass timing to CameraSimulator, or omit sim")
        self.simulator = sim
        self.host = host
        self._requested_port = port
        self._port: Optional[int] = None
        self._server: Any = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle --------------------------------------------------------

    def start(self, timeout: float = 15.0) -> "SimulatorServer":
        app = create_app(self.simulator)
        config = uvicorn.Config(app, host=self.host, port=self._requested_port,
                                log_level="warning", lifespan="off",
                                **_UVICORN_HEADER_OPTS)
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

    def stop(self, timeout: float = 10.0, grace: float = 1.5) -> None:
        """Shut down, escalating to a forced exit if a stream is still open.

        uvicorn's graceful shutdown waits for live connections, and a live view
        stream or event socket is by nature open-ended. Waiting the full timeout
        for each one would add seconds per test, so give graceful a short grace
        period and then stop being polite.
        """
        if self._server is not None:
            self._server.should_exit = True
            if self._thread is not None:
                self._thread.join(timeout=grace)
                if self._thread.is_alive():
                    self._server.force_exit = True
                    self._thread.join(timeout=timeout)
        elif self._thread is not None:
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
                lifespan="off", **_UVICORN_HEADER_OPTS)


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
