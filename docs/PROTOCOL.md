---
title: Protocol Dissection
nav_order: 2
---

# The Pentax K-S2 WiFi API; a dissection

Everything here was verified against a physical **Pentax K-S2** running firmware
`01.10`. Where a claim rests on an observation, the observation is stated. Where
the camera's behaviour contradicts "known" stuff (or contradicts this project's own
earlier assumptions), the contradiction is called out and resolved with data.

The camera exposes an undocumented HTTP API on `192.168.0.1` when its built-in
WiFi is active. There is no official developer documentation; the only public
prior art is a partial 2016 write-up of the closely-related K-1. This document
is a behaviourally-verified map of the K-S2's surface as far as I could
exercise it.

---

## 1. The shape of the API

The camera answers HTTP on port 80. It is self-describing: `GET /v1/apis`
returns the full list of endpoint templates.

The surface is organised as **five read "groups" crossed with four
"facets"**, plus a handful of action endpoints and a WebSocket.

The four facets are `camera`, `lens`, `liveview`, and `device`. The five groups
are different *views* of the same underlying state:

| Group | What it returns | Mutable? | Use it for |
|-------|-----------------|----------|------------|
| `constants` | Static capability lists (`*List` arrays) | No, never changes | Dropdown option sources — fetch once |
| `params` | Current set values | **GET + PUT** | Read/write live settings |
| `variables` | Params + lists + live values | Read-only | The "everything" poll |
| `status` | Transient runtime state | Read-only | `state`, `focused`, `liveState` |
| `props` | Flat legacy superset (K-1 style) | Read-only | Back-compat blob |

So `GET /v1/constants/camera` gives you the *possible* white-balance modes;
`GET /v1/params/camera` gives you the one that's *currently set*; `PUT
/v1/params/camera` changes it. The decomposition is clean and consistent across
all four facets, a nicer design than the K-1's single monolithic `props` blob,
which the K-S2 still provides for compatibility!

---

## 2. Two laws that govern every request

### Law 1 — `errCode` lives in the body, not the HTTP status

This is the single most important thing to know about this API. The HTTP status
line is **ALMOST ALWAYS `200`**, even when the request logically failed. The
real status is a field inside the JSON body:

```json
{"errCode": 412, "errMsg": "Precondition Failed"}
```

A client that trusts the HTTP status code will treat failures as successes. Every
request must parse the body and read `errCode`. Observed codes: `200` (ok), `400`
(bad request / illegal value), `412` (precondition failed).

There is exactly one exception: a genuinely **unhandled HTTP method** (e.g.
`DELETE`) bypasses the JSON layer and returns a raw HTML `400 Bad Request` page.
So "body is HTML, not JSON" is itself a signal, it means the method isn't
handled at all.

### Law 2 — datetime formats are inconsistent

Two endpoints, two formats:

- `GET /v1/ping` → ISO-8601: `2026-07-15T11:43:15`
- `GET /v1/photos/.../info` → colon-packed `YY:MM:DD:HH:MM:SS`: `26:07:15:11:09:11`

Numeric fields wobble too — exposure compensation `xv` has been seen as `"0"`,
`"0.0"`, and `"-0.7"` across responses. Any parser must tolerate all forms.
`pyks2` normalises these on the way in.

---

## 3. Reading state

All read endpoints return `200` quickly (~40–120 ms) and carry no side effects.
`GET /v1/ping` is the cheapest and is the recommended heartbeat — it never
triggers the card-read behaviour that other endpoints can, keeping things fast
and avoiding camera-side hangs.

The decomposed endpoints (`/v1/{group}/{facet}`) are preferred over the
monolithic `/v1/props`. They return exactly the slice you need which is far
more efficient, and combined with the `/v1/changes` WebSocket (§7) they let 
a client stay current without polling.

Captured examples for all of these live in [`examples/`](../examples/).

One schema curiosity: `variables/camera` carries an `exposureModeOption` field
that is **empty (`""`) in every mode** on this body (checked across M, Av, Tv, P,
autopict/scene, and TAv). It exists in the schema but seems to be unused on the K-S2,
probably populated on bodies/modes with a sub-selection. Safe to ignore here.

---

## 4. Writing settings

`PUT /v1/params/camera` with a form-encoded body writes settings:

```
PUT /v1/params/camera
av=8.0&sv=400
```

Three verified behaviours:

- **It sticks, and it echoes the full state back.** The response to a PUT is the
  complete camera state (lists + values), so a write doubles as a read.
- **It validates server-side.** An illegal value (`av=99`) is rejected with
  `errCode 400`. The client doesn't have to pre-validate, though checking against
  the `constants` lists gives a nicer UX.
- **The option lists are dynamic.** `avList` is not fixed, it changes with the
  lens and current aperture. After changing exposure mode or lens state, the
  available apertures differ. **Never cache the lists globally**; re-fetch after
  changes.

`focusMode` (on `/v1/params/lens`) and the WiFi `device` params look writable but
are **not** — see §6.

### `tv` is only writable when the mode lets the user control it

A subtle but clean rule, confirmed by writing `tv` across every exposure mode:
**`tv` is writable exactly when the camera reports a non-empty `tvList`, which
is exactly the modes where the user owns the shutter: M, Tv, P, and TAv (all
report a 54-value `tvList`).** In Av (aperture priority) and the auto/scene
modes (`autopict`, etc.), `tvList` comes back empty and the camera controls
shutter itself; a `tv` write returns `200` but is **silently ignored** (the
value doesn't change). So the list is not just a dropdown source, its
emptiness is the signal that the field is camera-controlled in the current
mode. Check `tvList` before offering `tv` as an editable control.

(One edge seen: in Tv mode a specific token like `1.320` occasionally didn't
stick even though the list was non-empty. The camera clamps to what the
current lens/ISO allows. Treat a write as "requested", then read back to
confirm what actually took.)

---

## 5. Capture, focus, and bulb exposure

### Stills capture

`POST /v1/camera/shoot` with `af=auto|on|off` fires the shutter.

```json
{"errCode": 200, "focused": true, "focusCenters": [], "captured": false}
```

Three things to internalise:

- **`captured` is always `false` in the immediate response.** Capture is
  asynchronous. To know a frame actually landed, either poll
  `/v1/photos/latest/info` until it updates, or even better- wait for the
  `changed: "storage"` WebSocket event (§7).
- **A `200` does not prove a frame was written.** When the card is full, `shoot`
  *still* returns `errCode 200` with `focused:false, captured:false` and
  nothing saved. There is no storage-specific error code. So success detection
  must be positive (a new file appears), never "the POST returned 200".
- **`af=off` releases the shutter without hunting for focus.** On a manual-focus
  lens or a fixed-focus rig, `af=auto` can make the camera entirely refuse to fire if
  it can't lock. `af=off` always releases. This is the correct choice for a
  controlled studio/rig capture.

### Focus

`POST /v1/lens/focus` with `pos=x,y` drives autofocus and sets the AF point,
returning `focused: true`. Note the asymmetry: you **can** trigger AF and place
the point over WiFi, but you **cannot** switch the camera between AF and MF since
on the KS2 that's the physical lever (§6).

### Bulb exposure: what `shoot/start` and `shoot/finish` really are

This is a correction to an earlier assumption in this project. `POST
/v1/camera/shoot/start` and `/finish` are **not** for video recording (video recording
on the KS2 disables the wifi) they are the **Bulb long-exposure mechanism**, 
and they *work over WiFi*.

The trap: in every non-Bulb mode, both return `errCode 412 Precondition
Failed`, which combined with the fact that movie mode disables WiFi made
them look permanently unreachable. But with the mode dial physically set to
**B (Bulb)**:

- `POST /v1/camera/shoot/start` → **`200`** (opens the shutter)
- `POST /v1/camera/shoot/finish` → **`200`** (closes it)
- plain `POST /v1/camera/shoot` → **`412`** in Bulb mode (use start/finish
  instead)

So the precondition on `shoot/start`/`finish` is simply *"the dial is in Bulb"*.
The elapsed time between `start` and `finish` is the exposure. Bulb
mode also reports empty `tvList` and `xvList` (no shutter-speed or EV-comp
value applies), which is the tell that a mode is bulb rather than a timed
exposure.

---

## 6. What the API cannot touch: the physical interlocks

A recurring theme: several pieces of camera state are governed by physical
controls and are read-only (or invisible) to the API. They might look write-able 
but they are not.

- **AF/MF lever**: read-only over the API, and uniquely, it fires **no**
  change event at all. The API can observe the resulting `focusMode` but cannot
  set it, and can't even tell when you flip it.
- **Mode dial**: read-only. Turning it fires `changed: "camera"`.
- **Movie mode**: not settable, and enabling it kills WiFi (see §5).
- **SD card door**: opening it **kills the WiFi connection** and drops the
  camera toward an off/idle state. An operator must never touch the door mid-session; a
  client must handle sudden disconnection gracefully and wait/prmpt for a reconnect.
- **`device` params** (`ssid`, `channel`, `key`): read-only; `PUT` → `400`. The
  WiFi network cannot be reconfigured through the API, physical interface only.

---

## 6.5 The full mode dial, and what each position exposes

Every physical dial position was characterised over WiFi. The API reports the
active mode in `exposureMode`, and crucially, **which exposure values are
user-settable in that mode is signalled by whether their list is non-empty**
(an empty `avList`/`tvList`/`svList`/`xvList` means the camera controls that
value in this mode; writes return `200` but are ignored):

| Dial | `exposureMode` | av | tv | sv | xv |
|------|----------------|----|----|----|----|
| P | `P` | ✓ | ✓ | ✓ | ✓ |
| Sv | `SV` | – | – | ✓ (28 steps) | ✓ |
| Tv | `TV` | – | ✓ | ✓ | ✓ |
| Av | `AV` | ✓ | – | ✓ | ✓ |
| TAv | `TAV` | ✓ | ✓ | – | ✓ |
| M | `M` | ✓ | ✓ | ✓ | ✓ |
| **B (Bulb)** | `B` | ✓ | – | ✓ | – |
| U1 / U2 | `U1` / `U2` | ✓ | ✓ | ✓ | ✓ |
| Auto Picture (green) | `autopict` | – | – | ✓ | ✓ |
| SCN (Scene) | `scene` | – | – | ✓ | ✓ |
| HDR | `AHDR` | – | – | ✓ | ✓ |
| Movie | *(WiFi disabled)* | — | — | — | — |

Notes:
- **Bulb** exposes no `tv`/`xv` (no shutter-speed value applies) the tell that
  a mode is bulb. Capture it via `shoot/start`/`shoot/finish` (§5).
- **Sv** unlocks a finer 28-value `svList` versus ~11 elsewhere.
- **U1/U2** report their own mode ids and behave like the exposure mode saved
  into them (all values writable in the cases seen).
- **Auto/Scene/HDR** only let you nudge ISO and EV comp; aperture and shutter
  are camera-controlled. The 18 scene sub-modes under `scene` are selected on
  the camera body, not over the API.
- **GPS** appears in `exposureModeList` probably interacts with the optional
  top-mounted GPS unit made by Pentax for star-tracking and such. Remains untested.

### EV compensation and ISO

`xv` (exposure compensation) is writable across the full **+5.0 … −5.0** range
and validated server-side (`xv=+9.9` → `400`). ISO is set via `sv`, and **ISO
AUTO is a real value: `sv=auto`** (returns `200`, then reads back as the
resolved sensitivity); out-of-range numbers (`sv=999999`) are rejected with
`400`.

## 6.6 Hardware state: battery, storage, lens

- **Battery** (`status/device.battery`) is an **integer percentage** observed
  100, 66, and 0, so it's a real percentage but is not to be taken for accurate.
  A `hot` boolean reports thermal state.
- **Storage `remain` is a FRAME COUNT, not bytes.** `storages[].remain` is the
  number of shots left observed `6467` near-empty and `1` with one frame of
  space, dependent on shooting settings (JPEG/RAW/BOTH).
  When files exist, the storage object also carries `dir`/`file` pointing
  at the latest. (A near-full card behaves normally for listing; the only effect
  is `shoot` silently not-saving once truly full — see §5.)
- **Lens identity is not exposed at all.** `constants/lens` is empty, and two
  different lenses (an 18–50 and a 50–200) returned **identical** data, only
  `focusMode`/`focused`/`focusCenters` exist anywhere. You **cannot** tell which
  lens is mounted over WiFi. The one lens-dependent signal is indirect: the
  `avList` in `variables/camera` reflects the mounted lens's aperture range.

---

## 7. The event stream

`GET /v1/changes` upgrades to a **WebSocket** (HTTP `101`). Once connected, the
camera pushes small JSON events when something changes:

```json
{"errCode": 200, "errMsg": "OK", "changed": "storage"}
```

The event vocabulary is deliberately coarse. Across a full physical sweep
(turning the dial, half-pressing, changing aperture and ISO, flipping the AF/MF
lever, firing the shutter, opening the SD door, raising the flash) only **two**
`changed` values ever appear:

- **`camera`**: any camera setting changed (mode dial, aperture, ISO…)
- **`storage`**: a file was written (a photo landed on the card)

Taking a photo fires both. Several physical actions fire *nothing* (AF/MF lever,
half-press, SD door, flash).

So the stream is not a value push, it's a **"go look at this group" nudge**. The
correct pattern is: connect once, and on `changed: X` re-fetch that group's
`params`. This replaces polling entirely and makes it more efficient than the way
Pentax's Image Sync app does things.

A later, longer listen across a wider set of actions (varied setting changes, a
capture, focus, an idle stretch, flash, lens zoom, playback review) produced
**only** `camera` and `storage`, nothing else. The two-value vocabulary is
therefore treated as **exhaustive**, not merely "all we happened to see": there
is no battery, idle, playback, or lens event.

---

## 8. Photos: browsing, previews, downloads

### `/v1/photos` does **not** hang

Early in this project, `GET /v1/photos` appeared to hang indefinitely and put the
camera into an orange-LED "card read" state, and it was written off as unusable
with `/v1/photos/latest/info` used as a workaround. **That conclusion was
wrong**, and disproving it is one of the more satisfying results here.

A controlled test measured `/v1/photos` at two card populations, three runs each,
plus a run *immediately after a 56-second full-DNG download* (the most "busy" the
camera ever gets):

| Condition | Files on card | Response time |
|-----------|--------------|---------------|
| Idle | 8 | 159 / 76 / 42 ms |
| Immediately after 56 s download | 8 | **158 ms — 200 OK** |
| Idle | 80 | 338 / 377 / 320 ms |
| Immediately after 54 s download | 80 | **408 ms — 200 OK** |

It never hung. Listing time scales gently and roughly linearly with file count
(~4 ms/file) which makes sense, and the "busy" state doesn't break it. 
The original hang was almost certainly a keep-alive / connection-handling 
artifact of the very first probes, or a card in a genuinely bad state,
**not** a property of the endpoint. The endpoint is safe to use.

The response structure is a list of directories, each with its files:

```json
{"dirs": [{"name": "100_1507",
           "files": ["IMGP1971.DNG", "IMGP1972.DNG", "..."]}]}
```

### A hidden `?limit=N`

Sweeping query parameters turned up an **undocumented** one: `?limit=N` caps the
listing to the first *N* files, returning in a constant ~60 ms regardless of card
size. Every other parameter tried (`dir`, `offset`, `page`, `count`, `from`/`to`)
was ignored and returned the full listing. So there is a head-limit but **no
offset/cursor**, no true pagination. Given how fast a full listing is, that's a
non-issue. It also seems like the official app doesn't use `limit` at all.

### Previews and downloads

Downloads are `GET /v1/photos/{dir}/{file}` with an optional `?size=`:

| `size=` | Result | Notes |
|---------|--------|-------|
| `thumb` | `errCode 400` | **Not supported** on the K-S2 |
| `view` | ~54 KB JPEG | The only working preview. Used for gallery thumbnails |
| `full` | ~18 MB DNG | ~55 s over WiFi |
| *(omitted)* | ~18 MB DNG | Identical to `full` |

Per-image metadata comes from `GET /v1/photos/{dir}/{file}/info`, and it works
for **arbitrary** files, not just recent ones — so full card browsing is
possible. `DELETE` is **not** supported (raw HTML `400`); photos can't be removed
over WiFi, probably a safety feature.

Bulk download over WiFi is impractical (55 s × N). For getting many RAWs off the
camera, a USB / SD card reader remains the right tool. WiFi download is for
grabbing the occasional preview or single reference frame.

---

## 9. Live view

`GET /v1/liveview` is a standard MJPEG stream:

```
Content-Type: multipart/x-mixed-replace; boundary=--boundarydonotcross
```

Frames are ~23 KB baseline JPEG at 720×480. The per-frame part-headers carry
only `Content-type: image/jpg`, with no embedded focus/exposure/histogram
metadata (the K-1 reportedly embedded more). Any on-screen overlays must be
computed client-side or pulled from `/v1/status`. In a browser, pointing an
`<img>` element at the stream URL just works, merely viewing it kicks the camera's
mirror up to start streaming, and just terminating the JPEG stream viewing flips 
the mirror back down.

`POST /v1/liveview/zoom` exists for digital zoom/pan but is **gated**: an empty
body returns `200`, but any parameters return `412` unless live view is actively
streaming.

With a stream running, it was probed exhaustively, and the result is **no
observable effect over WiFi on the test rig**. Every candidate parameter returns
`errCode 200` (`zoom`, `level`, `scale`, `magnify`, `ratio`, `pos`, `x`/`y`,
`on`), including deliberately nonsensical values, and **the live frame never
changes for any of them**. The endpoint is reachable and stream-gated, and it
does not validate its parameters.

The cause is **unconfirmed**. One plausible explanation: the endpoint may depend
on a hardware capability the test lenses don't expose, both lenses used here
are kit zooms, or on a camera state that isn't reachable over the API. Either
way, no usable digital-zoom control was observed via the API on this setup.

---

## 10. How the official app works — and how this project improves on it

Capturing the Android **Image Sync** app's own traffic (see
[METHODOLOGY](METHODOLOGY.md)) settled the last open questions about the gallery,
and revealed how the reference client is built.

The gallery is unremarkable: `GET /v1/photos` to enumerate, then
`.../info` per file for metadata, then `?size=view` per file for the thumbnail
image, and a bare `GET` for full download. Exactly the endpoints documented
above, sadly no secret sauce.

The more interesting finding is the app's *architecture*. Of 691 captured
requests, **618 were `GET /v1/props`**, the app polls the monolithic state blob
many times per second which I imagine contributes to rapid battery drainage and 
it's poor performance. It opens the `/v1/changes` WebSocket once (and gets its `101`) 
but then leans almost entirely on brute-force polling.

`pyks2` deliberately does better:

- **Event-driven, not poll-storming**: it uses `/v1/changes` and re-fetches only
  the group that changed.
- **Decomposed reads**: `/v1/{group}/{facet}` instead of the whole `props` blob.
- **Correct `errCode` handling**, dynamic list re-fetching, and graceful handling
  of the SD-door disconnect.
- **`?limit=N`** for fast initial gallery loads.

The result is a lighter, more responsive client than the vendor's own, built
entirely on a surface that had no public documentation, just some probing fun!

---

*Next: [METHODOLOGY](METHODOLOGY.md) — how all of this was probed, including the
false trails and the traffic-capture that cracked the gallery.*
