"""A ``ks2_simulator`` pytest fixture, registered via a ``pytest11`` entry point
so it is available as soon as ``pyks2[testing]`` is installed — no
``pytest_plugins`` wiring needed downstream.

    def test_capture(ks2_simulator):
        cam = ks2_simulator.client()        # a real K_S2_WiFi
        info = cam.capture(af="off")
        assert info.path

The fixture yields a :class:`~pyks2.testing.simulator.SimulatorServer` bound to
an **ephemeral port**, so parallel runs don't collide. Useful attributes:
``base_url``, ``host_port``, ``port``, ``client()``, and ``simulator`` for
asserting on emitted events.

Function-scoped on purpose: a capture mutates simulator state, so each test
gets a clean camera. Startup is a few milliseconds.

``ks2_simulator`` runs with latency switched **off** so suites stay quick. Use
``ks2_simulator_realistic`` when the timing itself is what you're testing —
timeout handling, or that you don't race a capture — since it reproduces the
camera's measured delays (a capture event ~2 s after the shoot, first live view
frame ~0.8 s while the mirror flips, ``/v1/photos`` scaling with file count).

The simulator is imported lazily inside the fixtures, so merely having pyks2
installed without the extra can never break an unrelated pytest run.
"""

from __future__ import annotations

from typing import Iterator

import pytest


@pytest.fixture
def ks2_simulator() -> Iterator["object"]:
    """A K-S2 simulator on an ephemeral loopback port, with no added latency."""
    from .simulator import FAST, SimulatorServer

    server = SimulatorServer(timing=FAST)
    server.start()
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture
def ks2_simulator_realistic() -> Iterator["object"]:
    """As ``ks2_simulator``, but reproducing the camera's measured latencies.

    Slower by design — a capture takes ~2 s to report, as on the real body.
    """
    from .simulator import SimulatorServer, Timing

    server = SimulatorServer(timing=Timing())
    server.start()
    try:
        yield server
    finally:
        server.stop()
