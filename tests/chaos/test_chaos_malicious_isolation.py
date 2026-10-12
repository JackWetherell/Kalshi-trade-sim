"""Malicious strategies, paper/prod isolation, live-venue refusals, REST failure behaviour."""

from __future__ import annotations

import ast
import dataclasses
import json
import os
from collections.abc import Callable, Sequence
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.support import (
    T0,
    TICKER,
    FixedEstimator,
    book,
    estimate,
    make_rig,
    market,
    proposal,
)

from kalshi_edge.config import AppConfig, ExchangeConfig
from kalshi_edge.data.memory import InMemoryMarketData
from kalshi_edge.domain import (
    Mode,
    OrderIntent,
    OrderState,
    Outcome,
    StrategyId,
    TimeInForce,
    TradeProposal,
)
from kalshi_edge.exchange.hosts import (
    DEMO_REST_BASE,
    PROD_REST_BASE,
    PROD_REST_HOST,
    ProductionOrderBlocked,
    ProductionOrderGuard,
)
from kalshi_edge.exchange.rest import (
    HttpStatusError,
    KalshiRestClient,
    RateLimited,
)
from kalshi_edge.exchange.venues import (
    CredentialsMissing,
    DemoExecutionVenue,
    LiveExecutionVenue,
    VenueConfigError,
)
from kalshi_edge.execution import (
    AmbiguousSubmission,
    ExecutionEngine,
    LiveTradingDisabled,
    OrderRejected,
    PaperVenue,
    Store,
)
from kalshi_edge.interfaces import DecisionContext
from kalshi_edge.monitoring.alerts import AlertSink
from kalshi_edge.risk import HardRiskManager, KillSwitch
from kalshi_edge.runtime.arming import ArmingToken
from kalshi_edge.runtime.clock import ReplayClock
from kalshi_edge.runtime.factory import build_runtime
from kalshi_edge.runtime.loop import TradingLoop

D = Decimal
ROOT = Path(__file__).resolve().parents[2] / "src" / "kalshi_edge"


# ====================================================== malicious / malformed strategies
class Scripted:
    """A 'strategy file' we do not trust: returns whatever proposals it is told to."""

    def __init__(self, make: Callable[[DecisionContext], Sequence[TradeProposal]],
                 sid: StrategyId = StrategyId.POSITIVE_EV_80) -> None:
        self.strategy_id = sid
        self._make = make

    def evaluate(self, ctx: DecisionContext) -> Sequence[TradeProposal]:
        return self._make(ctx)


def run_evil(tmp_path: Path, make: Callable[[DecisionContext], Sequence[TradeProposal]],
             sid: StrategyId = StrategyId.POSITIVE_EV_80, **kw: Any):
    rig = make_rig(tmp_path, strategy=Scripted(make, sid), **kw)
    rep = rig.loop.step()
    return rig, rep


def test_absurd_quantity_is_capped_to_one_percent_risk(tmp_path: Path) -> None:
    rig, rep = run_evil(tmp_path, lambda c: [proposal(qty="1000000000")])
    assert rep.submitted == 1
    spent = D("1009") - rig.venue.cash
    assert 0 < spent <= D("10.09")
    assert sum((p.quantity for p in rig.venue.get_account().positions), D(0)) <= 12


@pytest.mark.parametrize(("label", "kw", "reason"), [
    ("limit_1.00", dict(limit="1.00", expected="0.87"), "price_above_max"),
    ("limit_0.98", dict(limit="0.98", expected="0.87"), "price_above_max"),
    ("off_grid", dict(limit="0.8751", expected="0.87"), "limit_price_off_grid_or_unknown_grid"),
    ("negative_qty", dict(qty="-5"), "non_positive_quantity"),
    ("zero_qty", dict(qty="0"), "non_positive_quantity"),
    ("below_floor", dict(limit="0.60", expected="0.60"), "price_not_above_threshold"),
    ("limit_zero", dict(limit="0", expected="0.87"), "limit_price_off_grid_or_unknown_grid"),
])
def test_malformed_proposals_never_reach_the_venue(tmp_path: Path, label: str,
                                                   kw: dict[str, str], reason: str) -> None:
    rig, rep = run_evil(tmp_path, lambda c: [proposal(**kw)])
    assert rep.submitted == 0, label
    assert rep.rejections.get(reason, 0) >= 1, (label, rep.rejections)
    assert rig.venue.get_fills() == [] and rig.venue._orders == {}
    decisions = rig.store._conn.execute("SELECT approved, reasons FROM decisions").fetchall()
    assert decisions and all(r["approved"] == 0 for r in decisions)  # logged with reasons


def test_wrong_ticker_proposal_rejected(tmp_path: Path) -> None:
    ghost = market(ticker="GHOST-1")
    _rig, rep = run_evil(tmp_path, lambda c: [proposal(mkt=ghost)])
    assert rep.submitted == 0
    assert rep.rejections.get("missing_fresh_book_or_account", 0) == 1


def test_proposal_for_other_markets_book_is_rejected(tmp_path: Path) -> None:
    other = market(ticker="OTHER-1", event_ticker="E2")
    rig = make_rig(tmp_path, strategy=Scripted(lambda c: [proposal(mkt=other)]),
                   markets=[market(), other],
                   books=[book(), book(ticker="OTHER-1", yes_asks=[("0.55", "1000")],
                                       yes_bids=[("0.50", "10")])])
    rig.loop.step()
    # the 'OTHER-1' proposal claims 0.87 but its fresh book is 0.55 (below the floor)
    assert rig.venue.get_fills() == []


def test_wrong_side_estimate_and_fake_independence_rejected(tmp_path: Path) -> None:
    _rig, rep = run_evil(tmp_path, lambda c: [proposal(outcome=Outcome.NO, est=estimate())])
    assert rep.submitted == 0
    sub = tmp_path / "b"
    sub.mkdir()
    _rig2, rep2 = run_evil(sub, lambda c: [proposal(est=estimate(independent=False))])
    assert rep2.submitted == 0
    assert rep2.rejections.get("estimate_not_independent_or_not_calibrated", 0) >= 1


def test_flood_of_proposals_is_capped_by_aggregate_cluster_and_order_count(
        tmp_path: Path) -> None:
    rig, rep = run_evil(tmp_path, lambda c: [proposal(qty="1000") for _ in range(200)])
    assert rep.proposals == 200
    assert rep.submitted <= 10  # max_orders_per_day
    spent = D("1009") - rig.venue.cash
    assert spent <= D("1009") * D("0.05")  # correlated cluster (same event) hard cap
    assert rep.rejections  # the rest were refused with reasons


def test_strategy_a_b_d_proposals_in_live_loop_never_submit(tmp_path: Path) -> None:
    for sid in (StrategyId.NAIVE_80, StrategyId.SCALED_80, StrategyId.ABLATION):
        log: list[OrderIntent] = []
        evil = Scripted(lambda c, s=sid: [proposal(strategy=s)], sid)
        loop, _venue = live_loop(tmp_path / sid.value, evil, log)
        rep = loop.step()
        assert rep.submitted == 0 and log == [], sid
        assert rep.rejections.get("strategy_or_estimate_not_live_eligible", 0) >= 1


def test_strategy_that_raises_fails_closed_without_trading(tmp_path: Path) -> None:
    def boom(ctx: DecisionContext) -> list[TradeProposal]:
        raise RuntimeError("buggy generated strategy")

    rig = make_rig(tmp_path, strategy=Scripted(boom))
    rep = rig.loop.step()  # contained per market: recorded, nothing traded, loop survives
    assert rep.rejections.get("market_error:RuntimeError", 0) >= 1
    assert rep.submitted == 0
    assert rig.venue.get_fills() == [] and rig.venue._orders == {}
    # the heartbeat event is still written even though the step blew up
    assert any(r["kind"] == "step" for r in rig.store.recent_events())


def test_kill_switch_beats_malicious_strategy(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, strategy=Scripted(lambda c: [proposal()]))
    rig.kill.engage("stop")
    assert rig.loop.step().submitted == 0 and rig.venue._orders == {}


# ================================================================ fake LIVE loop (gate)
class FakeLiveVenue(PaperVenue):
    """A 'production' venue that is really a local fake. Records every submission."""

    def __init__(self, log: list[OrderIntent], **kw: Any) -> None:
        super().__init__(mode=Mode.PAPER, **kw)
        self.mode = Mode.LIVE
        self.log = log

    def submit(self, intent: OrderIntent):
        self.log.append(intent)
        return super().submit(intent)


def live_cfg() -> AppConfig:
    return AppConfig.model_validate({
        "mode": "live", "allow_live_trading": True, "exchange": {"environment": "prod"},
        "risk": {"category_allowlist": ["weather"]}})


def live_token(pid_offset: int = 0, ttl: timedelta = timedelta(hours=1),
               host: str = PROD_REST_HOST) -> ArmingToken:
    return ArmingToken(host=host, config_hash="h", issued_at=T0, expires_at=T0 + ttl,
                       max_total_risk=D(5), pid=os.getpid() + pid_offset)


def live_loop(tmp_path: Path, strategy: Any, log: list[OrderIntent],
              token: ArmingToken | None = None, clock: ReplayClock | None = None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    cfg = live_cfg()
    clk = clock or ReplayClock(T0)
    md = InMemoryMarketData()
    md.upsert_market(market())
    md.set_book(book(ts=clk.now()))
    fees = cfg.fees.schedule()
    venue = FakeLiveVenue(log, clock=clk, fees=fees, book_source=md.get_orderbook,
                          market_source=md.get_market, starting_cash=D("1009"))
    store = Store(tmp_path / "live.db")
    tok = token or live_token()
    loop = TradingLoop(
        config=cfg, clock=clk, market_data=md, estimator=FixedEstimator(D("0.95")),
        strategy=strategy, risk=HardRiskManager(cfg.risk, fees, slippage_buffer=D("0.005")),
        engine=ExecutionEngine(venue, store, clk), store=store,
        kill_switch=KillSwitch(tmp_path / "KILL"), alerts=AlertSink(store),
        submission_gate=tok.is_valid)
    return loop, venue


def test_live_loop_trades_only_with_valid_token_and_respects_canary(tmp_path: Path) -> None:
    from kalshi_edge.strategies import make_strategy
    cfg = live_cfg()
    strat = make_strategy(cfg.strategy, cfg.risk)
    log: list[OrderIntent] = []
    loop, venue = live_loop(tmp_path / "ok", strat, log)
    rep = loop.step()
    assert rep.submitted == 1 and len(log) == 1
    assert log[0].mode is Mode.LIVE and log[0].max_cost <= D("5")  # canary total risk <= $5
    assert D("1009") - venue.cash <= D("5")


@pytest.mark.parametrize(("label", "token_kw", "advance"), [
    ("wrong_pid", dict(pid_offset=1), 0),
    ("expired_at_expiry", dict(ttl=timedelta(hours=1)), 3600),
    ("over_24h_ttl", dict(ttl=timedelta(hours=25)), 0),
])
def test_live_loop_refuses_with_bad_or_expired_token(tmp_path: Path, label: str,
                                                     token_kw: dict[str, Any],
                                                     advance: int) -> None:
    from kalshi_edge.strategies import make_strategy
    cfg = live_cfg()
    log: list[OrderIntent] = []
    clk = ReplayClock(T0)
    loop, _ = live_loop(tmp_path / label, make_strategy(cfg.strategy, cfg.risk), log,
                        token=live_token(**token_kw), clock=clk)
    clk.advance(timedelta(seconds=advance))
    loop.md.set_book(book(ts=clk.now()))  # type: ignore[attr-defined]
    rep = loop.step()
    assert rep.submitted == 0 and log == [], label
    assert rep.rejections.get("mode_does_not_permit_submission", 0) >= 1


def test_live_loop_disarms_mid_session_when_token_expires(tmp_path: Path) -> None:
    from kalshi_edge.strategies import make_strategy
    cfg = live_cfg()
    log: list[OrderIntent] = []
    clk = ReplayClock(T0)
    loop, _ = live_loop(tmp_path, make_strategy(cfg.strategy, cfg.risk), log, clock=clk)
    assert loop.step().submitted == 1
    clk.advance(timedelta(hours=2))
    loop.md.set_book(book(ts=clk.now()))  # type: ignore[attr-defined]
    n = len(log)
    assert loop.step().submitted == 0 and len(log) == n


def test_build_runtime_live_refuses_without_gates(tmp_path: Path) -> None:
    cfg_off = AppConfig.model_validate({
        "mode": "live", "allow_live_trading": False, "exchange": {"environment": "prod"},
        "paths": {"state_dir": str(tmp_path / "a")}})
    md = InMemoryMarketData()
    with pytest.raises(PermissionError, match="allow_live_trading"):
        build_runtime(cfg_off, authorization=live_token(), market_data=md)
    cfg = AppConfig.model_validate({
        "mode": "live", "allow_live_trading": True, "exchange": {"environment": "prod"},
        "paths": {"state_dir": str(tmp_path / "b")}})
    for tok in (None, live_token(pid_offset=1)):
        with pytest.raises(PermissionError, match="arming"):
            build_runtime(cfg, authorization=tok, market_data=md)


# ================================================== paper never reaches production orders
PROD_ENV = {
    "KALSHI_PROD_KEY_ID": "prod-key-id-from-operator",
    "KALSHI_PROD_PRIVATE_KEY_PATH": "/nonexistent/prod.pem",
    "KALSHI_API_URL": PROD_REST_BASE,
    "KALSHI_ENV": "production", "ENV": "production", "KALSHI_BASE_URL": PROD_REST_BASE,
}


def test_paper_lifecycle_makes_zero_http_requests_even_with_prod_env(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in PROD_ENV.items():
        monkeypatch.setenv(k, v)
    sent: list[httpx.Request] = []

    def trap(self: httpx.Client, request: httpx.Request, **kw: Any) -> httpx.Response:
        sent.append(request)
        raise AssertionError(f"paper mode attempted HTTP {request.method} {request.url}")

    monkeypatch.setattr(httpx.Client, "send", trap)
    monkeypatch.setattr(httpx.AsyncClient, "send", trap)
    rig = make_rig(tmp_path)
    assert rig.loop.step().submitted == 1
    rig.kill.engage("end")
    rig.loop.step()
    assert sent == []


def test_paper_venue_has_no_route_to_exchange_code() -> None:
    tree = ast.parse((ROOT / "execution" / "paper.py").read_text())
    imported = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    imported |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    assert not any(m.startswith(("httpx", "requests", "socket", "urllib", "websockets",
                                 "kalshi_edge.exchange")) for m in imported), imported


def test_only_wiring_modules_import_the_live_venue() -> None:
    allowed = {"exchange/venues.py", "exchange/__init__.py", "runtime/factory.py", "cli.py"}
    offenders = []
    for py in ROOT.rglob("*.py"):
        rel = py.relative_to(ROOT).as_posix()
        if rel in allowed:
            continue
        if "LiveExecutionVenue" in py.read_text():
            offenders.append(rel)
    assert offenders == []


def test_strategy_risk_model_backtest_modules_cannot_reach_exchange_http() -> None:
    bad = []
    for pkg in ("strategies", "risk", "models", "execution"):
        for py in (ROOT / pkg).rglob("*.py"):
            tree = ast.parse(py.read_text())
            mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
            mods |= {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
            if any(m.startswith(("httpx", "kalshi_edge.exchange.rest",
                                 "kalshi_edge.exchange.venues")) for m in mods):
                bad.append(py.relative_to(ROOT).as_posix())
    assert bad == []


class Recorder:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    def __call__(self, req: httpx.Request) -> httpx.Response:
        self.requests.append(req)
        return httpx.Response(200, json={})


@pytest.mark.parametrize(("method", "path"), [
    ("POST", "/trade-api/v2/portfolio/events/orders"),
    ("DELETE", "/trade-api/v2/portfolio/events/orders/o-1"),
    ("PUT", "/trade-api/v2/portfolio/orders/o-1"),
    ("PATCH", "/trade-api/v2/portfolio/orders/o-1"),
    ("GET", "/trade-api/v2/portfolio/balance"),
    ("GET", "/trade-api/v2/PORTFOLIO/positions"),
    ("GET", "/trade-api/v2/portfolio/orders"),
])
def test_prod_host_blocks_orders_and_portfolio_before_transport(
        method: str, path: str, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in PROD_ENV.items():
        monkeypatch.setenv(k, v)
    rec = Recorder()
    c = KalshiRestClient(PROD_REST_BASE, transport=httpx.MockTransport(rec),
                         sleep=lambda s: None)
    with pytest.raises(ProductionOrderBlocked):
        c.send(method, path.removeprefix("/trade-api/v2"))
    assert rec.requests == []  # never even reached the (mock) wire


def test_prod_public_get_is_allowed_and_demo_host_posts_allowed() -> None:
    rec = Recorder()
    c = KalshiRestClient(PROD_REST_BASE, transport=httpx.MockTransport(rec), sleep=lambda s: None)
    c.send("GET", "/markets")
    assert len(rec.requests) == 1
    rec2 = Recorder()
    d = KalshiRestClient(DEMO_REST_BASE, transport=httpx.MockTransport(rec2),
                         sleep=lambda s: None)
    d.send("POST", "/portfolio/events/orders", json_body={})
    assert len(rec2.requests) == 1


@pytest.mark.parametrize("host", ["demo-api.kalshi.co.evil.com", "api.demo-api.kalshi.co",
                                  "kalshi.co", "localhost", "127.0.0.1", ""])
def test_lookalike_and_unknown_hosts_are_production(host: str) -> None:
    with pytest.raises(ProductionOrderBlocked):
        ProductionOrderGuard(None).check("POST", host, "/trade-api/v2/portfolio/events/orders")


def test_guard_authorization_must_match_host_and_be_valid() -> None:
    now = ReplayClock(T0).now
    g_ok = ProductionOrderGuard(live_token(), clock=now)
    g_ok.check("POST", PROD_REST_HOST, "/portfolio/events/orders")  # valid + right host
    for tok in (live_token(host="external-api.kalshi.com"), live_token(pid_offset=1),
                live_token(ttl=timedelta(seconds=-1))):
        with pytest.raises(ProductionOrderBlocked):
            ProductionOrderGuard(tok, clock=now).check("POST", PROD_REST_HOST,
                                                       "/portfolio/events/orders")
    expired = ReplayClock(T0 + timedelta(hours=2)).now
    with pytest.raises(ProductionOrderBlocked):
        ProductionOrderGuard(live_token(), clock=expired).check("DELETE", PROD_REST_HOST, "/x")


def test_demo_venue_refuses_prod_base_url_and_ignores_prod_credentials(
        monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in PROD_ENV.items():
        monkeypatch.setenv(k, v)
    cfg = ExchangeConfig(environment="demo")
    with pytest.raises(VenueConfigError):
        DemoExecutionVenue(cfg, ReplayClock(T0), base_url=PROD_REST_BASE,
                           env=dict(os.environ))
    with pytest.raises(CredentialsMissing):  # prod creds in the environment are not demo creds
        DemoExecutionVenue(cfg, ReplayClock(T0), env=dict(os.environ))
    with pytest.raises(VenueConfigError):
        DemoExecutionVenue(ExchangeConfig(environment="prod_public"), ReplayClock(T0),
                           env=dict(os.environ))


class StubSigner:
    def headers(self, method: str, path: str, ts: int) -> dict[str, str]:
        return {"KALSHI-ACCESS-KEY": "stub", "KALSHI-ACCESS-TIMESTAMP": str(ts),
                "KALSHI-ACCESS-SIGNATURE": "stub"}


def live_intent() -> OrderIntent:
    return OrderIntent("cid-live", "d", Mode.LIVE, StrategyId.POSITIVE_EV_80, TICKER, "E",
                       Outcome.YES, D(1), D("0.87"), TimeInForce.IMMEDIATE_OR_CANCEL, D(1), "h",
                       "v", T0)


def test_live_venue_construction_refusals() -> None:
    clk = ReplayClock(T0)
    cfg = ExchangeConfig(environment="prod")
    rec = Recorder()
    t = httpx.MockTransport(rec)
    common: dict[str, Any] = dict(signer=StubSigner(), transport=t)
    with pytest.raises(LiveTradingDisabled, match="no live authorization"):
        LiveExecutionVenue(cfg, clk, None, **common)
    with pytest.raises(LiveTradingDisabled, match="invalid or expired"):
        LiveExecutionVenue(cfg, clk, live_token(ttl=timedelta(seconds=-5)), **common)
    with pytest.raises(LiveTradingDisabled, match="invalid or expired"):
        LiveExecutionVenue(cfg, clk, live_token(pid_offset=1), **common)
    with pytest.raises(LiveTradingDisabled, match="different host"):
        LiveExecutionVenue(cfg, clk, live_token(host="external-api.kalshi.com"), **common)
    with pytest.raises(LiveTradingDisabled, match="not 'prod'"):
        LiveExecutionVenue(ExchangeConfig(environment="prod_public"), clk, live_token(), **common)
    with pytest.raises(LiveTradingDisabled):
        LiveExecutionVenue(ExchangeConfig(environment="demo"), clk, live_token(), **common)
    assert rec.requests == []


def test_live_venue_missing_prod_credentials_even_with_valid_token() -> None:
    with pytest.raises(CredentialsMissing):
        LiveExecutionVenue(ExchangeConfig(environment="prod"), ReplayClock(T0), live_token(),
                           env={})


def test_live_venue_rechecks_authorization_on_every_mutation() -> None:
    clk = ReplayClock(T0)
    rec = Recorder()
    v = LiveExecutionVenue(ExchangeConfig(environment="prod"), clk, live_token(),
                           signer=StubSigner(), transport=httpx.MockTransport(rec),
                           sleep=lambda s: None)
    clk.advance(timedelta(hours=2))  # token (1h) has expired while the process kept running
    with pytest.raises(LiveTradingDisabled):
        v.submit(live_intent())
    with pytest.raises(LiveTradingDisabled):
        v.cancel("cid-live")
    assert rec.requests == []


def test_live_venue_intent_mode_mismatch_refused() -> None:
    clk = ReplayClock(T0)
    rec = Recorder()
    v = LiveExecutionVenue(ExchangeConfig(environment="prod"), clk, live_token(),
                           signer=StubSigner(), transport=httpx.MockTransport(rec),
                           sleep=lambda s: None)
    paper_intent = dataclasses.replace(live_intent(), mode=Mode.PAPER)
    with pytest.raises(VenueConfigError):
        v.submit(paper_intent)
    assert rec.requests == []


# ============================================================ REST failure behaviour
def demo_venue(handler: Callable[[httpx.Request], httpx.Response], sleeps: list[float]
               ) -> DemoExecutionVenue:
    return DemoExecutionVenue(ExchangeConfig(environment="demo"), ReplayClock(T0),
                              httpx.MockTransport(handler), signer=StubSigner(),  # type: ignore[arg-type]
                              sleep=sleeps.append)


def demo_intent(cid: str = "ke-demo-1") -> OrderIntent:
    return OrderIntent(cid, "d", Mode.DEMO, StrategyId.POSITIVE_EV_80, TICKER, "E", Outcome.YES,
                       D(4), D("0.83"), TimeInForce.IMMEDIATE_OR_CANCEL, D(5), "h", "v", T0)


def test_get_retries_429_5xx_with_backoff_but_post_never() -> None:
    sleeps: list[float] = []
    counts = {"GET": 0, "POST": 0}

    def h(req: httpx.Request) -> httpx.Response:
        counts[req.method] += 1
        if req.method == "GET":
            return httpx.Response(429 if counts["GET"] == 1 else 503 if counts["GET"] == 2 else 200,
                                  json={"orders": [], "cursor": ""},
                                  headers={"Retry-After": "2"} if counts["GET"] == 1 else {})
        return httpx.Response(503)

    v = demo_venue(h, sleeps)
    assert v.get_order("nope") is None
    assert counts["GET"] == 3 and len([s for s in sleeps if s >= 2]) >= 1  # backoff happened
    with pytest.raises(AmbiguousSubmission):
        v.submit(demo_intent())
    assert counts["POST"] == 1
    counts["POST"] = 0

    def h429(req: httpx.Request) -> httpx.Response:
        counts["POST"] += 1
        return httpx.Response(429, json={})

    with pytest.raises(RateLimited):
        demo_venue(h429, []).submit(demo_intent("ke-demo-2"))
    assert counts["POST"] == 1


def test_get_gives_up_after_retries_and_surfaces_error() -> None:
    sleeps: list[float] = []
    n = {"c": 0}

    def h(req: httpx.Request) -> httpx.Response:
        n["c"] += 1
        return httpx.Response(500, json={})

    with pytest.raises(HttpStatusError):
        demo_venue(h, sleeps).get_order("x")
    assert 2 <= n["c"] <= 5 and len(sleeps) >= 1


def test_engine_with_demo_venue_queries_before_retry_after_post_timeout(tmp_path: Path) -> None:
    posts: list[httpx.Request] = []
    order = {"order_id": "o-9", "client_order_id": "ke-demo-1", "ticker": TICKER, "side": "yes",
             "action": "buy", "status": "executed", "initial_count_fp": "4.00",
             "fill_count_fp": "4.00", "remaining_count_fp": "0.00",
             "created_time": "2026-01-05T12:00:00Z", "last_update_time": "2026-01-05T12:00:01Z",
             "yes_price_dollars": "0.8300"}

    def h(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            posts.append(req)
            raise httpx.ReadTimeout("timed out after send")
        if req.url.path.endswith("/portfolio/orders"):
            return httpx.Response(200, json={"orders": [order], "cursor": ""})
        return httpx.Response(200, json={"fills": [], "cursor": ""})

    clk = ReplayClock(T0)
    store = Store(tmp_path / "d.db")
    eng = ExecutionEngine(demo_venue(h, []), store, clk)
    res = eng.submit(demo_intent())
    assert len(posts) == 1  # exactly one POST, then a GET query, never a second POST
    assert res.state is OrderState.FILLED
    assert store.get_order("ke-demo-1").state is OrderState.FILLED  # type: ignore[union-attr]
    # re-submitting the same decision id is impossible
    with pytest.raises(Exception, match="ke-demo-1"):
        eng.submit(demo_intent())
    assert len(posts) == 1


def test_demo_4xx_post_is_definitive_rejection_not_ambiguous(tmp_path: Path) -> None:
    store = Store(tmp_path / "d.db")
    eng = ExecutionEngine(demo_venue(lambda r: httpx.Response(400, json={"e": 1}), []), store,
                          ReplayClock(T0))
    res = eng.submit(demo_intent())
    assert res.state is OrderState.REJECTED
    with pytest.raises(OrderRejected):
        demo_venue(lambda r: httpx.Response(422, json={}), []).submit(demo_intent("z"))


def test_secrets_never_appear_in_persisted_state(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.loop.step()
    blob = json.dumps([dict(r) for r in rig.store._conn.execute("SELECT * FROM events")])
    for needle in ("PRIVATE KEY", "KALSHI-ACCESS-SIGNATURE", "prod-key-id-from-operator"):
        assert needle not in blob
