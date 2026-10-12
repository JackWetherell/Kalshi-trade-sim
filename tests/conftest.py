"""Global test guards: no test may open a real network connection."""

from __future__ import annotations

import socket
from typing import Any

import pytest


class NetworkAccessError(RuntimeError):
    """Raised when a test tries to open a real socket connection."""


def _blocked(*args: Any, **kwargs: Any) -> None:
    raise NetworkAccessError(f"outbound network blocked in tests: connect{args[1:]}")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly on any attempt to connect a socket (AF_INET/AF_INET6/AF_UNIX alike)."""
    monkeypatch.setattr(socket.socket, "connect", _blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", _blocked)
    monkeypatch.setattr(socket, "create_connection", _blocked)
    monkeypatch.setattr(socket, "getaddrinfo", _blocked)
