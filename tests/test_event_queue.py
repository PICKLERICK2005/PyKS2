"""Lossless synchronous /v1/changes event consumption."""

from __future__ import annotations

import json

from pyks2.events import ChangesClient


def frame(changed: str) -> bytes:
    payload = json.dumps({"changed": changed}).encode()
    return bytes((0x81, len(payload))) + payload


class ChunkSocket:
    def __init__(self, chunks=()):
        self.chunks = list(chunks)
        self.recv_calls = 0
        self.timeout = None

    def settimeout(self, timeout):
        self.timeout = timeout

    def recv(self, _size):
        self.recv_calls += 1
        if self.chunks:
            return self.chunks.pop(0)
        return b""

    def close(self):
        pass


def client_with(chunks=(), buffered=b""):
    client = ChangesClient("camera")
    client._sock = ChunkSocket(chunks)
    client._buf = buffered
    return client


def test_next_event_keeps_excess_decoded_events_without_another_receive():
    client = client_with(buffered=frame("camera") + frame("storage"))

    assert client.next_event(timeout=0).changed == "camera"
    assert client.next_event(timeout=0).changed == "storage"
    assert client._sock.recv_calls == 0
    assert client.next_event(timeout=0) is None


def test_next_event_returns_five_coalesced_events_once_in_order():
    changed = ["camera", "storage", "camera", "storage", "storage"]
    client = client_with(buffered=b"".join(map(frame, changed)))

    returned = [client.next_event(timeout=0).changed for _ in changed]

    assert returned == changed
    assert len(client._pending) == 0


def test_next_event_preserves_partial_frames_across_network_reads():
    a, b, c = frame("camera"), frame("storage"), frame("storage")
    client = client_with([
        a[:5],
        a[5:] + b + c[:7],
        c[7:],
    ])

    assert client.next_event(timeout=1).changed == "camera"
    assert client.next_event(timeout=1).changed == "storage"
    assert client._sock.recv_calls == 2
    assert client.next_event(timeout=1).changed == "storage"
    assert client._sock.recv_calls == 3


def test_iterator_and_next_event_return_equivalent_order():
    changed = ["camera", "storage", "storage"]
    wire = b"".join(map(frame, changed))
    iterator_client = client_with([wire])
    next_client = client_with([wire])

    iterated = [event.changed for event in iterator_client]
    repeated = [next_client.next_event(timeout=1).changed for _ in changed]

    assert iterated == repeated == changed
