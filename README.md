# pyks2

[![tests](https://github.com/PICKLERICK2005/pyks2/actions/workflows/test.yml/badge.svg)](https://github.com/PICKLERICK2005/pyks2/actions/workflows/test.yml)
[![python](https://img.shields.io/badge/python-3.9%2B-blue)](https://www.python.org/)
[![license](https://img.shields.io/badge/license-MIT-green)](https://github.com/PICKLERICK2005/pyks2/blob/main/LICENSE)

A Python library and CLI for controlling the **Pentax K-S2** over its
built-in WiFi, built on a complete, hardware-verified reverse-engineering of
the camera's undocumented HTTP API. (A web GUI is planned; see below.)

The K-S2 has a WiFi remote-control API, but Pentax never documented it. This
project maps the **entire surface** of the K-S2's API against a physical camera,
writes it up as a proper dissection, and ships a clean client that is lighter
and more capable than the vendor's own Image Sync app.

> **Two things make this more than "another camera library":**
> 1. A full [protocol dissection](https://github.com/PICKLERICK2005/pyks2/blob/main/docs/PROTOCOL.md) and
>    [reverse-engineering methodology](https://github.com/PICKLERICK2005/pyks2/blob/main/docs/METHODOLOGY.md), every endpoint,
>    every quirk, every hardware limitation, with the raw captures to prove it.
> 2. A design that **beats the official app**: event-driven via the camera's
>    WebSocket instead of the poll-storm Image Sync uses (~90% of its total
>    requests were just polling one endpoint while interacting with app actively).

---

## What's here

```
pyks2/          the library (camera-only HTTP client, typed models, WS events)
  ├─ client.py       K_S2_WiFi — the API client
  ├─ models.py       typed response models (defensive parsing)
  ├─ events.py       /v1/changes WebSocket client (stdlib, zero-dep)
  ├─ constants.py    endpoints + capability enums
  ├─ errors.py       typed exceptions (errCode-aware)
  └─ cli.py          the command-line interface
docs/           the reverse-engineering write-up (GitHub Pages source)
  ├─ PROTOCOL.md     the complete API dissection
  └─ METHODOLOGY.md  how it was probed (approach, false trails, traffic capture)
examples/       real captured JSON responses + the machine-readable API reference
```

---

## Quick start

```bash
pip install pyks2            # or: pip install -e .  from a clone
```

Join the camera's WiFi (`PENTAX_XXXXXX`), then:

```python
from pyks2 import K_S2_WiFi

cam = K_S2_WiFi()            # defaults to 192.168.0.1
assert cam.ping()

# take one photo safely: records the baseline, fires, waits for the NEW file,
# and downloads it all in one call (af="off" is MF-safe: always releases)
info = cam.capture(af="off", download_to="shot.dng")
print("captured:", info.path)

# change settings (validated by the camera; illegal values raise)
cam.set_camera_params(av="8.0", sv="400")

# browse the card
for photo in cam.list_photos():
    print(photo.path)
```

Event-driven, no polling:

```python
with cam.events() as ev:              # /v1/changes WebSocket
    for change in ev:
        if change.is_storage:         # a frame just landed
            print("captured:", cam.latest_info().path)
        elif change.is_camera:        # a setting changed on the body
            print("settings:", cam.get_camera_params())
```

Live view (MJPEG):

```python
for jpeg in cam.iter_liveview_frames(max_frames=1):
    open("frame.jpg", "wb").write(jpeg)
```

---

## CLI

Everything the library does, from a terminal:

```bash
pyks2 ping
pyks2 info                          # model, firmware, battery, storage
pyks2 shoot --af off --wait --download shot.dng
pyks2 settings                      # show current settings
pyks2 settings av=8.0 sv=400        # set them
pyks2 lists                         # capability lists (dropdown sources)
pyks2 browse --limit 20             # list photos
pyks2 download 100_1507/IMGP1974.DNG --size view -o preview.jpg
pyks2 liveview -o frame.jpg
pyks2 watch --resolve               # stream camera events live
```

---

## Web GUI

A browser-based control panel is planned, full Image-Sync parity plus
extensions (live view, remote capture, touch-to-focus, a settings panel driven
by the camera's own capability lists, an idle-safe gallery, and event-driven
updates), served by a thin local backend that reuses this library so it runs on
any OS in any browser. *Not yet included in this release, the library and CLI
are the shipping surfaces.*

---

## The reverse engineering

The heart of this project is the write-up. Highlights:

- **The full API is 40 endpoints**, organised as five read *groups*
  (`constants`/`params`/`variables`/`status`/`props`) × four *subsystems*
  (`camera`/`lens`/`liveview`/`device`), plus capture/focus/photo/liveview
  actions and a WebSocket.
- **Two protocol laws** every client must respect: the real status is in the
  body's `errCode` (not the HTTP status), and datetime/numeric formats are
  inconsistent across endpoints.
- **Hardware interlocks** mapped and explained: the AF/MF lever, mode dial, and
  movie mode are physical-only; movie mode *disables WiFi entirely*; opening the
  SD-card door kills the connection; and the camera's WiFi access point uses
  client isolation (which shaped how the official app's traffic had to be
  captured).
- **How Image Sync actually works**, captured off the wire — and why this
  client's event-driven design is better.

Start with **[docs/PROTOCOL.md](https://github.com/PICKLERICK2005/pyks2/blob/main/docs/PROTOCOL.md)**, then
**[docs/METHODOLOGY.md](https://github.com/PICKLERICK2005/pyks2/blob/main/docs/METHODOLOGY.md)**. The raw evidence is in
**[examples/](https://github.com/PICKLERICK2005/pyks2/tree/main/examples)** and the machine-readable spec is
**[examples/API_REFERENCE.json](https://github.com/PICKLERICK2005/pyks2/blob/main/examples/API_REFERENCE.json)**.

---

## Development & tests

```bash
git clone https://github.com/PICKLERICK2005/pyks2.git
cd pyks2
pip install -e ".[dev]"     # installs pytest, mypy, ruff
pytest -q                    # 37 tests, no camera required
```

The test suite runs entirely against captured fixtures (`examples/*.json`) via a
mock camera in `tests/conftest.py`, so it needs no hardware. Tests also serve as
executable documentation of the API's behaviour — including the trickier
findings (async capture, the Bulb correction, dynamic capability lists).

## Compatibility

Verified against a **Pentax K-S2, firmware 01.10**. Other Pentax bodies (K-1,
KP, K-70, K-3…) share much of this API family but differ in specifics. Running
the probing approach in [docs/METHODOLOGY.md](https://github.com/PICKLERICK2005/pyks2/blob/main/docs/METHODOLOGY.md) on another
body and sending the diffs is the most useful contribution you could make!

## License

MIT — see [LICENSE](https://github.com/PICKLERICK2005/pyks2/blob/main/LICENSE).

## Acknowledgements

The 2016 K-1 WiFi analysis on the antiguru wiki was a useful starting point for
hypotheses. Everything here was independently verified against a physical K-S2.
This project inspects only the camera's own network behaviour and my own hardware;
it contains no vendor code or any unmentioned references.
