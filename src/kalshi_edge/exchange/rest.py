"""Synchronous Kalshi REST client with rate limiting and safe retries.

* GET: retried on 429 / 5xx / timeouts / transport errors with exponential backoff, jitter and
  ``Retry-After`` honoring.
* POST / DELETE (anything mutating): NEVER retried. A timeout or 5xx raises
  :class:`AmbiguousSubmission`; callers must query the order by ``client_order_id`` first.
* Every request passes through a :class:`ProductionOrderGuard` event hook.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from kalshi_edge.config import ExchangeConfig
from kalshi_edge.domain import Market, OrderBook
from kalshi_edge.exchange.auth import RsaPssSigner
from kalshi_edge.exchange.hosts import PROD_REST_BASE, ProductionOrderGuard
from kalshi_edge.exchange.normalize import (
    SeriesInfo,
    is_supported_binary,
    normalize_market,
    normalize_orderbook,
    normalize_series,
)
from kalshi_edge.exchange.schema import SchemaError
from kalshi_edge.execution.errors import AmbiguousSubmission

MAX_RETRY_AFTER_SECONDS = 60.0


class KalshiError(RuntimeError):
    pass


class HttpStatusError(KalshiError):
    def __init__(self, status: int, method: str, path: str, body: str = "") -> None:
        super().__init__(f"{method} {path} -> HTTP {status}: {body[:200]}")
        self.status = status
        self.method = method
        self.path = path


class RateLimited(HttpStatusError):
    pass


class TransportFailure(KalshiError):
    """Request could not be (fully) sent / read; for GET after retries were exhausted."""


class TokenBucket:
    def __init__(self, rate_per_second: float, burst: float | None = None, *,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if rate_per_second <= 0:
            raise ValueError("rate must be positive")
        self.rate = rate_per_second
        self.capacity = burst if burst is not None else max(1.0, rate_per_second)
        self._tokens = self.capacity
        self._clock = clock
        self._sleep = sleep
        self._last = clock()

    def acquire(self, cost: float = 1.0) -> None:
        now = self._clock()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now
        if self._tokens < cost:
            wait = (cost - self._tokens) / self.rate
            self._sleep(wait)
            self._tokens = 0.0
            self._last = self._clock()
        else:
            self._tokens -= cost


class KalshiRestClient:
    def __init__(
        self,
        base_url: str,
        *,
        signer: RsaPssSigner | None = None,
        guard: ProductionOrderGuard | None = None,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 10.0,
        max_requests_per_second: float = 5.0,
        max_retries: int = 4,
        backoff_base: float = 0.5,
        backoff_cap: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,
        clock_ms: Callable[[], int] | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._prefix = urlsplit(self.base_url).path  # "/trade-api/v2"
        self._signer = signer
        self._guard = guard if guard is not None else ProductionOrderGuard(None)
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._sleep = sleep
        self._rng = rng
        self._clock_ms = clock_ms or (lambda: int(time.time() * 1000))
        self._bucket = TokenBucket(max_requests_per_second, sleep=sleep)
        self._http = httpx.Client(
            base_url=self.base_url + "/",
            timeout=timeout,
            transport=transport,
            event_hooks={"request": [self._guard]},
            follow_redirects=False,
        )

    def close(self) -> None:
        self._http.close()

    # -- core -----------------------------------------------------------------
    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        if retry_after:
            try:
                delay: float = float(retry_after)
            except ValueError:
                pass
            else:
                return min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS)
        ceiling: float = min(self._backoff_cap, self._backoff_base * (2 ** attempt))
        jitter: float = self._rng()
        return ceiling * (0.5 + jitter / 2)

    def send(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
             json_body: Mapping[str, Any] | None = None) -> httpx.Response:
        method = method.upper()
        mutating = method != "GET"
        rel = path.lstrip("/")
        attempts = 1 if mutating else self._max_retries + 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            self._bucket.acquire()
            headers: dict[str, str] = {}
            if self._signer is not None:
                headers = self._signer.headers(method, f"{self._prefix}/{rel}", self._clock_ms())
            try:
                resp = self._http.request(method, rel, params=dict(params) if params else None,
                                          json=dict(json_body) if json_body is not None else None,
                                          headers=headers)
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                if mutating:  # request never left the machine
                    raise TransportFailure(f"{method} {path}: connect failed") from exc
                last_exc = exc
            except httpx.TimeoutException as exc:
                if mutating:
                    raise AmbiguousSubmission(
                        f"{method} {path} timed out; outcome unknown, query before retrying"
                    ) from exc
                last_exc = exc
            except httpx.TransportError as exc:
                if mutating:
                    raise AmbiguousSubmission(
                        f"{method} {path} transport error {type(exc).__name__}; outcome unknown"
                    ) from exc
                last_exc = exc
            else:
                status = resp.status_code
                if 200 <= status < 300:
                    return resp
                if mutating:
                    if status == 429:
                        raise RateLimited(status, method, path, resp.text)
                    if status >= 500:
                        raise AmbiguousSubmission(
                            f"{method} {path} -> HTTP {status}; outcome unknown")
                    raise HttpStatusError(status, method, path, resp.text)
                if status == 429 or status >= 500:
                    last_exc = (RateLimited if status == 429 else HttpStatusError)(
                        status, method, path, resp.text)
                    if attempt + 1 < attempts:
                        self._sleep(self._backoff(attempt, resp.headers.get("Retry-After")))
                    continue
                raise HttpStatusError(status, method, path, resp.text)
            if attempt + 1 < attempts:
                self._sleep(self._backoff(attempt, None))
        if isinstance(last_exc, HttpStatusError):
            raise last_exc
        raise TransportFailure(f"{method} {path}: failed after {attempts} attempts") from last_exc

    def request_json(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
                     json_body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        resp = self.send(method, path, params=params, json_body=json_body)
        try:
            data = resp.json()
        except ValueError as exc:
            raise SchemaError(f"{method} {path}: response is not JSON") from exc
        if not isinstance(data, dict):
            raise SchemaError(f"{method} {path}: response is not an object")
        return data

    def get(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return self.request_json("GET", path, params=params)

    def paginate(self, path: str, items_key: str, params: Mapping[str, Any] | None = None, *,
                 limit: int = 200, max_pages: int = 1000) -> Iterator[dict[str, Any]]:
        """Yield items across cursor pages. Stops on empty cursor; refuses cursor loops."""
        for page in self.paginate_pages(path, params, limit=limit, max_pages=max_pages):
            items = page.get(items_key)
            if not isinstance(items, list):
                raise SchemaError(f"{path}: page missing list {items_key!r}")
            for item in items:
                if not isinstance(item, dict):
                    raise SchemaError(f"{path}: non-object item in {items_key!r}")
                yield item

    def paginate_pages(self, path: str, params: Mapping[str, Any] | None = None, *,
                       limit: int = 200, max_pages: int = 1000) -> Iterator[dict[str, Any]]:
        for resp in self.paginate_responses(path, params, limit=limit, max_pages=max_pages):
            data = resp.json()
            if not isinstance(data, dict):
                raise SchemaError(f"{path}: page is not an object")
            yield data

    def paginate_responses(self, path: str, params: Mapping[str, Any] | None = None, *,
                           limit: int = 200, max_pages: int = 1000) -> Iterator[httpx.Response]:
        query: dict[str, Any] = dict(params or {})
        query["limit"] = limit
        seen: set[str] = set()
        for _ in range(max_pages):
            resp = self.send("GET", path, params=query)
            yield resp
            try:
                cursor = resp.json().get("cursor") or ""
            except (ValueError, AttributeError):
                raise SchemaError(f"{path}: unparseable page") from None
            if not cursor:
                return
            if cursor in seen:
                raise SchemaError(f"{path}: cursor loop detected")
            seen.add(cursor)
            query["cursor"] = cursor
        raise KalshiError(f"{path}: exceeded max_pages={max_pages}")


class PublicMarketData:
    """Credential-free, GET-only market data (implements ``interfaces.MarketDataProvider``)."""

    def __init__(self, config: ExchangeConfig | None = None, *, base_url: str = PROD_REST_BASE,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] | None = None,
                 client: KalshiRestClient | None = None,
                 allow_fractional: bool = False) -> None:
        cfg = config or ExchangeConfig()
        self._cfg = cfg
        self._clock = clock or (lambda: datetime.now(UTC))
        self._allow_fractional = allow_fractional
        self._client = client or KalshiRestClient(
            base_url, signer=None, guard=ProductionOrderGuard(None), transport=transport,
            timeout=cfg.request_timeout_seconds,
            max_requests_per_second=cfg.max_requests_per_second, sleep=sleep)
        self._series_cache: dict[str, SeriesInfo | None] = {}
        self.skipped: list[tuple[str, str]] = []  # (ticker, reason) for excluded/malformed

    @property
    def client(self) -> KalshiRestClient:
        return self._client

    def get_series(self, series_ticker: str) -> SeriesInfo | None:
        if series_ticker not in self._series_cache:
            try:
                data = self._client.get(f"/series/{series_ticker}")
                body = data.get("series")
                self._series_cache[series_ticker] = (
                    normalize_series(body) if isinstance(body, dict) else None)
            except SchemaError as exc:
                self.skipped.append((series_ticker, f"series: {exc}"))
                self._series_cache[series_ticker] = None
            except HttpStatusError as exc:
                if exc.status != 404:
                    raise
                self._series_cache[series_ticker] = None
        return self._series_cache[series_ticker]

    def _normalize(self, payload: dict[str, Any], source: str) -> Market | None:
        ticker = str(payload.get("ticker", "?"))
        ok, reason = is_supported_binary(payload)
        if not ok:
            self.skipped.append((ticker, reason))
            return None
        event = str(payload.get("event_ticker", ""))
        series_ticker = str(payload.get("series_ticker") or event.split("-", 1)[0])
        series = self.get_series(series_ticker)
        if series is None:
            self.skipped.append((ticker, "unknown fees: series unavailable"))
            return None
        try:
            return normalize_market(payload, series=series, source=source,
                                    allow_fractional=self._allow_fractional)
        except SchemaError as exc:
            self.skipped.append((ticker, str(exc)))
            return None

    def list_markets(self) -> list[Market]:
        out: list[Market] = []
        allow = self._cfg.series_allowlist
        queries: list[dict[str, Any]] = (
            [{"status": "open", "series_ticker": s} for s in allow] if allow
            else [{"status": "open"}])
        for q in queries:
            for payload in self._client.paginate("/markets", "markets", q,
                                                 limit=self._cfg.markets_page_limit):
                m = self._normalize(payload, "GET /markets")
                if m is not None:
                    out.append(m)
        return out

    def get_market(self, ticker: str) -> Market | None:
        try:
            data = self._client.get(f"/markets/{ticker}")
        except HttpStatusError as exc:
            if exc.status == 404:
                return None
            raise
        body = data.get("market")
        if not isinstance(body, dict):
            raise SchemaError("get_market: missing 'market' object")
        return self._normalize(body, f"GET /markets/{ticker}")

    def get_orderbook(self, ticker: str) -> OrderBook | None:
        try:
            before = self._clock()
            data = self._client.get(f"/markets/{ticker}/orderbook")
        except HttpStatusError as exc:
            if exc.status == 404:
                return None
            raise
        received = self._clock()
        # The REST orderbook carries no server timestamp: we only know it is no older than
        # the request start. Use request start as the (conservative) authoritative time.
        return normalize_orderbook(data, ticker=ticker, ts=before, received_ts=received,
                                   source=f"GET /markets/{ticker}/orderbook")
