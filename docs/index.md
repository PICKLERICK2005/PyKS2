---
title: Home
nav_order: 1
---

# pyks2: the open source Pentax K-S2 WiFi API

The Pentax K-S2 has an undocumented WiFi remote-control API. This project maps
its **entire surface** against a physical camera and ships a clean Python
client and CLI built on the findings.

- **[Protocol dissection](PROTOCOL.md)** is every endpoint, every quirk, with
  captured evidence.
- **[Methodology](METHODOLOGY.md)** is how it was probed, including the false
  trails and the PENTAX Image Sync app traffic capture that cracked the gallery grid view.

The raw captured responses are in
[`examples/`](https://github.com/PICKLERICK2005/pyks2/tree/main/examples), and the
machine-readable spec is
[`examples/API_REFERENCE.json`](https://github.com/PICKLERICK2005/pyks2/blob/main/examples/API_REFERENCE.json).

## Highlights

- 40 endpoints, cleanly structured as read-groups × subsystems + actions + a
  WebSocket event stream.
- Two protocol laws (errCode-in-body; inconsistent datetime formats).
- Every hardware interlock explained (AF/MF lever, movie mode disabling WiFi,
  SD-door killing the connection, AP client isolation).
- A capture of how the official Image Sync app works — and why this client's
  event-driven design beats it.
