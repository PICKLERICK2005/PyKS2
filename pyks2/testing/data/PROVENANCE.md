# Simulator fixture provenance

These files are the simulator's serving data. They are **copies of the real
captures** in the repo's `examples/` directory, duplicated here on purpose:
`examples/` is not installed, so a simulator reading from it would work in a git
checkout and break for anyone who ran `pip install pyks2[testing]`. Everything
here ships in both the sdist and the wheel.

All of it came off a physical **Pentax K-S2**, firmware `01.10`. See
`../../../examples/README.md` for the original capture provenance and
`../../../docs/VERIFICATION.md` for the hardware-verification record behind the
`/v1/changes` and `/v1/liveview` captures.

| File | Endpoint |
|---|---|
| `ping.json`, `apis.json` | `GET /v1/ping`, `GET /v1/apis` |
| `props.json`, `props-*.json` | `GET /v1/props`, `GET /v1/props/{sub}` |
| `params-camera.json`, `params-lens.json`, `params-device.json` | `GET /v1/params/{sub}` |
| `constants-camera.json`, `constants-device.json` | `GET /v1/constants/{sub}` |
| `variables-camera.json` | `GET /v1/variables/camera` — source of the `*List` writability signal |
| `status-*.json` | `GET /v1/status/{sub}` |
| `photos-listing.json` | `GET /v1/photos` |
| `photos-latest-info.json` | `GET /v1/photos/latest/info` |
| `camera-shoot-response.json` | `POST /v1/camera/shoot` |
| `lens-focus-response.json` | `POST /v1/lens/focus` |
| `error-400-bad-request.json`, `error-412-precondition.json` | `errCode` body shapes |
| `changes-events.jsonl` | `WS /v1/changes` — both `changed` kinds |
| `changes-capture-sequence.jsonl` | `WS /v1/changes` — complete sequence for one capture |
| `liveview-frame-raw.bin` | `GET /v1/liveview` — one raw multipart part, framing intact |

## Where the simulator does not replay captured bytes

Two payloads are not real captures, because no capture exists. Both are called
out in the code that produces them:

1. **`?size=view` photo download** serves the JPEG extracted from
   `liveview-frame-raw.bin`. Those are real camera JPEG bytes, but from a live
   view frame, not a photo preview — no preview binary was ever captured.
2. **`?size=full` photo download** is synthetic: TIFF magic (`II*\0`) plus
   padding. A real DNG is ~18 MB and none is committed (`*.dng` is gitignored).
   It is sized and shaped only to satisfy the client's magic sniff and
   `min_bytes` guard.

## Two internal inconsistencies in the source captures

Worth knowing if a value looks surprising:

- `photos-latest-info.json` names `112_1106/IMGP0341.DNG`, which
  `photos-listing.json` does not contain — the two were captured from different
  cards. The simulator therefore uses the latest-info file as a *metadata
  template* and overrides `dir`/`file` to stay consistent with its own listing.
- `params-camera.json` and `variables-camera.json` were captured in `M` mode
  with all four `*List`s non-empty, so in the default simulator every exposure
  value is writable. To exercise the camera-controlled path (empty list → PUT
  returns 200 and is silently ignored), empty a list on the instance:

  ```python
  sim = CameraSimulator()
  sim._variables["svList"] = []      # ISO now camera-controlled
  ```
