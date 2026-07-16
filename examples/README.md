# Captured API examples

Real, unmodified JSON responses captured from a physical **Pentax K-S2**
(firmware `01.10`) over its built-in WiFi HTTP API. These are the raw evidence
behind the dissection.

## The reference file

**`API_REFERENCE.json`** is the single source of truth: every endpoint, its
method, verified status, purpose, timing, quirks, and a pointer to the example
response below. It is both human-readable and machine-readable (it drives the
`pyks2` library's endpoint definitions and tests).

## Response captures

| File | Endpoint | Notes |
|------|----------|-------|
| `apis.json` | `GET /v1/apis` | Self-describing endpoint list |
| `ping.json` | `GET /v1/ping` | Heartbeat + ISO datetime |
| `props.json` | `GET /v1/props` | Legacy flat superset |
| `constants-*.json` | `GET /v1/constants/{facet}` | Static capability lists |
| `params-*.json` | `GET /v1/params/{facet}` | Current settings |
| `variables-*.json` | `GET /v1/variables/{facet}` | Params + lists + live |
| `status-*.json` | `GET /v1/status/{facet}` | Transient runtime state |
| `props-*.json` | `GET /v1/props/{facet}` | Per-facet legacy slice (camera/lens/liveview/device) |
| `photos-listing.json` | `GET /v1/photos` | Card enumeration structure |
| `photos-latest-info.json` | `GET /v1/photos/latest/info` | Latest shot metadata |
| `camera-shoot-response.json` | `POST /v1/camera/shoot` | Capture response |
| `changes-events.jsonl` | `WS /v1/changes` | Event stream samples |
| `error-400-bad-request.json` | (various) | `errCode` 400 body shape |
| `error-412-precondition.json` | `shoot/start`,`finish` (non-bulb) | `errCode` 412 body shape |
| `constants.json` / `params.json` | `GET /v1/constants` / `/v1/params` (bare) | Merged roots: constants+device identity; params+lens+device |
| `lens-focus-response.json` | `POST /v1/lens/focus` | AF trigger success (focused:true) |
| `camera-shoot-start-bulb.json` / `camera-shoot-finish-bulb.json` | `POST /v1/camera/shoot/start` / `finish` | Bulb exposure open/close (dial=B) |

## Two protocol laws to remember

1. **`errCode` lives in the body, not the HTTP status.** The HTTP line is
   almost always `200`; the real status is the JSON `errCode` field. (Unhandled
   methods like `DELETE` are the exception, they return raw HTML.)
2. **Datetime formats vary by endpoint.** `/v1/ping` is ISO-8601;
   `/v1/photos/.../info` is `YY:MM:DD:HH:MM:SS`. Parsers must tolerate both.

See `../docs/PROTOCOL.md` for the full dissection and methodology.
