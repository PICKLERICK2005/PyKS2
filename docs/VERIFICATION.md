# Hardware verification record

Evidence behind pyks2's "hardware-verified" claims. Each entry records what was
run against a physical camera, what the camera actually returned, and the
pass/fail verdict per path. Anything not listed here is not claimed as verified.

---

## 2026-07-29 — simulator fidelity (`pyks2.testing`)

The shipped simulator claims to behave like the camera, so it was measured
against one: the same raw-socket probe was run against the physical body and
against the simulator, and the results diffed.

### Rig

Pentax K-S2, firmware `01.10`, serial `4477116`, over AP `PENTAX_6C5AA9`
(RSSI −39 dBm, battery 33%). Probe recorded verbatim status lines, headers,
bodies and timings; one shutter actuation was used (`IMGP2331.DNG`).

### Result

**40 of 40 checks match**, covering wire behaviour (error codes, gating, listing
shape, PUT shape, MJPEG framing, WebSocket frame bytes) and latency (medians
within tolerance). Three deviations are deliberate and documented in
`../pyks2/testing/data/PROVENANCE.md`: header casing/order, chunked
transfer-encoding on the MJPEG stream, and the synthetic `?size=full` payload.
None is observable through an HTTP client.

### Measured latency, now modelled by `Timing`

| Operation | Real camera | Simulator |
|---|---|---|
| `GET /v1/ping` | 60 ms | modelled |
| `GET /v1/props` | 103 ms median (76–186) | 125 ms |
| `GET /v1/props/{sub}` etc. | ~70 ms | modelled |
| `GET /v1/photos` (358 files) | 1485 ms | 1514 ms |
| `GET /v1/photos?limit=2` | 165 ms | 124 ms |
| `PUT /v1/params/camera` | 160 ms | 188 ms |
| `POST /v1/camera/shoot` | 191 ms | 216 ms |
| `GET ...?size=view` (53 KB) | 268 ms | 283 ms |
| shoot → `storage` event | 1933 ms (3.4 s seen earlier) | 1966 ms |
| live view first frame | 834 ms (mirror flip-up) | 843 ms |
| live view frame interval | 103 ms median (44–201), ~7.6 fps | 109 ms |

`/v1/photos` scales with the number of files *returned* — ~110 ms + ~3.9 ms per
file — which is the whole reason `?limit` exists.

### Corrections to earlier claims

- **`/v1/liveview/zoom` is not body-dependent.** PROTOCOL.md §9 said an empty
  body returned `200`. With no stream running, all of no body, an empty body and
  `zoom=1` returned `412`; with a stream running, all three returned `200`. The
  gate is purely whether live view is streaming. §9 corrected.
- **`/v1/photos/latest/info` does not always report `captured: true`.** After a
  power cycle with 358 files on the card it returned
  `{"errCode": 200, "errMsg": "OK", "captured": false}` with no `dir`/`file`.
  "Latest" tracks the current power session, not the card. `latest_info()` and
  `wait_for_capture()` docstrings corrected.

### Behaviours newly pinned down

- A `PUT /v1/params/camera` echoes a **`variables`-shaped** body: `avList`,
  `tvList`, `svList`, `xvList`, `exposureModeOption` and `state` on top of the
  values a `GET` returns.
- `?limit=N` keeps **every** directory in the response, giving those past the
  limit an empty `files` list; `limit=0` means *no limit*. (A limit of exactly
  one less than the total returned everything — 357 of 358 gave 358 — which looks
  like a firmware off-by-one and is not reproduced.)
- A missing photo is `errCode 404`; an unknown path is `errCode 400`, both under
  HTTP 200. An unhandled **method** is the one break from Law 1: a real HTTP
  `400` with an HTML body.
- The `/v1/changes` payload ends with a **newline** — the storage frame is 53
  bytes on the wire, unmasked opcode 1.
- Every response carries `Server: server`, `Cache-Control: no-cache, no-store,
  max-age=0, must-revalidate`, `Pragma: no-cache`, `Expires: 0`, `Max-Age: 0`,
  `Accept-Ranges: bytes`, and **no `Date`**.
- The card holds a RAW+JPEG pair sharing shot number 2224, and the camera lists
  `IMGP2224.JPG` before `IMGP2224.DNG` — so listing order is ascending shot
  number, not sorted filenames.

### Bug this pass found in pyks2 itself

`capture()` reads its baseline from `latest_info()`, and when that is empty it
passes `since=None`, which makes `wait_for_capture()` re-read the baseline
*after* the shutter has fired. That is safe against the real camera only because
the file takes ~2 s to appear. An early simulator build created the file
instantly and `capture()` hung, adopting the new file as its own baseline. The
simulator now defers the file (and the event) exactly as the camera does, with a
floor that never collapses to zero even when latency is switched off.

---

## 2026-07-29 — async streaming path (`pyks2[async]`)

Closes the one gap left open by 1.1.0b1: the async transport layer
(`websockets`/`httpx`) driving the already-verified sync parsing logic against
the real camera.

### Rig

| | |
|---|---|
| Camera | Pentax K-S2, firmware `01.10`, serial `4477116` |
| Transport | Camera WiFi AP `PENTAX_6C5AA9`, 2.4 GHz ch 1, RSSI −39 dBm |
| Host | Windows 11, CPython 3.13.14 |
| Deps | `websockets` 16.1.1, `httpx` 0.28.1, `Pillow` 12.3.0 (validation only) |
| Under test | pyks2 `1.1.0b1` @ `f94dc1c` (`pyks2/async_client.py`) |
| Camera state | `state: idle`, `focusMode: af`, `exposureMode: TAV`, battery 100 %, sd1 writable (2755 frames free) |

Preflight: `GET /v1/props` → `200`. Mode dial in an AF position (`focusMode:
af`), required because firing over WiFi with `af=auto` does not trigger in MF.

### Result summary

| Path | Verdict |
|---|---|
| `events_async()` — `/v1/changes` over `websockets` | **PASS** |
| `iter_liveview_frames_async()` — `/v1/liveview` over `httpx` | **PASS** |

---

### 1. Async event stream — `events_async()`

**Handshake.** Probed the raw upgrade first, because `events.ChangesClient`
treats an `Sec-WebSocket-Accept` mismatch as non-fatal, which would break a
strict RFC-6455 client like `websockets`. The camera is in fact compliant:

```
HTTP/1.1 101 Switching Protocols
Upgrade: WebSocket
Connection: Upgrade
Sec-WebSocket-Accept: XJTSceqCUrD/EZkqSEzF0Cdqd8A=
```

Sent key `68/CWWPBv9RYGyCvtubZJQ==` → expected accept
`XJTSceqCUrD/EZkqSEzF0Cdqd8A=`. **Match.** So `websockets` accepts the camera's
handshake unmodified; no leniency needed on the async side.

**Capture event.** Opened `cam.events_async()`, then fired
`POST /v1/camera/shoot` (`af=auto`) with the stream already connected:

```
async WS connected (websockets transport)
shoot -> ShootResult(focused=True, focus_centers=[], captured=False,
         raw={'errCode': 200, 'errMsg': 'OK', 'focused': True,
              'focusCenters': [], 'captured': False})
ASYNC EVENT t+3.97s changed='storage'
```

Raw frame off the wire:

```json
{"errCode": 200,"errMsg": "OK","changed": "storage"}
```

Yielded a `ChangeEvent(changed='storage')` with `is_storage=True`. The shot
landed as `105_2907/IMGP2328.DNG` (`captured: true`).

**Parity with the verified sync path.** Ran `capture_with_events(af="auto")`
immediately after, recording its raw payloads the same way. It produced
`105_2907/IMGP2329.DNG` and the **byte-identical** payload:

```json
{"errCode": 200,"errMsg": "OK","changed": "storage"}
```

Async and sync agree on payload set and ordering (`identical_payload_sets:
true`, `same_sequence: true`). Same event, same shape, different transport.

**Completeness of the sequence.** Re-ran the capture with no early exit and a
20 s listen window to be sure nothing was being truncated: exactly **one**
message per capture (`storage` at t+3.40 s), with no trailing `camera` frame.
Captured as `examples/changes-capture-sequence.jsonl`.

**Both event kinds reach the async transport.** A settings write
(`set_exposure_comp(+0.3)`, then restored to `0.0`) produced
`{"errCode": 200,"errMsg": "OK","changed": "camera"}` at t+6.25 s over the same
async client — so `events_async()` sees `camera` events, not just `storage`.
Two writes coalesced into one event, consistent with PROTOCOL.md §7 describing
these as coarse "go re-fetch that group" pokes.

**Verdict: PASS.**

---

### 2. Async live view — `iter_liveview_frames_async()`

**Frames.** Iterated the async generator; every frame fully decoded with
Pillow (`verify()` plus a full `load()`), not merely marker-checked:

```
async frame 1: 27024 bytes
async frame 2: 26715 bytes
async frame 3: 26779 bytes   <- mirror probe taken here
async frame 4: 26782 bytes
async frame 5: 26779 bytes
async frame 6: 26730 bytes
```

All 6: `format=JPEG`, `mode=RGB`, `size=720x480`, SOI `ffd8` / EOI `ffd9`.
Matches the 720×480 / ~23 KB baseline in PROTOCOL.md §9 and the sync path's
frames (same dimensions, same byte-size band).

**Mirror raises on start / drops on exit.** `liveState` is useless as an oracle
here — it reads `"idle"` in **all three** groups (`/v1/props`, `/v1/status`,
`/v1/status/liveview`) even mid-stream, at rest and during. This is camera
firmware behaviour, identical on the sync path, not an async defect.

The working oracle is the documented `POST /v1/liveview/zoom` gate
(PROTOCOL.md §9): parameters return `412` unless live view is actively
streaming, `200` while it is. Probing it around the async stream:

| Point in time | `errCode` | Meaning |
|---|---|---|
| Before opening the stream | `412` | live view inactive — mirror down |
| During, at frame 3 | `200` | live view active — **mirror up** |
| 1.5 s after `aclose()` | `412` | live view inactive — **mirror dropped** |

So the async iterator raises the mirror when it starts streaming and drops it
when the generator closes.

**Parity with the verified sync path.** The same three probes around
`cam.liveview(max_frames=6)` gave the same `412 → 200 → 412`, with 6 frames
also decoding as 720×480 RGB JPEG. Async matches sync exactly.

**Verdict: PASS.**

---

### Fixtures captured for the mock-server work

Recorded while the camera was connected, so that round needs no hardware:

- `examples/changes-capture-sequence.jsonl` — complete `/v1/changes` sequence
  for one capture (one `storage` frame; verified nothing follows).
- `examples/liveview-frame-raw.bin` — one raw `/v1/liveview` multipart part,
  27 379 bytes, boundary and part headers intact:
  `--boundarydonotcross\r\nContent-type: image/jpg\r\n\r\n<JPEG>`, stream
  `Content-Type: multipart/x-mixed-replace; boundary=--boundarydonotcross`.
- `/v1/props`, `/v1/photos`, `/v1/photos/latest/info` were re-captured and
  matched the existing `examples/props.json`, `photos-listing.json` and
  `photos-latest-info.json` key-for-key, so no duplicate fixtures were added.

### Side effects

Three real exposures written to sd1 (`IMGP2328`–`IMGP2330.DNG`). `xv` was
moved to `+0.3` and restored to `0.0`; camera left `state: idle`, mirror down.
