from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import NOW, FakeAuth, FixedClock, fx

from kalshi_edge.config import ExchangeConfig
from kalshi_edge.domain import Mode, OrderIntent, OrderState, Outcome, StrategyId, TimeInForce
from kalshi_edge.exchange.hosts import PROD_REST_HOST
from kalshi_edge.exchange.venues import (
    CredentialsMissing,
    DemoExecutionVenue,
    LiveExecutionVenue,
    VenueConfigError,
    build_create_order_body,
)
from kalshi_edge.exchange.ws import BookBuilder, SequenceGap, apply_delta
from kalshi_edge.execution.errors import AmbiguousSubmission, LiveTradingDisabled, OrderRejected
from kalshi_edge.interfaces import ExecutionVenue

D = Decimal
TK = "KXHIGHNY-26OCT12-T75"


def intent(outcome: Outcome = Outcome.YES, mode: Mode = Mode.DEMO, qty: str = "4",
           price: str = "0.8300", tif: TimeInForce = TimeInForce.FILL_OR_KILL) -> OrderIntent:
    return OrderIntent("cid-1", "dec-1", mode, StrategyId.POSITIVE_EV_80, TK, "EV", outcome,
                       D(qty), D(price), tif, D("4"), "h", "v", NOW)


def demo_env(pem: Path) -> dict[str, str]:
    return {"KALSHI_DEMO_KEY_ID": "kid", "KALSHI_DEMO_PRIVATE_KEY_PATH": str(pem)}


def demo(pem: Path, handler: Any) -> DemoExecutionVenue:
    return DemoExecutionVenue(ExchangeConfig(environment="demo"), FixedClock(),
                              httpx.MockTransport(handler), env=demo_env(pem),
                              sleep=lambda s: None)


def test_body_mapping_yes_and_no() -> None:
    y = build_create_order_body(intent(Outcome.YES))
    assert (y["side"], y["price"], y["count"]) == ("bid", "0.8300", "4.00")
    n = build_create_order_body(intent(Outcome.NO, price="0.8200", qty="2.5"))
    assert (n["side"], n["price"], n["count"]) == ("ask", "0.1800", "2.50")
    assert y["time_in_force"] == "fill_or_kill" and y["client_order_id"] == "cid-1"
    with pytest.raises(ValueError):
        build_create_order_body(intent(price="0.83001"))
    with pytest.raises(ValueError):
        build_create_order_body(intent(qty="1.005"))


def test_demo_submit_request_and_response(pem_path: Path) -> None:
    seen: list[httpx.Request] = []

    def h(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json=fx("create_order_response.json"))

    v = demo(pem_path, h)
    assert isinstance(v, ExecutionVenue)
    st = v.submit(intent())
    r = seen[0]
    assert r.method == "POST" and r.url.host == "demo-api.kalshi.co"
    assert r.url.path == "/trade-api/v2/portfolio/events/orders"
    assert json.loads(r.content)["side"] == "bid"
    assert r.headers["KALSHI-ACCESS-KEY"] == "kid" and r.headers["KALSHI-ACCESS-SIGNATURE"]
    assert st.state is OrderState.FILLED and st.exchange_order_id == "ord-123"
    assert st.filled_quantity == D("4.00")


def test_demo_submit_timeout_is_ambiguous_and_not_retried(pem_path: Path) -> None:
    n = []

    def h(req: httpx.Request) -> httpx.Response:
        n.append(1)
        raise httpx.ReadTimeout("x")

    with pytest.raises(AmbiguousSubmission):
        demo(pem_path, h).submit(intent())
    assert len(n) == 1


def test_demo_rejection_and_unfilled_fok(pem_path: Path) -> None:
    with pytest.raises(OrderRejected):
        demo(pem_path, lambda r: httpx.Response(400, json={"error": "bad"})).submit(intent())
    body = {"order_id": "o", "client_order_id": "cid-1", "fill_count": "0.00",
            "remaining_count": "0.00", "ts_ms": 1791800000000}
    st = demo(pem_path, lambda r: httpx.Response(200, json=body)).submit(intent())
    assert st.state is OrderState.CANCELED and st.remaining_quantity == 0


def test_demo_get_order_by_client_id_and_cancel(pem_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def h(req: httpx.Request) -> httpx.Response:
        calls.append((req.method, req.url.path))
        if req.method == "DELETE":
            return httpx.Response(200, json={})
        cur = req.url.params.get("cursor", "")
        return httpx.Response(200, json=fx("orders_page2.json" if cur else "orders_page1.json"))

    v = demo(pem_path, h)
    o = v.get_order("cid-1")
    assert o is not None and o.exchange_order_id == "o-1" and o.outcome is Outcome.NO
    assert v.get_order("missing") is None
    v.cancel("cid-1")
    assert ("DELETE", "/trade-api/v2/portfolio/events/orders/o-1") in calls


def test_demo_account_and_fills(pem_path: Path) -> None:
    def h(req: httpx.Request) -> httpx.Response:
        p = req.url.path
        if p.endswith("/balance"):
            return httpx.Response(200, json=fx("balance.json"))
        if p.endswith("/positions"):
            return httpx.Response(200, json=fx("positions.json"))
        if p.endswith("/fills"):
            return httpx.Response(200, json=fx("fills.json"))
        return httpx.Response(200, json=fx("orders_page2.json"))

    v = demo(pem_path, h)
    acct = v.get_account()
    assert acct.cash == D("1000.50") and len(acct.positions) == 1 and len(acct.open_orders) == 1
    assert len(v.get_fills(NOW)) == 1


def test_demo_refuses_wrong_env_host_and_missing_creds(pem_path: Path) -> None:
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={}))
    with pytest.raises(VenueConfigError):
        DemoExecutionVenue(ExchangeConfig(environment="prod_public"), FixedClock(), t,
                           env=demo_env(pem_path))
    with pytest.raises(VenueConfigError):
        DemoExecutionVenue(ExchangeConfig(environment="demo"), FixedClock(), t,
                           env=demo_env(pem_path),
                           base_url="https://api.elections.kalshi.com/trade-api/v2")
    with pytest.raises(CredentialsMissing):
        DemoExecutionVenue(ExchangeConfig(environment="demo"), FixedClock(), t, env={})


def live(pem_path: Path, auth: FakeAuth | None, handler: Any,
         clock: FixedClock | None = None) -> LiveExecutionVenue:
    from kalshi_edge.exchange.auth import RsaPssSigner
    return LiveExecutionVenue(ExchangeConfig(environment="prod"), clock or FixedClock(), auth,
                              httpx.MockTransport(handler),
                              signer=RsaPssSigner.from_pem_path("k", pem_path),
                              sleep=lambda s: None)


def test_live_deny_by_default(pem_path: Path) -> None:
    n = []
    h = lambda r: (n.append(1), httpx.Response(200, json={}))[1]  # noqa: E731
    with pytest.raises(LiveTradingDisabled):
        live(pem_path, None, h)
    with pytest.raises(LiveTradingDisabled):
        live(pem_path, FakeAuth(PROD_REST_HOST, valid=False), h)
    with pytest.raises(LiveTradingDisabled):
        live(pem_path, FakeAuth("demo-api.kalshi.co"), h)
    assert n == []


def test_live_rechecks_per_call(pem_path: Path) -> None:
    auth = FakeAuth(PROD_REST_HOST)
    n = []

    def h(req: httpx.Request) -> httpx.Response:
        n.append(1)
        return httpx.Response(200, json=fx("create_order_response.json"))

    v = live(pem_path, auth, h)
    assert v.submit(intent(mode=Mode.LIVE)).state is OrderState.FILLED
    auth.valid = False  # expires after construction
    with pytest.raises(LiveTradingDisabled):
        v.submit(intent(mode=Mode.LIVE))
    with pytest.raises(LiveTradingDisabled):
        v.cancel("cid-1")
    with pytest.raises(LiveTradingDisabled):  # guard also blocks reads under /portfolio
        v.get_account()
    assert len(n) == 1


# ------------------------------------------------------------------ websocket

def snap() -> dict[str, Any]:
    return fx("ws_snapshot.json")


def delta(seq: int, price: str, d: str, side: str = "yes") -> dict[str, Any]:
    return {"type": "orderbook_delta", "sid": 1, "seq": seq,
            "msg": {"market_ticker": TK, "price_dollars": price, "delta_fp": d, "side": side}}


def test_apply_delta_pure() -> None:
    lv = {D("0.5"): D("3")}
    assert apply_delta(lv, D("0.5"), D("-3")) == {}
    assert apply_delta(lv, D("0.6"), D("1.5"))[D("0.6")] == D("1.5") and lv == {D("0.5"): D("3")}
    with pytest.raises(Exception, match="negative"):
        apply_delta(lv, D("0.5"), D("-4"))


def test_book_builder_snapshot_delta_and_gap() -> None:
    b = BookBuilder(TK)
    assert b.to_orderbook() is None
    b.handle(snap(), NOW)
    b.handle(delta(2, "0.8300", "5.00"), NOW + timedelta(seconds=1))
    b.handle(delta(3, "0.8200", "-13.00"), NOW + timedelta(seconds=2))
    book = b.to_orderbook()
    assert book is not None and book.seq == 3
    assert [lv.price for lv in book.yes_bids] == [D("0.8100"), D("0.8300")]
    with pytest.raises(SequenceGap):
        b.handle(delta(5, "0.8300", "1.00"), NOW)
    assert not b.valid and b.needs_resnapshot and b.to_orderbook() is None
    b.handle(snap(), NOW)  # resnapshot restores
    assert b.valid


def test_book_builder_bad_delta_invalidates() -> None:
    b = BookBuilder(TK)
    b.handle(snap(), NOW)
    with pytest.raises(Exception, match="negative"):
        b.handle(delta(2, "0.8100", "-99.00"), NOW)
    assert not b.valid


def test_stream_resubscribes_on_gap() -> None:
    import asyncio
    from contextlib import asynccontextmanager

    sessions = [
        [json.dumps(snap()), json.dumps(delta(2, "0.8300", "1.00")),
         json.dumps(delta(4, "0.8300", "1.00"))],  # gap
        [json.dumps(snap())],
    ]
    sent: list[str] = []
    books: list[Any] = []

    class Conn:
        def __init__(self, msgs: list[str]) -> None:
            self.msgs = msgs

        async def send(self, data: str) -> None:
            sent.append(data)

        def __aiter__(self) -> Any:
            async def gen() -> Any:
                for m in self.msgs:
                    yield m
            return gen()

    @asynccontextmanager
    async def connect() -> Any:
        yield Conn(sessions.pop(0))

    from kalshi_edge.exchange.ws import run_orderbook_stream
    asyncio.run(run_orderbook_stream(connect, TK, books.append, clock=lambda: NOW))
    assert len(sent) == 2 and all("orderbook_delta" in s for s in sent)
    assert books[2] is None and books[3] is not None and books[-1] is None
