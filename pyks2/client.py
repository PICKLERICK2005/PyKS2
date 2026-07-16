"""The K-S2 WiFi HTTP client.

``K_S2_WiFi`` is a pure API client — no rig logic, no scanning, no file
ingest. It speaks the camera's HTTP API exactly as characterised in
PROTOCOL.md, including the two protocol laws:

    Law 1: errCode lives in the response body, not the HTTP status.
    Law 2: datetime/numeric formats vary (handled in models.py).

Every request goes over a fresh connection with ``Connection: close`` (Law 3),
which is the reliable pattern for this camera's server.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterator, List, Optional
from urllib.parse import quote

import requests

from . import constants as C
from .errors import (
    KS2APIError,
    KS2Error,
    KS2ConnectionError,
    KS2NotFoundError,
    KS2UnsupportedError,
)
from .models import (
    CameraConstants,
    CameraParams,
    DeviceInfo,
    LensState,
    PhotoInfo,
    PhotoListing,
    ShootResult,
)


class K_S2_WiFi:
    """Client for the Pentax K-S2 built-in WiFi HTTP API.

    Args:
        ip: Camera IP (default 192.168.0.1).
        timeout: Default request timeout in seconds.
        logger: Optional callable(str) for trace logging.

    Example:
        >>> cam = K_S2_WiFi()
        >>> cam.ping()
        True
        >>> info = cam.capture(af="off")   # baseline+shoot+wait, race-free
        >>> cam.download(info.path, "shot.dng")
    """

    def __init__(self, ip: str = C.DEFAULT_IP, timeout: float = C.DEFAULT_TIMEOUT,
                 logger: Optional[Callable[[str], None]] = None):
        self.ip = ip
        self.timeout = timeout
        self._log = logger or (lambda _m: None)

    # -- low-level ----------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"http://{self.ip}{path}"

    def _request(self, method: str, path: str, *, body: Optional[str] = None,
                 timeout: Optional[float] = None, stream: bool = False,
                 raw: bool = False) -> Any:
        """Make one request over a fresh connection.

        Returns the parsed JSON dict (default), or the raw ``requests.Response``
        if ``raw=True`` (used for binary downloads / streams).

        Raises:
            KS2ConnectionError: transport failure / timeout / dropped socket.
            KS2APIError: camera returned a non-200 errCode in the body.
        """
        url = self._url(path)
        headers = dict(C.DEFAULT_HEADERS)
        if body is None:
            headers.pop("Content-Type", None)
        to = timeout if timeout is not None else self.timeout
        self._log(f"{method} {path}" + (f"  <{body}>" if body else ""))
        try:
            with requests.Session() as s:
                s.headers.update(headers)
                resp = s.request(method, url, data=body, timeout=to,
                                 stream=stream)
        except requests.exceptions.RequestException as e:
            raise KS2ConnectionError(
                f"{method} {path} failed: {type(e).__name__}: {e}") from e

        if raw:
            return resp

        # Law 1: parse the body and check errCode, not the HTTP status.
        text = resp.text
        try:
            data = json.loads(text)
        except (ValueError, json.JSONDecodeError):
            # Non-JSON body (e.g. unhandled method returns raw HTML) — treat as
            # an unsupported/failed operation.
            raise KS2APIError(resp.status_code, "non-JSON response body", path)

        err = data.get("errCode", 200)
        if err != 200:
            raise KS2APIError(err, data.get("errMsg", ""), path)
        return data

    # -- connection / health -----------------------------------------------

    def ping(self) -> bool:
        """Return True if the camera answers /v1/ping.

        The cheapest, safest liveness check — no card-read side effects.
        """
        try:
            self._request("GET", C.EP.PING, timeout=min(self.timeout, 5.0))
            return True
        except KS2Error:
            return False
        except Exception:
            return False

    def apis(self) -> List[str]:
        """Return the camera's self-described endpoint list (/v1/apis)."""
        return self._request("GET", C.EP.APIS).get("apis", [])

    # -- reads: decomposed groups ------------------------------------------

    def _get_group(self, group: str, sub: Optional[str]) -> Dict[str, Any]:
        if sub is None:
            path = {"props": C.EP.PROPS}.get(group)
            if path is None:  # constants/params/variables/status have no bare form we use
                path = f"/v1/{group}"
        else:
            path = f"/v1/{group}/{sub}"
        return self._request("GET", path)

    def props(self, sub: Optional[str] = None) -> Dict[str, Any]:
        """GET /v1/props (or /v1/props/{sub}). Legacy flat superset."""
        return self._get_group("props", sub)

    def constants(self, sub: str = "camera") -> Dict[str, Any]:
        """GET /v1/constants/{sub}. Static capability lists."""
        return self._get_group("constants", sub)

    def params(self, sub: str = "camera") -> Dict[str, Any]:
        """GET /v1/params/{sub}. Current values."""
        return self._get_group("params", sub)

    def variables(self, sub: str = "camera") -> Dict[str, Any]:
        """GET /v1/variables/{sub}. Params + lists + live values."""
        return self._get_group("variables", sub)

    def status(self, sub: str = "camera") -> Dict[str, Any]:
        """GET /v1/status/{sub}. Transient runtime state."""
        return self._get_group("status", sub)

    # -- reads: typed convenience ------------------------------------------

    def get_camera_params(self) -> CameraParams:
        return CameraParams.from_dict(self.params("camera"))

    def get_camera_constants(self) -> CameraConstants:
        """Fetch all capability lists for the camera.

        The camera splits these across two endpoints: the *mode* lists
        (WBModeList, exposureModeList, shootModeList, effectList, filterList,
        stillSizeList) live in ``constants/camera`` and are static, while the
        *exposure-value* lists (avList, tvList, svList, xvList) live in
        ``variables/camera`` because they are DYNAMIC — avList in particular
        changes with the lens and current aperture. This merges both so callers
        get a complete picture, but you should re-fetch after settings changes
        rather than caching (see PROTOCOL.md §4).
        """
        merged: Dict[str, Any] = {}
        merged.update(self.constants("camera"))
        try:
            merged.update(self.variables("camera"))
        except KS2APIError:
            pass
        return CameraConstants.from_dict(merged)

    def get_lens_state(self) -> LensState:
        """Merge params/lens (focusMode) + status/lens (focused, focusCenters).

        The three fields the model exposes live in two endpoints: focusMode is
        in params/lens, while focused and focusCenters are in status/lens.
        """
        merged: Dict[str, Any] = {}
        try:
            merged.update(self.params("lens"))
        except KS2APIError:
            pass
        try:
            merged.update(self.status("lens"))
        except KS2APIError:
            pass
        return LensState.from_dict(merged)

    def get_device_info(self) -> DeviceInfo:
        """Merge constants/device + status/device + params/device.

        Identity (model/firmware/serial) is in constants/device, live state
        (battery/storages) in status/device, and the WiFi fields (ssid/channel/
        key) in params/device. All three are merged so DeviceInfo is complete.
        """
        merged: Dict[str, Any] = {}
        for getter in (lambda: self.constants("device"),
                       lambda: self.status("device"),
                       lambda: self.params("device")):
            try:
                merged.update(getter())
            except KS2APIError:
                pass
        return DeviceInfo.from_dict(merged)

    def get_state(self) -> Optional[str]:
        """Return camera 'state' ('idle'/'capturing'/...) from status/camera."""
        return self.status("camera").get("state")

    def get_focus_mode(self) -> Optional[str]:
        """Return 'af' or 'mf' (the physical lever position). Read-only."""
        return self.get_lens_state().focus_mode

    def is_idle(self) -> bool:
        return self.get_state() == "idle"

    # -- writes -------------------------------------------------------------

    def set_camera_params(self, **kwargs: Any) -> CameraParams:
        """PUT /v1/params/camera. Write settings; returns the echoed state.

        Keys use the camera's names (av, tv, sv, xv, WBMode, shootMode,
        exposureMode, stillSize, movieSize, effect, filter). Illegal values
        raise KS2APIError(400).

        Example:
            >>> cam.set_camera_params(av="8.0", sv="400")
        """
        if not kwargs:
            raise ValueError("no parameters given")
        body = "&".join(f"{k}={v}" for k, v in kwargs.items())
        data = self._request("PUT", C.EP.PARAMS_SUB.format(sub="camera"),
                             body=body)
        return CameraParams.from_dict(data)

    def set_lens_params(self, **kwargs: Any) -> None:
        """Attempt to write lens params. focusMode is read-only (physical
        lever) and will raise KS2UnsupportedError."""
        if "focusMode" in kwargs:
            raise KS2UnsupportedError(
                400, "focusMode is controlled by the physical AF/MF lever "
                     "and cannot be set over WiFi", C.EP.PARAMS_SUB.format(sub="lens"))
        body = "&".join(f"{k}={v}" for k, v in kwargs.items())
        self._request("PUT", C.EP.PARAMS_SUB.format(sub="lens"), body=body)

    # -- capture ------------------------------------------------------------

    def shoot(self, af: Optional[str] = None) -> ShootResult:
        """Fire the shutter (stills). Returns the immediate response.

        ``captured`` in the result is always False (capture is async) — call
        ``wait_for_capture()`` or watch events to get the written file.

        Args:
            af: 'auto' | 'on' | 'off'. If None, auto-selects based on the
                physical AF/MF lever: MF -> 'off' (fires without hunting),
                otherwise 'auto'. 'off' is the right choice for MF lenses and
                fixed-focus rigs (always releases the shutter).
        """
        if af is None:
            mode = self.get_focus_mode()
            af = "off" if (mode and mode.lower() == "mf") else "auto"
            self._log(f"auto af-mode: focusMode={mode} -> af={af}")
        if af not in C.AF_MODES:
            raise ValueError(f"af must be one of {C.AF_MODES}, got {af!r}")
        data = self._request("POST", C.EP.SHOOT, body=f"af={af}")
        return ShootResult.from_dict(data)

    def focus(self, x: int = 52, y: int = 52) -> LensState:
        """Drive autofocus / set the AF point via POST /v1/lens/focus.

        Works over WiFi even though focusMode (AF/MF) is read-only.
        """
        data = self._request("POST", C.EP.FOCUS, body=f"pos={x},{y}")
        return LensState.from_dict(data)

    def bulb_start(self, af: str = "off") -> None:
        """Open the shutter for a Bulb exposure (POST /v1/camera/shoot/start).

        Requires the physical mode dial to be set to **B (Bulb)** — otherwise
        the camera returns errCode 412. Pair with ``bulb_finish()``; the elapsed
        time between them is the exposure. In Bulb mode, plain ``shoot()`` does
        NOT work (412) — use this instead.
        """
        self._request("POST", C.EP.SHOOT_START, body=f"af={af}")

    def bulb_finish(self, af: str = "off") -> None:
        """Close the shutter, ending a Bulb exposure (POST /v1/camera/shoot/finish).

        Call after ``bulb_start()`` while the dial is in Bulb mode.
        """
        self._request("POST", C.EP.SHOOT_FINISH, body=f"af={af}")

    def bulb_exposure(self, seconds: float, af: str = "off") -> PhotoInfo:
        """Take a timed Bulb exposure of ``seconds`` and return the new file.

        Records a baseline, opens the shutter, waits, closes it, then waits for
        the written file. The dial must be in Bulb mode.
        """
        import time
        try:
            baseline = self.latest_info().path
        except KS2Error:
            baseline = None
        self.bulb_start(af=af)
        time.sleep(seconds)
        self.bulb_finish(af=af)
        return self.wait_for_capture(since=baseline)

    # -- photos: browse -----------------------------------------------------

    def list_photos(self, limit: Optional[int] = None) -> PhotoListing:
        """GET /v1/photos. Enumerate photos as {dirs:[{name,files}]}.

        This is reliable (the old "it hangs" claim is debunked — see
        PROTOCOL.md §8). Listing time scales ~linearly with file count.

        Args:
            limit: If given, uses the undocumented ?limit=N to cap results to
                   the first N files (constant ~60ms regardless of card size).
                   There is no offset/cursor, so this is a head-limit only.
        """
        path = C.EP.PHOTOS
        if limit is not None:
            path = f"{path}?limit={int(limit)}"
        return PhotoListing.from_dict(self._request("GET", path, timeout=30.0))

    def photo_info(self, path: str) -> PhotoInfo:
        """GET /v1/photos/{dir}/{file}/info. Works for any file, not just
        the latest. ``path`` is 'DIR/FILE'."""
        d, _, f = path.partition("/")
        if not (d and f):
            raise ValueError(f"path must be 'DIR/FILE', got {path!r}")
        ep = C.EP.PHOTO_INFO.format(dir=quote(d), file=quote(f))
        return PhotoInfo.from_dict(self._request("GET", ep))

    def latest_info(self) -> PhotoInfo:
        """GET /v1/photos/latest/info. Metadata for the most recent shot."""
        return PhotoInfo.from_dict(self._request("GET", C.EP.PHOTO_LATEST_INFO))

    def wait_for_capture(self, since: Optional[str] = None,
                         timeout: float = 30.0,
                         poll_interval: float = 0.5) -> PhotoInfo:
        """Poll latest/info until a NEW captured file appears.

        ``/v1/photos/latest/info`` always reports ``captured: true`` for the
        last existing image, so a naive "captured is true" check returns the
        *previous* photo instantly. This method compares against a baseline
        path and only returns once the latest path actually changes.

        Args:
            since: The path (``DIR/FILE``) that was latest BEFORE you triggered
                the shot. If None, this reads the current latest as the baseline
                (only correct if you call this before shooting; prefer passing
                the baseline you captured yourself, or use ``capture()``).
            timeout: Max seconds to wait.
            poll_interval: Seconds between polls.

        Returns:
            PhotoInfo for the newly written file.

        Raises:
            KS2ConnectionError: if no new file appears within ``timeout``.
        """
        import time
        if since is None:
            try:
                since = self.latest_info().path
            except KS2Error:
                since = None
        start = time.time()
        last: Optional[PhotoInfo] = None
        while time.time() - start < timeout:
            info = self.latest_info()
            last = info
            if info.captured and info.path and info.path != since:
                return info
            time.sleep(poll_interval)
        raise KS2ConnectionError(
            f"no new captured file within {timeout}s "
            f"(baseline={since}, last={last.path if last else 'none'})")

    def capture_with_events(self, af: Optional[str] = None,
                            timeout: float = 30.0) -> PhotoInfo:
        """Take one photo using the event stream for completion detection.

        Connects to /v1/changes BEFORE firing the shutter — otherwise a fast
        ``storage`` event could fire between the shoot request and the event
        connection and be missed. On the ``storage`` event, fetches the new
        file's info.

        Falls back to nothing special if the stream can't connect — prefer
        ``capture()`` (poll-based) if you don't need the event path.
        """
        with self.events() as ev:  # connect first
            self.shoot(af=af)
            import time
            deadline = time.time() + timeout
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                change = ev.next_event(timeout=remaining)
                if change is None:
                    break
                if change.is_storage:
                    return self.latest_info()
        raise KS2ConnectionError(
            f"no storage event within {timeout}s of capture")

    def capture(self, af: Optional[str] = None, timeout: float = 30.0,
                download_to: Optional[str] = None,
                size: Optional[str] = None) -> PhotoInfo:
        """Take one photo safely: baseline, shoot, wait for the NEW file,
        optionally download it. This is the recommended one-shot capture.

        Records the current latest path *before* firing, so completion
        detection can't be fooled by the pre-existing last image.

        Args:
            af: AF mode (see ``shoot``); None auto-selects from the AF/MF lever.
            timeout: Max seconds to wait for the file to land.
            download_to: If given, download the new file here after capture.
            size: Download size ('view'|'full'); only used with download_to.

        Returns:
            PhotoInfo for the captured file.
        """
        try:
            baseline = self.latest_info().path
        except KS2Error:
            baseline = None
        self.shoot(af=af)
        info = self.wait_for_capture(since=baseline, timeout=timeout)
        if download_to and info.path:
            self.download(info.path, download_to, size=size)
        return info

    # -- photos: download ---------------------------------------------------

    def download(self, path: str, out_path: str, size: Optional[str] = None,
                 chunk: int = 8192, min_bytes: int = 1000) -> int:
        """Download a photo atomically. Returns bytes written.

        Writes to ``out_path + '.part'`` and renames on success, so a failure
        never leaves a corrupt file at the destination. Detects the camera's
        "200 + JSON error body instead of an image" case, wraps mid-stream
        network errors as KS2ConnectionError, and removes the partial file on
        any failure.

        Args:
            path: 'DIR/FILE'.
            size: None/'full' -> raw DNG (~18MB, ~55s). 'view' -> ~54KB JPEG.
                  'thumb' is UNSUPPORTED (400) and raises KS2UnsupportedError.
            min_bytes: Downloads smaller than this are treated as a failed
                       fetch (the camera returned an error/empty body).
        """
        import os
        if size == "thumb":
            raise KS2UnsupportedError(
                400, "size=thumb is not supported on the K-S2; use size=view",
                C.EP.PHOTO_FILE)
        d, _, f = path.partition("/")
        if not (d and f):
            raise ValueError(f"path must be 'DIR/FILE', got {path!r}")
        ep = C.EP.PHOTO_FILE.format(dir=quote(d), file=quote(f))
        if size:
            ep = f"{ep}?size={size}"

        resp = self._request("GET", ep, timeout=C.DOWNLOAD_TIMEOUT,
                             stream=True, raw=True)

        # Detect a JSON error body masquerading as a download. Buffer enough to
        # hold a complete small error body (they are well under 256 bytes), and
        # sniff a valid image magic on the leading bytes.
        head = b""
        try:
            it = resp.iter_content(chunk)
            for c in it:
                if not c:
                    continue
                head += c
                if len(head) >= 256:
                    break
        except requests.exceptions.RequestException as e:
            resp.close()
            raise KS2ConnectionError(
                f"download stream failed reading header: {e}") from e

        ctype = (resp.headers.get("Content-Type") or "").lower()
        looks_json = ("json" in ctype) or head.lstrip()[:1] == b"{"
        is_image = head[:2] == b"\xff\xd8" or head[:2] in (b"II", b"MM")
        if looks_json and not is_image:
            resp.close()
            try:
                err = json.loads(head.decode("utf-8", "replace"))
                raise KS2APIError(err.get("errCode", 400),
                                  err.get("errMsg", ""), ep)
            except (ValueError, json.JSONDecodeError):
                raise KS2APIError(400, "download returned a non-image body", ep)

        # Stream the rest to a .part file, then atomically rename.
        part = out_path + ".part"
        out_dir = os.path.dirname(os.path.abspath(out_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        n = 0
        try:
            with open(part, "wb") as fh:
                if head:
                    fh.write(head)
                    n += len(head)
                for c in it:
                    if c:
                        fh.write(c)
                        n += len(c)
        except requests.exceptions.RequestException as e:
            resp.close()
            _quiet_remove(part)
            raise KS2ConnectionError(f"download stream failed: {e}") from e
        except OSError as e:
            resp.close()
            _quiet_remove(part)
            raise KS2Error(f"could not write {part}: {e}") from e
        finally:
            resp.close()

        if n < min_bytes:
            _quiet_remove(part)
            raise KS2NotFoundError(
                f"download too small ({n}B < {min_bytes}); likely missing "
                f"or an error body: {path}")

        os.replace(part, out_path)  # atomic on the same filesystem
        return n

    def preview_bytes(self, path: str) -> bytes:
        """Return the ~54KB JPEG preview (size=view) as bytes, in memory."""
        d, _, f = path.partition("/")
        ep = C.EP.PHOTO_FILE.format(dir=quote(d), file=quote(f)) + "?size=view"
        resp = self._request("GET", ep, timeout=30.0, stream=True, raw=True)
        data = resp.content
        resp.close()
        if data[:2] != b"\xff\xd8":
            raise KS2APIError(400, "preview did not return a JPEG", ep)
        return data

    # -- live view ----------------------------------------------------------

    def liveview_zoom(self, **params: Any) -> dict:
        """POST /v1/liveview/zoom — digital zoom/pan endpoint.

        NOTE: no observable effect over WiFi on the test rig. With an active
        liveview stream it returns errCode 200 for any parameter (zoom/level/
        scale/pos/etc., including nonsense), but the live frame never changes.
        Without an active stream it returns 412. Cause unconfirmed — may need a
        hardware capability the test lenses lack, or a camera state not reachable
        via the API. Kept for completeness; returns whatever the camera sends.
        """
        body = "&".join(f"{k}={v}" for k, v in params.items()) if params else None
        return self._request("POST", C.EP.LIVEVIEW_ZOOM, body=body)

    def liveview_stream(self) -> "requests.Response":
        """Return a streaming Response for the MJPEG live view.

        Content-Type: multipart/x-mixed-replace; boundary=--boundarydonotcross.
        Caller is responsible for iterating/closing. See events.iter_mjpeg_frames.
        """
        return self._request("GET", C.EP.LIVEVIEW, timeout=C.DOWNLOAD_TIMEOUT,
                             stream=True, raw=True)

    def iter_liveview_frames(self, max_frames: Optional[int] = None
                             ) -> Iterator[bytes]:
        """Yield individual JPEG frames from the live view stream.

        Parses the multipart/x-mixed-replace boundary and yields each complete
        JPEG (SOI..EOI). Runs until the stream ends or ``max_frames`` reached.
        """
        resp = self.liveview_stream()
        buf = b""
        count = 0
        try:
            for chunk in resp.iter_content(8192):
                if not chunk:
                    continue
                buf += chunk
                while True:
                    soi = buf.find(b"\xff\xd8")
                    eoi = buf.find(b"\xff\xd9", soi + 2) if soi >= 0 else -1
                    if soi >= 0 and eoi > soi:
                        yield buf[soi:eoi + 2]
                        buf = buf[eoi + 2:]
                        count += 1
                        if max_frames and count >= max_frames:
                            return
                    else:
                        break
        finally:
            resp.close()

    # -- events -------------------------------------------------------------

    def events(self, **kwargs):
        """Return a ChangesClient for the /v1/changes WebSocket event stream.

        Usage:
            >>> for ev in cam.events():
            ...     if ev.is_storage: ...   # a shot completed
        """
        from .events import ChangesClient
        return ChangesClient(self.ip, **kwargs)


def _quiet_remove(path: str) -> None:
    """Remove a file if present, ignoring any error (partial-download cleanup)."""
    import os
    try:
        os.remove(path)
    except OSError:
        pass
