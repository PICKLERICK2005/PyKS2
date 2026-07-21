# Changelog

All notable changes to **pyks2** are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [1.1.0b1] — 2026-07-21

Three additive features on top of 1.0.0. All are backward-compatible — no
existing public API changed behaviour.

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
  clear `ImportError` pointing at the extra. **NOT yet verified against
  physical hardware.** The sync `ChangesClient` handshake and
  `MjpegFrameParser` framing this reuses ARE hardware-verified; what's
  unverified is the async transport (httpx/websockets) driving them against
  the real camera's WiFi. Treat as inferred-correct pending that
  verification.

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
