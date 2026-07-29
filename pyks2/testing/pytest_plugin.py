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

The simulator is imported lazily inside the fixture, so merely having pyks2
installed without the extra can never break an unrelated pytest run.
"""

from __future__ import annotations

from typing import Iterator

import pytest


@pytest.fixture
def ks2_simulator() -> Iterator["object"]:
    """A running K-S2 simulator on an ephemeral loopback port."""
    from .simulator import SimulatorServer

    server = SimulatorServer()
    server.start()
    try:
        yield server
    finally:
        server.stop()
