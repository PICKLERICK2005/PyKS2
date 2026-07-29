# Hardware verification record

Evidence behind pyks2's "hardware-verified" claims. Each entry records what was
run against a physical camera, what the camera actually returned, and the
pass/fail verdict per path. Anything not listed here is not claimed as verified.

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
