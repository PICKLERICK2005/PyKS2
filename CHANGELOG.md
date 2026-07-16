# Changelog

All notable changes to **pyks2** are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [1.0.0] — 2026-07-16

First stable release: an extensive, hardware-verified reverse-engineering of the
Pentax K-S2's built-in WiFi HTTP API, with a Python library, a CLI, and a
protocol write-up. The camera's 38 API endpoint templates are characterised as
confirmed working, confirmed read-only, or confirmed unsupported, with the
remaining gaps noted in the docs.

### The dissection
- Broad map of the `/v1/*` API: five read groups
  (`constants`/`params`/`variables`/`status`/`props`) × four subsystems
  (`camera`/`lens`/`liveview`/`device`), plus capture, focus, photo, live view,
  and the `/v1/changes` WebSocket.
- Two protocol laws documented: `errCode` lives in the body (not the HTTP
  status), and datetime/numeric formats vary by endpoint.
- Mode-dial behaviour is characterised (P/Sv/Tv/Av/TAv/M/**Bulb**/U1/U2/auto/scene/
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
