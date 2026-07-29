# Simulator fixture provenance

These files are the simulator's serving data. They live here, rather than being
read from the repo's `examples/`, because `examples/` is not installed: a
simulator reading from it would work in a git checkout and break for anyone who
ran `pip install pyks2[testing]`. Everything here ships in both the sdist and the
wheel.

All of it came off a physical **Pentax K-S2**, firmware `01.10`. The bodies are
unmodified response bytes. See `../../../docs/VERIFICATION.md` for the
hardware-verification and fidelity records.

| File | Endpoint |
|---|---|
| `ping.json`, `apis.json` | `GET /v1/ping`, `GET /v1/apis` |
| `props.json`, `props-*.json` | `GET /v1/props`, `GET /v1/props/{sub}` |
| `params-camera.json`, `params-lens.json`, `params-device.json` | `GET /v1/params/{sub}` |
| `constants-camera.json`, `constants-device.json` | `GET /v1/constants/{sub}` |
| `variables-camera.json` | `GET /v1/variables/camera` — source of the `*List` writability signal |
| `status-*.json` | `GET /v1/status/{sub}` |
| `photos-listing.json` | `GET /v1/photos` — full card, 358 files across 6 dirs |
| `photos-latest-info.json` | `GET /v1/photos/latest/info` |
| `photo-info.json` | `GET /v1/photos/{dir}/{file}/info` (a non-latest file) |
| `photo-preview-view.jpg` | `GET /v1/photos/{dir}/{file}?size=view` — real 53 KB preview |
| `camera-shoot-response.json` | `POST /v1/camera/shoot` |
| `lens-focus-response.json` | `POST /v1/lens/focus` |
| `error-400-bad-request.json`, `error-404-not-found.json`, `error-412-precondition.json` | `errCode` body shapes |
| `unhandled-method.html` | the HTML body an unhandled method returns |
| `changes-events.jsonl` | `WS /v1/changes` — both `changed` kinds |
| `changes-capture-sequence.jsonl` | `WS /v1/changes` — complete sequence for one capture |
| `liveview-frame-raw.bin` | `GET /v1/liveview` — one raw multipart part, framing intact |

The listing, latest-info, photo-info, preview JPEG and error bodies were
re-captured on **2026-07-29** during the fidelity pass, which is why the listing
and latest-info now agree with each other (`105_2907/IMGP2331.DNG` is present in
the listing) where earlier copies came from different cards.

## Generated rather than replayed

Responses that depend on simulator state have to be built. They are encoded with
`camera_json()`, which reproduces the firmware's JSON house style — `,\n `
between members, `[ ` opening a non-empty array, a trailing newline. That is
checked by round-tripping `photos-listing.json`, `photo-info.json` and
`photos-latest-info.json`: re-encoding the parsed form gives back the original
bytes exactly.

`props.json` is *not* round-trippable — the firmware formats it inconsistently
(`"storages" : [`, space before the colon) — so it and every other static
response are served verbatim instead.

The one generated body with no fixture is `latest/info` before any capture,
`{"errCode": 200,\n "errMsg": "OK",\n "captured": false}`. Re-capturing that
state needs a power cycle, so it is asserted against the observed bytes in the
tests rather than stored as a file.

## Where the simulator is not the camera

Three deliberate deviations, none observable through an HTTP client:

1. **`?size=full` photo download is synthetic** — TIFF magic plus padding. A real
   DNG is ~18 MB and none is committed (`*.dng` is gitignored). It is shaped only
   to satisfy the client's magic sniff and `min_bytes` guard. The only fabricated
   bytes here.
2. **Header casing, order, and `Connection`** — uvicorn lowercases header names,
   orders them its own way, writes `Content-Length: N` where the camera writes
   `Content-Length:N`, and owns the connection lifecycle. `Connection` is
   hop-by-hop, so overriding it fights the server rather than emulating the
   camera. Header names are case-insensitive and the space after a colon is
   optional, so nothing here is functionally visible.
3. **MJPEG uses chunked transfer-encoding** — the camera writes the multipart
   body raw, delimited only by connection close; an ASGI server must use chunked
   framing for an unbounded response. Every HTTP client decodes it transparently
   (`requests`, `httpx`, browsers), so parsers see identical bytes. Only a
   raw-socket reader sees the chunk-size lines.

## Exercising the camera-controlled path

`params-camera.json` and `variables-camera.json` were captured in `M` mode with
all four `*List`s non-empty, so by default every exposure value is writable. To
test the camera-controlled path (empty list → PUT returns 200 and is silently
ignored), empty a list on the instance:

```python
sim = CameraSimulator()
sim._variables["svList"] = []      # ISO now camera-controlled
```
