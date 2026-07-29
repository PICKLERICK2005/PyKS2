"""Test helpers for pyks2 and for libraries built on it.

This is **shipped public surface**, not internal test scaffolding: downstream
projects import it to run their integration tests against a faithful fake
camera instead of mocking pyks2 out. Treat its API as stable.

    pip install pyks2[testing]

The centrepiece is a protocol-level simulator replaying real captured K-S2
wire data (see :mod:`pyks2.testing.simulator`):

    >>> from pyks2.testing import SimulatorServer
    >>> with SimulatorServer() as server:
    ...     cam = server.client()          # a real K_S2_WiFi
    ...     cam.ping()
    True

Or from a terminal::

    python -m pyks2.testing.simulator --port 8080

A ``ks2_simulator`` pytest fixture is registered automatically when this extra
is installed; see :mod:`pyks2.testing.pytest_plugin`.

Importing this package does not import the extra's dependencies, so
``import pyks2`` stays clean on a bare install. Only building or running the
simulator raises, and it names the extra when it does.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "CameraSimulator",
    "SimulatorServer",
    "create_app",
    "run_simulator",
    "MJPEG_CONTENT_TYPE",
]

# Re-exported lazily (PEP 562) rather than imported up front, so that
# `python -m pyks2.testing.simulator` doesn't execute the simulator module twice
# — once as `pyks2.testing.simulator` via this package, then again as
# `__main__` — which runpy warns about and which would give the two copies
# separate state. It also keeps `import pyks2.testing` genuinely cheap.
_LAZY = dict.fromkeys(__all__, "simulator")


def __getattr__(name: str) -> Any:
    module = _LAZY.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(f".{module}", __name__), name)


def __dir__() -> list:
    return sorted(__all__)
