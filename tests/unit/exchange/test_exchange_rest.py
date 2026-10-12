from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import NOW, FixedClock, fx

from kalshi_edge.config import ExchangeConfig
from kalshi_edge.domain import Outcome
from kalshi_edge.exchange.historical import (
    RawPageStore,
    fetch_historical_candlesticks,
    fetch_historical_markets,
    get_cutoff,
)
from kalshi_edge.exchange.hosts import PROD_REST_BASE
from kalshi_edge.exchange.rest import (
    HttpStatusError,
    KalshiRestClient,
    PublicMarketData,
    TokenBucket,
)
from kalshi_edge.exchange.schema import SchemaError
from kalshi_edge.execution.errors import AmbiguousSubmission
from kalshi_edge.interfaces import MarketDataProvider


def client(handler: Any, sleeps: list[float] | None = None, **kw: Any) -> KalshiRestClient:
    s = sleeps if sleeps is not None else []
    return KalshiRestClient(PROD_REST_BASE, transport=httpx.MockTransport(handler),
                            sleep=s.append, rng=lambda: 1.0, **kw)


def test_429_backoff_then_success_honors_retry_after() -> None:
    calls = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        if len(calls) == 2:
            return httpx.Response(503)
        return httpx.Response(200, json={"ok": 1})

    sleeps: list[float] = []
    c = client(h, sleeps)
    assert c.get("/markets") == {"ok": 1}
    assert len(calls) == 3
    assert 7.0 in sleeps and len(sleeps) >= 2
    assert sleeps[-1] == 1.0  # base 0.5 * 2**1 with rng=1.0 -> full ceiling


def test_get_exhausts_retries() -> None:
    c = client(lambda r: httpx.Response(500), max_retries=2)
    with pytest.raises(HttpStatusError):
        c.get("/x")


def test_get_timeout_retried() -> None:
    n = []

    def h(req: httpx.Request) -> httpx.Response:
        n.append(1)
        if len(n) < 3:
            raise httpx.ReadTimeout("t")
        return httpx.Response(200, json={})

    client(h).get("/x")
    assert len(n) == 3


def test_post_never_retried_on_timeout_or_5xx() -> None:
    n = []

    def h(req: httpx.Request) -> httpx.Response:
        n.append(1)
        raise httpx.ReadTimeout("t")

    from kalshi_edge.exchange.hosts import DEMO_REST_BASE
    c = KalshiRestClient(DEMO_REST_BASE, transport=httpx.MockTransport(h), sleep=lambda s: None)
    with pytest.raises(AmbiguousSubmission):
        c.request_json("POST", "/portfolio/events/orders", json_body={"a": 1})
    assert len(n) == 1
    n.clear()
    c2 = KalshiRestClient(DEMO_REST_BASE, transport=httpx.MockTransport(
        lambda r: (n.append(1), httpx.Response(502))[1]), sleep=lambda s: None)
    with pytest.raises(AmbiguousSubmission):
        c2.request_json("POST", "/portfolio/events/orders", json_body={})
    assert len(n) == 1


def test_pagination_and_cursor_loop() -> None:
    pages = {"": fx("markets_page1.json"), "CUR2": fx("markets_page2.json")}
    seen: list[dict[str, str]] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(dict(req.url.params))
        return httpx.Response(200, json=pages[req.url.params.get("cursor", "")])

    items = list(client(h).paginate("/markets", "markets", {"status": "open"}, limit=2))
    assert len(items) == 4 and seen[1]["cursor"] == "CUR2" and seen[0]["limit"] == "2"
    loop = client(lambda r: httpx.Response(200, json={"markets": [], "cursor": "SAME"}))
    with pytest.raises(SchemaError, match="loop"):
        list(loop.paginate("/markets", "markets"))


def test_token_bucket_sleeps() -> None:
    t = [0.0]
    slept: list[float] = []
    b = TokenBucket(2.0, burst=1.0, clock=lambda: t[0], sleep=slept.append)
    b.acquire()
    b.acquire()
    assert slept == [0.5]


def public_handler(req: httpx.Request) -> httpx.Response:
    p = req.url.path
    assert req.method == "GET" and "authorization" not in req.headers
    assert not any(k.lower().startswith("kalshi-access") for k in req.headers)
    if "/NOPE" in p:
        return httpx.Response(404)
    if p.endswith("/orderbook"):
        return httpx.Response(200, json=fx("orderbook.json"))
    if p.startswith("/trade-api/v2/series/"):
        return httpx.Response(200, json=fx("series.json"))
    if p.endswith("/markets") and not req.url.params.get("cursor"):
        return httpx.Response(200, json=fx("markets_page1.json"))
    if p.endswith("/markets"):
        return httpx.Response(200, json=fx("markets_page2.json"))
    if p.endswith("/NOPE"):
        return httpx.Response(404)
    return httpx.Response(200, json=fx("market_binary_tapered.json"))


def test_public_market_data() -> None:
    md = PublicMarketData(ExchangeConfig(environment="prod_public"),
                          transport=httpx.MockTransport(public_handler), sleep=lambda s: None,
                          clock=FixedClock().now)
    assert isinstance(md, MarketDataProvider)
    markets = md.list_markets()
    assert [m.ticker for m in markets] == ["KXHIGHNY-26OCT12-T75", "KXFRAC-26OCT12-A"]
    assert any("binary" in r for _, r in md.skipped)
    book = md.get_orderbook("KXHIGHNY-26OCT12-T75")
    assert book is not None and book.best_ask(Outcome.YES) is not None and book.ts == NOW
    assert md.get_orderbook("NOPE") is None
    assert md.get_market("KXHIGHNY-26OCT12-T75") is not None


def test_public_cannot_post_to_prod() -> None:
    from kalshi_edge.exchange.hosts import ProductionOrderBlocked
    md = PublicMarketData(transport=httpx.MockTransport(public_handler), sleep=lambda s: None)
    with pytest.raises(ProductionOrderBlocked):
        md.client.request_json("POST", "/portfolio/events/orders", json_body={})


def test_historical_store(tmp_path: Path) -> None:
    pages = {"": fx("historical_markets_page1.json"), "H2": fx("historical_markets_page2.json")}

    def h(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/cutoff"):
            return httpx.Response(200, json=fx("historical_cutoff.json"))
        if p.endswith("/candlesticks"):
            return httpx.Response(200, json={"candlesticks": []})
        return httpx.Response(200, json=pages[req.url.params.get("cursor", "")])

    c = client(h)
    store = RawPageStore(tmp_path / "raw", clock=lambda: NOW)
    cut = get_cutoff(c, store)
    assert cut.market_settled_ts.year == 2026
    assert fetch_historical_markets(c, store) == 2
    fetch_historical_candlesticks(c, store, "T", start_ts=1, end_ts=2, period_interval=60)
    lines = [json.loads(x) for x in store.manifest_path.read_text().splitlines()]
    assert len(lines) == 4 and lines[-1]["proxy_only"] and not lines[0]["proxy_only"]
    assert all(len(x["sha256"]) == 64 for x in lines) and store.verify() == []
    (tmp_path / "raw" / lines[1]["file"]).write_bytes(b"tampered")
    assert store.verify()
    with pytest.raises(FileExistsError):
        # same counter+name collision cannot overwrite
        store._counter = 0
        store.write("/historical/cutoff", {}, (tmp_path / "raw" / lines[0]["file"]).read_bytes())
