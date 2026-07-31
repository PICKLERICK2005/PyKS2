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

Two generated bodies have no fixture behind them:

- **`latest/info` before any capture**, `{"errCode": 200,\n "errMsg": "OK",\n
  "captured": false}`. Re-capturing that state needs a power cycle, so it is
  asserted against the observed bytes in the tests rather than stored as a file.
- **`POST /v1/liveview/zoom` succeeding** (200 with a stream running). The *gate*
  was measured exhaustively — 412 without a stream, 200 with one, whatever the
  body — but the success body's bytes were not kept, so they are **inferred**:
  `camera_json({"errCode": 200, "errMsg": "OK"})`. That is the house style of
  every captured two-member OK body except `camera-shoot-finish-bulb.json`, which
  is compact. Treat the zoom success body's *formatting* as unverified; its
  `errCode` is not.

Error bodies are never generated. The firmware formats them unlike its data
bodies — `{"errCode": 400,"errMsg": "Bad Request"}`, with no break after the
comma — so `camera_json()` does not reproduce them, and every refusal the
simulator issues serves the captured file. (An illegal `PUT /v1/params/camera`
value used to rebuild its 400 and so differed from the capture by two bytes;
fixed in 1.2.0.)

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
all four `*List`s non-empty, so by default every exposure value is writable. Use
the public configuration API:

```python
sim.set_camera_controlled("sv")    # ISO now camera-controlled
sim.set_user_controlled("sv")      # back to the captured list
sim.set_exposure_mode("B")         # the real Bulb capture: tvList/xvList empty
```

Faults are keyed by the `paths.*` constants rather than URL strings:

```python
from pyks2.testing import paths

sim.fail(paths.SHOOT, "precondition")   # a real captured 412 body
sim.drop(paths.PROPS)                   # connection dies mid-response
```

Earlier releases documented poking `sim._variables` directly. That is retired —
`set_camera_controlled()` and friends are supported API.

`set_exposure_mode()` deliberately accepts only `"M"` and `"B"`, the two dial
positions with a captured capability set. Capability lists differ per mode, so
accepting an arbitrary mode would mean inventing them and turning the
writability signal into fiction.

## Modes and states captured on 2026-07-29

| File | Physical state |
|---|---|
| `params-camera.json`, `variables-camera.json` | dial on `M`, all four lists populated |
| `params-camera-bulb.json`, `variables-camera-bulb.json` | dial on **`B`**: `tvList` and `xvList` **empty** — the camera owns shutter and exposure compensation in Bulb |
| `camera-shoot-start-bulb.json`, `camera-shoot-finish-bulb.json` | a real 2 s bulb exposure on `B`; the resulting frame reported `tv: "198.100"` (1.98 s) |
| `params-lens.json`, `variables-lens.json`, `status-lens.json` | AF/MF switch on **AF** |
| `params-lens-mf.json`, `variables-lens-mf.json`, `status-lens-mf.json` | AF/MF switch on **MF** |
| `lens-focus-response.json` | `POST /v1/lens/focus` succeeding in AF |

`set_focus_mode()` switches all three lens read groups to the matching capture,
which is why both sets exist. They are replayed rather than derived because the
camera contradicts itself: in **AF** it reports `focused: false` on `status/lens`
and `props/lens` but `focused: true` on `variables/lens`, in one physical state.
In **MF** all three say `true`. No rule reproduces that, and 1.2.0rc1 tried to —
it derived `focused` as "MF means focused", which inverted `variables/lens` in AF
and left `status/lens` on the AF capture regardless of the lever.

`props/lens` is the exception: it is the legacy flat superset and was only
captured in AF, so it does not follow the lever. Nothing was invented to make it.

One error body serves several scenarios because the camera really does send the
same bytes for all of them — verified by hash. `error-412-precondition.json` is
the response to: `/v1/liveview/zoom` with no stream, `shoot/start` or
`shoot/finish` off `B`, a plain `shoot` on `B`, `shoot af=auto` in MF, and a
`shoot` while live view is streaming. `error-400-bad-request.json` likewise
covers an illegal parameter value and the refusal to write `focusMode`.

## Redacted, not raw

`macAddress`, `serialNo`, `ssid` and `key` are replaced with the repo's
placeholders (`00:11:22:33:44:55`, `0000000`, `PENTAX_XXXXXX`, `XXXXXXXX`) in
every fixture that carries them — `props.json`, `props-device.json`,
`constants-device.json`, `params-device.json`, `variables-device.json`,
`constants.json`, `params.json`, `variables.json`. Those files are therefore
**redacted, not raw**: byte-exact apart from those four values. Shipping a real
camera's WiFi key in a package on PyPI is not acceptable, and the placeholders
match what `examples/` has always used.

## Observed but not modelled

- **`camera` events are intermittent.** A settings write is documented to emit
  `changed: "camera"`, and the payload is byte-exact when it arrives, but on
  2026-07-29 only 2 of 5 attempts delivered one (~3.0 s latency; ~6.2 s in an
  earlier session), and once the newest of two WebSocket clients received a
  flushed backlog of three while the older client got none. The simulator emits
  reliably, because an unreproducible behaviour is not worth encoding. **Callers
  should not treat `camera` events as a guarantee — poll after writes.**
  `storage`-on-capture was reliable throughout and is what the capture flow
  depends on.
- **Card full.** Never captured: the test card had thousands of frames free, and
  it cannot be forced without filling it. There is deliberately no `card_full`
  entry in `ERROR_BODIES` — inventing that body would break the rule that every
  byte on the wire is real. Asking for it raises with a pointer to
  `examples/status-device-cardfull.json` **in the repo**, a genuine capture of a
  nearly-full card (`remain: 1`). It is a *state* rather than a failure response,
  so it is not shipped here as serving data — and its formatting was normalised
  when it was written into `examples/`, so its values are real while its bytes
  are not the wire bytes. That is also why it is not a fixture: promoting it
  would mean re-encoding it and calling the result captured.
