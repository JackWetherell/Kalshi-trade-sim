"""Kalshi host constants and the production-order transport guard.

Fail-closed rule: any host that is not a known *demo* host is treated as production.
Against production, only ``GET`` requests to non-``/portfolio`` paths are allowed unless the
guard was constructed with a currently valid :class:`LiveAuthorization` for that host.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol, runtime_checkable
from urllib.parse import urlsplit

import httpx

from kalshi_edge.execution.errors import LiveTradingDisabled

PROD_REST_HOST = "api.elections.kalshi.com"
PROD_REST_ALT_HOST = "external-api.kalshi.com"  # newer recommended host per some sources
DEMO_REST_HOST = "demo-api.kalshi.co"

PROD_REST_BASE = f"https://{PROD_REST_HOST}/trade-api/v2"
DEMO_REST_BASE = f"https://{DEMO_REST_HOST}/trade-api/v2"
PROD_WS_URL = f"wss://{PROD_REST_HOST}/trade-api/ws/v2"
DEMO_WS_URL = f"wss://{DEMO_REST_HOST}/trade-api/ws/v2"

DEMO_HOSTS: frozenset[str] = frozenset({DEMO_REST_HOST})
PROD_HOSTS: frozenset[str] = frozenset({PROD_REST_HOST, PROD_REST_ALT_HOST})

__all__ = ["LiveTradingDisabled"]  # re-export of execution.errors.LiveTradingDisabled


class ProductionOrderBlocked(LiveTradingDisabled):
    """Raised when a request would reach a production host without live authorization."""


@runtime_checkable
class LiveAuthorization(Protocol):
    """Issued by the runtime arming ceremony (runtime/arming.py). Never created by tests
    against a real production host."""

    host: str

    def is_valid(self, now: datetime) -> bool: ...


def host_of(url: str) -> str:
    host = urlsplit(url).hostname
    return (host or "").lower()


def is_demo_host(host: str) -> bool:
    return host.lower() in DEMO_HOSTS


def is_production_host(host: str) -> bool:
    """Everything that is not a known demo host counts as production (fail closed)."""
    return not is_demo_host(host)


class ProductionOrderGuard:
    """httpx request event hook. Use as ``httpx.Client(event_hooks={"request": [guard]})``."""

    def __init__(self, authorization: LiveAuthorization | None = None, *,
                 clock: object | None = None) -> None:
        self._authorization = authorization
        self._clock = clock  # callable returning aware datetime; None => wall clock

    def _now(self) -> datetime:
        if callable(self._clock):
            value = self._clock()
            if isinstance(value, datetime):
                return value
        return datetime.now(UTC)

    def check(self, method: str, host: str, path: str) -> None:
        host = host.lower()
        if is_demo_host(host):
            return
        is_read_public = method.upper() == "GET" and "/portfolio" not in path.lower()
        if is_read_public:
            return
        auth = self._authorization
        if auth is None:
            raise ProductionOrderBlocked(
                f"blocked {method.upper()} {path} to production host {host}: no live authorization")
        if auth.host.lower() != host or not auth.is_valid(self._now()):
            raise ProductionOrderBlocked(
                f"blocked {method.upper()} {path} to {host}: live authorization invalid/expired")

    def __call__(self, request: httpx.Request) -> None:
        self.check(request.method, request.url.host, request.url.path)

    def __repr__(self) -> str:
        return f"ProductionOrderGuard(authorized={self._authorization is not None})"
