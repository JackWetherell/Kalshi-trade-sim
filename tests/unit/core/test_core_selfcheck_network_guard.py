"""The autouse conftest fixture must make real connects fail."""

from __future__ import annotations

import socket

import httpx
import pytest
from tests.conftest import NetworkAccessError


def test_socket_connect_is_blocked() -> None:
    s = socket.socket()
    try:
        with pytest.raises(NetworkAccessError):
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()


def test_create_connection_and_dns_blocked() -> None:
    with pytest.raises(NetworkAccessError):
        socket.create_connection(("example.com", 443))
    with pytest.raises(NetworkAccessError):
        socket.getaddrinfo("api.elections.kalshi.com", 443)


def test_httpx_real_transport_cannot_reach_kalshi() -> None:
    with pytest.raises((NetworkAccessError, httpx.HTTPError)):
        httpx.get("https://api.elections.kalshi.com/trade-api/v2/exchange/status", timeout=1)
