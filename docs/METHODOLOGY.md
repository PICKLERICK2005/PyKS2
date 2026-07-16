---
title: Methodology
nav_order: 3
---

# How this was reverse-engineered

The [protocol dissection](PROTOCOL.md) presents the finished map. This document
is about *how* the map was made. The approach, the tools, the false trails, and
the traffic capture to find out how the official Image Sync app works. If you 
want to replicate this on your own non-KS2 Pentax body, start here.

There was no documentation to work from. Everything below is the process of
turning a black-box camera into a fully characterised API.

---

## Principle: confirmed, not inferred

The guiding rule throughout: **a behaviour isn't "known" until the hardware
demonstrates it.** The official Pentax Image Sync android app, forum posts, and a 
partial 2016 write-up of the related K-1 on the pentax forums were useful for coming up with 
hypotheses, but every claim in the final map rests on a response captured from the actual camera body. 
Several "facts" inherited from prior assumptions turned out to be wrong (see the `/v1/photos`
myth below), which is exactly why the this rule and methedology mattered most throughout
this project. The KS2 being very different from it's predecessors (post-ricoh acquisition)
is the main reason why this whole project was initiated, not to mention the fact that the 
already-existing PKTRIGGERCORD does not support the KS2.

---

## Stage 1: find the surface

The break came from `GET /v1/apis`, which the camera answers with a list of all
its own endpoint templates. That turned an open-ended search into a finite
checklist of ~40 endpoints to characterise.

From there, the read endpoints were easy, and pleasantly so! because the
camera serves plain JSON that a browser renders directly (all hail firefox). 
Navigating to `http://192.168.0.1/v1/props`, `/v1/constants/camera`, and so on in 
Firefox showed each response formatted and readable, no tooling required. Hitting 
each `/v1/{group}/{facet}` combination and diffing the results is where the
five-groups-×-four-facets structure became clear.

## Stage 2: an automated scrubber

Hitting 40 endpoints by hand doesn't scale and isn't fun, so the
probing was scripted. A single sequenced scrubber:

- fired every endpoint in a defined order,
- captured status, headers, timing, and body for each,
- wrote every response to disk (the curated results became
  [`examples/`](../examples/)),
- snapshotted the camera's settings before write-tests and restored them after
  (so probing never left the camera in a weird state),
- and paused at the handful of probes that need physcial changes to the camera body or observations.

That last point became a core technique, using claude's help to make the scrubber script and instead
focusing on physically manipulating the camera when prompted.

The main methodology for these automated scripts is: human drives the hardware; 
the script reads and correlates and logs any changes.**

This is how the event vocabulary was mapped. With the `/v1/changes` WebSocket
open, the script prompted through a scripted sequence of physical actions: "turn
the mode dial," "half-press the shutter," "flip the AF/MF lever," "open the SD
door" etc... and logged which `changed` events each action produced.

---

## Stage 5: capturing traffic from Pentax's Image Sync app on android

One question resisted every direct probe: how does the official app populate its
image gallery grid efficiently given that `?size=thumb` returns `400`? 
The only way to know for certain was to watch the app's own traffic.
Because the camera speaks plain HTTP and does not verify it's being controlled by the 
app whatsoever, no TLS interception was needed, only running an android phone's traffic through
a machine running mitmproxy was needed.

Although I was able to connect more than one device to the camera's hotspot, 
**The camera's WiFi access point uses client isolation.** With the phone and a
PC both joined to the camera's network, so they cannot see each other, every
attempt to route the phone's traffic through a proxy on the PC timed out. 
The PC could reach the camera (`192.168.0.1`) but not the phone on the same network. 
Device-to-device traffic is blocked at the AP.

The fix was to change the topology so the phone→PC link doesn't go through the
camera at all:

```
Phone ──(PC's own hotspot)──> Windows Computer ──(camera WiFi)──> Camera
                                    │
                              proxy sits here
```

The PC hosts a WiFi hotspot, the phone joins *that* (no isolation on the
PC's own network), and the PC bridges to the camera over its separate
WiFi connection with the proxy in the middle. Right away, even before setting up
the proxy, just by having the android device connected to the PC's hotspot which was
in turn connected to the KS2's hotspot, the Image Sync app was able to control the camera
just fine.

With the phone's proxy pointed at the PC, the app's requests were thoroughly recorded and
used to verify earlier findings. The gallery turned out to use exactly the documented endpoints 
(`/v1/photos` →per-file `/info` → `?size=view` thumbnails), and the capture also revealed the
app's poll-heavy architecture (618 of 691 requests were `/v1/props` over a couple minutes).
No secret endpoints, just confirmation, plus the architectural insight that `pyks2` could
do better with the WebSocket due to the native app's poor performance.

> This technique was the least intrusive and didn't decompile or direclty modify any vendor software.

---

## Replicating on your own body

1. Enable the camera's WiFi and join its network (`PENTAX_XXXXXX`).
2. Confirm reachability by opening `http://192.168.0.1/v1/ping` in a browser
   the camera serves readable JSON, so most reads can be explored this way.
3. Walk the `/v1/{group}/{facet}` endpoints and the `/v1/apis` list; capture
   each response. Scripting this (fire in sequence, save body + headers +
   timing, snapshot/restore settings around any write-tests, and pause for
   operator observation on the physical probes) makes it reproducible on different 
   bodies or firmware versions.
4. Compare your captures against [`examples/`](../examples/). Different bodies
   (K-1, KP, K-70) share this API family and will most likely differ in multiple ways
   contributions documenting those differences are more than welcome!

---

*Back to the [protocol dissection](PROTOCOL.md), or on to the library and CLI in
the [README](../README.md).*
