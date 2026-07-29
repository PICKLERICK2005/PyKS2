# Changelog

All notable changes to **pyks2** are documented here. This project follows
[Semantic Versioning](https://semver.org/).

## [1.2.0rc1] — 2026-07-29

Feature-complete for the 1.2 line, and the simulator's public API is frozen
pending final downstream validation. Everything here is measured: this was the
last session with the physical camera, so the read surface was swept
exhaustively and every remaining hardware-dependent response captured.

### Added
- **The whole client surface now works against the simulator.** Nine public
  calls previously failed. Bulb (`/v1/camera/shoot/start` + `/finish`) was not
  implemented at all; `/v1/lens/focus` was registered GET-only while the client
  POSTs to it; and ten group reads (the bare `/v1/constants`, `/v1/params`,
  `/v1/variables`, `/v1/status` roots plus `constants/lens`,
  `constants/liveview`, `params/liveview`, `variables/lens`,
  `variables/liveview`, `variables/device`) existed in `examples/` but had never
  been copied into package data. A new test iterates the client's entire
  camera-facing surface and asserts nothing fails unexpectedly, rather than
  trusting a hand-written list.
- **A public configuration API**, replacing the documented advice to poke
  `sim._variables`:
  `set_exposure_mode()`, `set_focus_mode()`, `set_camera_controlled()`,
  `set_user_controlled()`, `writable()`, `seed_photos()`, `add_photo()`.
  `set_exposure_mode()` accepts only `"M"` and `"B"` — the two dial positions
  with a real captured capability set — because lists differ per mode and
  inventing them would make the writability signal fiction.
- **Fault injection.** `fail(path, error, times=)` returns a real captured error
  body; `drop(path)`, `delay(path, seconds)` and `drop_stream_after(frames)`
  reproduce transport misbehaviour; `clear_faults()` resets. `ERROR_BODIES`
  advertises only errors that were actually captured — `"precondition"` (412),
  `"bad_request"` (400), `"not_found"` (404), `"unhandled_method"` (a real HTTP
  400 with an HTML body). There is deliberately **no card-full**: that response
  was never captured and the simulator does not invent wire data.
- Bulb is modelled properly: `shoot/start` and `shoot/finish` are gated on the
  dial being on `B`, a plain `shoot` on `B` returns 412, and a completed bulb
  exposure writes a file and fires one `storage` event.

### Fixed
- **Live view serves one stream at a time.** Measured over two trials: opening a
  second stream delivers one more frame to the first and then closes it, and the
  first never recovers — the newest requester wins. The simulator served both
  independently, which it no longer does. (An earlier note in this project
  claiming the camera "permits concurrency" was based on a flawed test that only
  read the second stream's headers.)
- **In MF, `shoot af=auto` returns 412** — a hard refusal, not a silent no-op,
  and no file is written. `POST /v1/lens/focus` fails in MF too, and writing
  `focusMode` over WiFi returns 400 with the value unchanged. All measured.
- `?limit=abc` is **ignored** and returns the full listing; the simulator was
  answering errCode 400. An unrecognised `PUT` key is accepted and ignored;
  the simulator was adding it to the params and firing a spurious `camera` event.
- Replaced the deprecated `asyncio.get_event_loop()` and kept a strong reference
  to the background capture task, which asyncio only holds weakly. The suite now
  runs under `-W error::DeprecationWarning`.

### Changed
- **Python 3.9 is dropped**; `requires-python` is now `>=3.10`. It was already
  untenable: current `starlette` and `uvicorn` both require 3.10, so
  `pip install pyks2[testing]` on 3.9 either failed to resolve or silently
  backslid to versions this code has never been tested against. The ruff target
  and the classifiers move with it.
- **CI runs on `develop`** as well as `main`, across 3.10–3.13 on Linux plus a
  Windows leg, and runs lint. Every simulator change before this had been
  verified on a single machine.
- `.gitattributes` normalises source line endings while keeping the captured
  fixtures byte-exact, verified with `git check-attr`.

### Captured this session
Bulb start/finish bodies and the genuine Bulb-mode empty-list state; the MF lens
state and its refusals; `POST /v1/lens/focus` succeeding; the ten missing group
reads; and an exhaustive 32-endpoint read sweep built from `pyks2.constants`
itself, all of which returned 200. A 2 s bulb exposure reported
`tv: "198.100"` — 1.98 s — independently corroborating the `tv` encoding.
Identifying fields (`ssid`, `key`, `macAddress`, `serialNo`) are redacted to the
repo's existing placeholders, and `PROVENANCE.md` labels those fixtures redacted
rather than raw. It also records what was observed but deliberately **not**
modelled: `camera` event delivery was intermittent (2 of 5 attempts), so the
simulator emits reliably and callers are told not to depend on it.

## [1.2.0b2] — 2026-07-29

The simulator, measured against the camera instead of written from the notes.
The same raw-socket probe was run against the physical body and against the
simulator and the results diffed: **40 of 40 checks now match**, covering wire
behaviour and response times. Full record in
[`docs/VERIFICATION.md`](docs/VERIFICATION.md).

Still beta for the same reason as `1.2.0b1` — the simulator's public API is not
frozen yet, not because anything is unverified.

### Fixed
- **`/v1/liveview/zoom` gating was documented wrong.** PROTOCOL.md §9 claimed an
  empty body returned `200`. Measuring it: with no stream running, all of no
  body, an empty body and `zoom=1` returned `412`; with a stream running, all
  three returned `200`. The gate is purely whether live view is streaming. §9
  corrected and the simulator matches.
- **`latest_info()` does not always report a file.** After a power cycle, with
  358 files on the card, `/v1/photos/latest/info` returned `captured: false` with
  no `dir`/`file` — "latest" means latest *this power session*, not newest on the
  card. The `latest_info()` and `wait_for_capture()` docstrings said otherwise
  and are corrected; the simulator now starts in that state.
- **Captured fixtures were being corrupted by git.** With `core.autocrlf=true`,
  the norm on Windows, a fresh clone rewrote every LF as CRLF: the `/v1/changes`
  payload became 54 bytes instead of the 53 measured on the wire, and
  `photos-listing.json` picked up 29 stray CRs. CI runs on Linux so it would
  never have failed there. A `.gitattributes` now pins the fixture bytes;
  `unhandled-method.html` needed the opposite treatment, since the camera really
  does send CRLF in that body.
- **Not every fixture type was reaching the wheel.** The package-data glob listed
  extensions, so a new `.jpg` and `.html` fixture were silently left out of the
  wheel while the sdist was fine — the failure only shows up from an installed
  wheel. Matched by wildcard now.
- The live view stream busy-looped when the frame interval was zero, flooding the
  socket and starving the event loop, and `stop()` waited its full timeout on any
  open stream. Fixed with a floored interval, a disconnect check, and a forced
  exit after a short grace period. The suite went from 376 s to 21 s.

### Added
- **Modelled response latency** (`pyks2.testing.Timing`), from measured medians:
  ~103 ms for `/v1/props`, ~1.5 s for a 358-file `/v1/photos` (it scales at
  ~110 ms + 3.9 ms per file returned, which is why `?limit` exists), ~1.9 s from
  shutter to `storage` event, ~830 ms for the first live view frame while the
  mirror flips up, ~7.6 fps thereafter. Realistic is the **default**, because a
  mock that answers instantly hides the timeout and ordering bugs a fake camera
  exists to catch; pass `timing=FAST` for none. The `ks2_simulator` fixture is
  fast, and a new `ks2_simulator_realistic` is there when the timing itself is
  under test.
- Generated responses are encoded in the firmware's own JSON house style
  (`camera_json()`), verified by round-tripping captured bodies byte-for-byte, so
  computed responses look like replayed ones on the wire.
- Re-captured fixtures: `/v1/photos` is now a **full card**, 358 files across 6
  directories, so cross-directory flattening and ordering are genuinely
  exercised; and `?size=view` serves the **real 53 KB camera preview** rather
  than a live view frame standing in for one. `?size=full` is now the only
  fabricated payload, an 18 MB DNG being impractical to commit.

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
- Every response carries `Server: server`, `Cache-Control`, `Pragma`, `Expires`,
  `Max-Age` and `Accept-Ranges`, and no `Date`.

### Note on `capture()`
Worth knowing rather than changing: with no latest photo yet, `capture()` passes
`since=None`, which makes `wait_for_capture()` re-read the baseline *after* the
shutter has fired. That is safe against the real camera only because the file
takes ~2 s to appear. An early simulator build created the file instantly and
`capture()` hung, adopting the new file as its own baseline. The simulator now
defers the file exactly as the camera does, with a floor that survives
`timing=FAST`.

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
  are not captured bytes (`?size=view`, `?size=full`) are documented in
  `pyks2/testing/data/PROVENANCE.md`.
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
