# Changelog

All notable changes to **pyks2** are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [1.2.0b1] — 2026-07-29

A prerelease so the new simulator can soak before its API is frozen. Beta
because that public surface may still change, **not** because anything is
unverified — the simulator replays hardware-captured wire data throughout.

### Added
- **A shipped camera simulator (`pyks2[testing]`)**: `pyks2.testing` serves a
  protocol-level fake K-S2 over a real socket, so the *actual* pyks2 client —
  sync and async — can be driven end to end with no camera on the bench. This
  is deliberately **public, supported surface**, not internal test scaffolding:
  downstream libraries import it to run their own integration tests against a
  faithful camera instead of mocking pyks2 out.

  ```python
  from pyks2.testing import SimulatorServer

  with SimulatorServer() as server:
      cam = server.client()          # a real K_S2_WiFi
      info = cam.capture(af="off")
  ```

  Also available as `python -m pyks2.testing.simulator --port 8080`, and as a
  `ks2_simulator` pytest fixture on an ephemeral port, registered through a
  `pytest11` entry point so downstream suites get it just by installing the
  extra. Covers `/v1/props`, `/v1/params/camera` (GET + PUT), `/v1/photos` and
  photo download, `POST /v1/camera/shoot`, the `/v1/changes` WebSocket and the
  `/v1/liveview` MJPEG stream, plus the ping/apis/constants/variables/status
  reads.

  Every response body is **replayed from bytes captured off a physical K-S2**
  (firmware 01.10) and shipped inside the package as `pyks2/testing/data/`, so
  it works from a plain `pip install` rather than only in a git checkout. It
  reproduces the verified protocol behaviours rather than an idealised API:
  `errCode` in the body with HTTP 200 (Law 1), `/v1/photos` oldest-first with
  `?limit` as a head-limit only, the empty-list writability signal (a write to
  a camera-controlled value returns 200 and is silently ignored), exactly one
  `storage` event per capture and a `camera` event per settings write, and the
  412/200 gating on `/v1/liveview/zoom`. State is intentionally shallow: only a
  capture mutates anything, making a new file appear and firing the matching
  event so a shoot → new-file → download sequence works. The two payloads that
  is not captured bytes (`?size=full`) is documented in
  `pyks2/testing/data/PROVENANCE.md`.
- **The simulator was measured against the physical camera**, not just written to
  match the notes: the same raw-socket probe was run against both and diffed,
  and **40 of 40 checks match** — error codes, gating, listing and PUT shapes,
  MJPEG framing, WebSocket frame bytes, and latency. See
  [`docs/VERIFICATION.md`](docs/VERIFICATION.md). Three deviations are
  deliberate and documented (header casing/order, chunked transfer-encoding on
  the MJPEG stream, the synthetic `?size=full` payload); none is observable
  through an HTTP client.
- **Modelled response latency** (`pyks2.testing.Timing`), from measured medians:
  ~103 ms for `/v1/props`, ~1.5 s for a 358-file `/v1/photos` (it scales at
  ~110 ms + 3.9 ms/file, which is why `?limit` exists), ~1.9 s from shutter to
  `storage` event, ~830 ms for the first live view frame while the mirror flips
  up, ~7.6 fps thereafter. Realistic is the default, because a mock that answers
  instantly hides the timeout and ordering bugs a fake camera exists to catch;
  pass `timing=FAST` for none. The shipped `ks2_simulator` fixture is fast, and
  `ks2_simulator_realistic` is there when the timing is the thing under test.
- Generated responses are encoded in the firmware's own JSON house style
  (`camera_json()`), verified by round-tripping captured bodies byte-for-byte, so
  computed responses look like replayed ones on the wire.

### Fixed
- **`/v1/liveview/zoom` gating was documented wrong.** PROTOCOL.md §9 claimed an
  empty body returned `200`. Measuring it: with no stream running, all of no
  body, an empty body and `zoom=1` returned `412`; with a stream running, all
  three returned `200`. The gate is purely whether live view is streaming. §9
  corrected and the simulator matches.
- **`latest_info()` does not always report a file.** After a power cycle, with
  358 files on the card, `/v1/photos/latest/info` returned `captured: false` with
  no `dir`/`file` — "latest" means latest *this power session*. The
  `latest_info()` and `wait_for_capture()` docstrings said otherwise and are
  corrected; the simulator now starts in that state. Worth knowing when reading
  `capture()`: with an empty baseline it lets `wait_for_capture()` re-read the
  baseline *after* firing, which is safe only because the file takes ~2 s to
  appear.

### Also pinned down
- `PUT /v1/params/camera` echoes a `variables`-shaped body (the capability lists,
  `state` and `exposureModeOption`), not just the params a `GET` returns.
- `?limit=N` keeps every directory in the response, giving those past the limit
  an empty `files` list, and `limit=0` means *no limit*.
- A missing photo is `errCode 404` and an unknown path `errCode 400`, both under
  HTTP 200; an unhandled **method** is the one break from Law 1, a real HTTP 400
  with an HTML body.
- `/v1/changes` payloads end with a newline — the storage frame is 53 bytes.
- Listing order is ascending shot number, which is not the same as sorted
  filenames: a RAW+JPEG pair shares a number and the `.JPG` comes first.
- **Tests driving the real client against the simulator** over loopback, for
  both transports — `events_async()` and `iter_liveview_frames_async()`
  included — covering the requests/httpx/websockets transports and the MJPEG
  and event parsers that the existing fake-transport tests cannot reach.

### Fixed
- **`K_S2_WiFi` now accepts an address with a port** (`"127.0.0.1:8080"`).
  `ip` was interpreted inconsistently: HTTP interpolated it straight into the
  URL, so it could carry a port, but the `/v1/changes` clients needed host and
  port separately — the async one built `ws://host:8080:80/v1/changes` and the
  sync one passed the whole string to a socket connect. There was therefore no
  working way to point the event stream anywhere but port 80. `.host` and
  `.port` are now parsed once and used for the WebSocket. Invisible against the
  camera, which is always `192.168.0.1:80`; found immediately by pointing the
  client at the simulator.

### Packaging
- New `testing` extra (`starlette`, `uvicorn`, `pytest`, plus `pyks2[async]`).
  `dev` now includes it. `import pyks2` still works with no extras installed;
  only building or running the simulator raises, and the error names the extra.
- `pyks2.testing` added to the distribution, with its `data/` declared as
  package-data so the fixtures land in the **wheel** as well as the sdist.
- CI installs `[dev,async,testing]`.

## [1.1.0] — 2026-07-29

Three additive features on top of 1.0.0, all now hardware-verified. All are
backward-compatible — no existing public API changed behaviour. This promotes
`1.1.0b1` unchanged in behaviour: the only code differences are the version
bump and the removal of the async caveats, which the verification below
retired.

### Added
- **Live view context manager**: `with cam.liveview() as stream: for frame in
  stream: ...` guarantees the underlying streaming Response (and therefore
  the camera's mirror-up state) closes on `__exit__`, even if the caller
  breaks out of the loop early or an exception propagates through it.
  `liveview_stream()` and `iter_liveview_frames()` are unchanged and still
  supported — the latter's cleanup still depends on the generator being
  exhausted or garbage-collected, which is exactly the gap `liveview()`
  closes. Hardware-verified (same transport as the existing liveview code).
- **Typed exposure-value accessors**: `set_iso()`, `set_aperture()`,
  `set_shutter_speed()`, `set_exposure_comp()`, and `set_wb()` accept native
  Python types (`int`/`"auto"` for ISO, a `fractions.Fraction` of seconds for
  shutter speed, signed floats for EV comp) and consult the camera's
  list-emptiness writability signal (PROTOCOL.md §6.5) *before* writing.
  Writing a camera-controlled value now **raises `KS2UnsupportedError`**
  instead of silently no-opping. Added `CameraConstants.sv_writable` /
  `.xv_writable`, mirroring the existing `tv_writable`/`av_writable`.
  `set_camera_params(**kwargs)` remains the raw, unvalidated escape hatch.
  Hardware-verified (writability semantics per PROTOCOL.md §6.5; value
  encoding validated against captured examples).
- **Async streaming (`pyks2[async]` extra)**: `cam.events_async()` returns an
  `AsyncChangesClient` for `async for ev in cam.events_async(): ...` over
  `/v1/changes`, and `cam.iter_liveview_frames_async()` gives an async live
  view frame iterator. Both share their parsing with the sync path — MJPEG
  framing via the new `MjpegFrameParser` (`pyks2._mjpeg`), event decoding via
  `events._payload_to_event` — so there is no duplicated protocol logic
  between sync and async. Requires the optional `httpx`/`websockets`
  dependencies (`pip install pyks2[async]`); the base install stays
  dependency-light, and `import pyks2` / `import pyks2.async_client` both
  succeed with neither installed — only calling the async APIs raises a
  clear `ImportError` pointing at the extra. Hardware-verified — see below.

### Verified
- **The async transport is now hardware-verified**, closing the one gap left
  open by `1.1.0b1`. Against a physical K-S2 (firmware `01.10`) on 2026-07-29:
  `events_async()` delivered the same `/v1/changes` payload for a capture as
  the already-verified sync `capture_with_events()` — byte-identical
  `{"changed": "storage"}`, same ordering — and also receives `camera` events
  on a settings write. `iter_liveview_frames_async()` yielded real 720×480
  JPEGs (full Pillow decode, not just marker checks) and raises the camera's
  mirror on start / drops it on close, matching the sync path exactly. Full
  record, including what the camera returned, in
  [`docs/VERIFICATION.md`](docs/VERIFICATION.md).
- The camera's `/v1/changes` WebSocket handshake is RFC-6455 compliant (its
  `Sec-WebSocket-Accept` matches), so strict clients like `websockets` connect
  without the leniency the sync client allows for.

### Findings
- **`liveState` never reports live view.** It reads `"idle"` in all three
  groups (`/v1/props`, `/v1/status`, `/v1/status/liveview`) even while frames
  are streaming. Callers must not use it to detect an active stream; the
  reliable signal is the documented `/v1/liveview/zoom` gate (`412` when
  inactive, `200` while streaming — PROTOCOL.md §9). Affects sync and async
  identically; this is camera firmware behaviour, not a pyks2 bug.
- A single capture emits **exactly one** `/v1/changes` message (`storage`),
  with no trailing `camera` frame — confirmed over a 20 s listen window.
  Multiple rapid settings writes coalesce into one `camera` event, consistent
  with PROTOCOL.md §7 describing these as coarse "re-fetch that group" pokes.

### Packaging
- Development status classifier moved from `4 - Beta` to
  `5 - Production/Stable`.
- `MANIFEST.in` now ships `examples/*.bin`, for the raw MJPEG fixture below.

### Examples
- `examples/changes-capture-sequence.jsonl` — the complete `/v1/changes`
  message sequence for one capture.
- `examples/liveview-frame-raw.bin` — one raw `/v1/liveview` multipart part
  with boundary and part headers intact, for replaying real framing.

## [1.0.0] — 2026-07-16

First stable release: an extensive, hardware-verified reverse-engineering of the
Pentax K-S2's built-in WiFi HTTP API, with a Python library, a CLI, and a
protocol write-up. The camera's 38 API endpoint templates are characterised as
confirmed working, confirmed read-only, or confirmed unsupported, with the
remaining gaps noted in the docs.

### The dissection
- Complete map of the `/v1/*` API: five read groups
  (`constants`/`params`/`variables`/`status`/`props`) × four subsystems
  (`camera`/`lens`/`liveview`/`device`), plus capture, focus, photo, live view,
  and the `/v1/changes` WebSocket.
- Two protocol laws documented: `errCode` lives in the body (not the HTTP
  status), and datetime/numeric formats vary by endpoint.
- Full mode-dial characterisation (P/Sv/Tv/Av/TAv/M/**Bulb**/U1/U2/auto/scene/
  HDR/movie), with a per-mode value-writability matrix driven by list emptiness.
- Hardware interlocks mapped and explained: AF/MF lever, mode dial, movie mode
  disabling WiFi, the SD-door disconnect, device/lens read-only params, and the
  WiFi AP's client isolation.
- 40 real captured responses in `examples/`, plus a machine-readable
  `examples/API_REFERENCE.json`.

### Key findings that corrected earlier assumptions
- **`/v1/photos` does not hang.** The long-standing "it hangs indefinitely"
  belief was a client-side artifact; the endpoint is reliable and scales gently
  with file count. It also supports an undocumented `?limit=N`.
- **`shoot/start` / `shoot/finish` are Bulb exposure controls, not movie**, and
  they work over WiFi when the dial is on B. (`bulb_start()`, `bulb_finish()`,
  `bulb_exposure(seconds)`.)
- **`storages[].remain` is a frame count, not bytes.**
- **Lens identity is not exposed** over WiFi.
- **`/v1/liveview/zoom` is a no-op over WiFi** — accepts any param, returns 200,
  but never changes the frame.

### Library
- `K_S2_WiFi` a camera-only HTTP client with typed models, defensive parsing
  for the datetime/numeric quirks, and `errCode`-aware exceptions.
- Race-free capture via `capture()` (baseline → shoot → wait for the new file).
- Event-driven workflow via the `/v1/changes` WebSocket (`events()`), replacing
  the official app's polling.
- Correct handling of dynamic capability lists (e.g. `avList`) and the
  list-emptiness writability signal for `av`/`tv`.

### CLI
- `pyks2 ping | info | apis | lists | shoot | settings | focus | browse |
  download | liveview | bulb | watch`.

### Not included (planned)
- A web GUI is planned for a later release; this version ships the library, CLI,
  and documentation.
