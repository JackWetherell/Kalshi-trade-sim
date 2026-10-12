"""Shared builders for the independent test-suite (no network, deterministic)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from kalshi_edge.config import AppConfig, RiskLimits
from kalshi_edge.data.memory import InMemoryMarketData
from kalshi_edge.domain import (
    AccountSnapshot,
    CalibrationStatus,
    Market,
    MarketStatus,
    Mode,
    OrderBook,
    Outcome,
    PriceLevel,
    PriceRange,
    ProbabilityEstimate,
    StrategyId,
    TradeProposal,
)
from kalshi_edge.execution import ExecutionEngine, PaperVenue, Store
from kalshi_edge.fees import FeeSchedule
from kalshi_edge.interfaces import Strategy
from kalshi_edge.monitoring.alerts import AlertSink
from kalshi_edge.risk import (
    Exposure,
    HardRiskManager,
    HealthStatus,
    KillSwitch,
    RiskContext,
    RiskState,
)
from kalshi_edge.runtime.clock import ReplayClock
from kalshi_edge.runtime.loop import TradingLoop, default_submission_gate
from kalshi_edge.strategies import make_strategy

D = Decimal
T0 = datetime(2026, 1, 5, 12, tzinfo=UTC)
TICKER = "KXHIGHNY-26JAN05-T40"
EVENT = "KXHIGHNY-26JAN05"
GRID = (PriceRange(D("0.01"), D("0.99"), D("0.01")),)
FEES = FeeSchedule()


def market(**kw: object) -> Market:
    base: dict[str, object] = dict(
        ticker=TICKER, event_ticker=EVENT, series_ticker="KXHIGHNY", category="weather",
        title="NYC high > 40", status=MarketStatus.OPEN, market_type="binary",
        open_time=T0 - timedelta(days=1), close_time=T0 + timedelta(hours=10),
        price_ranges=GRID, rules_verified=True)
    base.update(kw)
    return Market(**base)  # type: ignore[arg-type]


def book(yes_asks: Sequence[tuple[str, str]] = (("0.87", "1000"),),
         no_asks: Sequence[tuple[str, str]] = (), *, yes_bids: Sequence[tuple[str, str]] = (
             ("0.85", "100"),), no_bids_extra: Sequence[tuple[str, str]] = (),
         ticker: str = TICKER, ts: datetime = T0) -> OrderBook:
    """Book from desired ASK levels (YES ask a <=> NO bid 1-a) plus explicit YES bids.

    Defaults: YES ask 0.87 x1000, YES bid 0.85 -> NO ask 0.15 (NO best bid 0.13).
    """
    nb = [PriceLevel(D(1) - D(p), D(q)) for p, q in yes_asks]
    nb += [PriceLevel(D(p), D(q)) for p, q in no_bids_extra]
    yb = [PriceLevel(D(1) - D(p), D(q)) for p, q in no_asks]
    yb += [PriceLevel(D(p), D(q)) for p, q in yes_bids]
    return OrderBook(ticker, tuple(sorted(yb, key=lambda lv: lv.price)),
                     tuple(sorted(nb, key=lambda lv: lv.price)), ts)


def estimate(q: str = "0.97", q_safe: str = "0.95", *, outcome: Outcome = Outcome.YES,
             independent: bool = True, status: CalibrationStatus = CalibrationStatus.ACCEPTED,
             ticker: str = TICKER, as_of: datetime = T0) -> ProbabilityEstimate:
    return ProbabilityEstimate(ticker, outcome, D(q), D(q_safe), "m", "1", independent, status,
                               as_of)


def proposal(*, strategy: StrategyId = StrategyId.POSITIVE_EV_80, qty: str = "10",
             limit: str = "0.87", expected: str | None = None, outcome: Outcome = Outcome.YES,
             est: ProbabilityEstimate | str | None = "default",
             mkt: Market | None = None, fee_pc: str = "0.01") -> TradeProposal:
    e = estimate(outcome=outcome) if est == "default" else est
    assert e is None or isinstance(e, ProbabilityEstimate)
    return TradeProposal(
        strategy_id=strategy, market=mkt or market(), outcome=outcome, quantity=D(qty),
        limit_price=D(limit), expected_price=D(expected or limit), fee_per_contract=D(fee_pc),
        estimate=e, edge_per_contract=None, ev_per_contract=None, book_ts=T0)


def limits(**kw: object) -> RiskLimits:
    base: dict[str, object] = {"category_allowlist": ("weather",)}
    base.update(kw)
    return RiskLimits(**base)  # type: ignore[arg-type]


def risk_ctx(*, bk: OrderBook | None = None, mode: Mode = Mode.PAPER,
             account: AccountSnapshot | None = None, exposure: Exposure | None = None,
             state: RiskState | None = None, health: HealthStatus | None = None,
             kill: bool = False, permitted: bool = True, capital: str = "1009",
             equity: str | None = None, pending_cash: str = "0",
             now: datetime = T0, live_budget: str | None = "5") -> RiskContext:
    acct = account or AccountSnapshot(now, D(capital), (), ())
    return RiskContext(
        now=now, mode=mode, book=bk or book(ts=now), account=acct,
        exposure=exposure or Exposure(), risk_state=state or RiskState.fresh(D(capital), now),
        health=health or HealthStatus(True, True, 0), kill_engaged=kill,
        submission_permitted=permitted, strategy_capital=D(capital),
        account_equity=D(equity or capital), pending_cash=D(pending_cash),
        live_max_total_risk=None if live_budget is None else D(live_budget))


class FixedEstimator:
    model_id = "fixed"
    model_version = "test"

    def __init__(self, q_yes: Decimal, *, q_safe_haircut: Decimal = Decimal(0),
                 status: CalibrationStatus = CalibrationStatus.ACCEPTED,
                 independent: bool = True, as_of_lag: timedelta = timedelta(0)) -> None:
        self.q_yes, self.status, self.independent = q_yes, status, independent
        self.haircut, self.lag = q_safe_haircut, as_of_lag

    def estimate(self, mkt: Market, outcome: Outcome, as_of: datetime
                 ) -> ProbabilityEstimate:
        q = self.q_yes if outcome is Outcome.YES else D(1) - self.q_yes
        return ProbabilityEstimate(mkt.ticker, outcome, q, max(D(0), q - self.haircut),
                                   self.model_id, self.model_version, self.independent,
                                   self.status, as_of - self.lag)


@dataclass
class Rig:
    cfg: AppConfig
    clock: ReplayClock
    md: InMemoryMarketData
    venue: PaperVenue
    store: Store
    loop: TradingLoop
    kill: KillSwitch
    alerts: AlertSink
    db_path: Path
    extra: dict[str, object] = field(default_factory=dict)

    def step_forward(self, **delta: float) -> None:
        self.clock.advance(timedelta(**delta))

    def refresh_book(self, **kw: object) -> None:
        self.md.set_book(book(ts=self.clock.now(), **kw))  # type: ignore[arg-type]


def make_rig(tmp_path: Path, *, strategy: Strategy | None = None,
             strategy_id: StrategyId = StrategyId.POSITIVE_EV_80, q_yes: str = "0.95",
             mode: Mode = Mode.PAPER, venue: PaperVenue | None = None,
             md: InMemoryMarketData | None = None, clock: ReplayClock | None = None,
             db_name: str = "s.sqlite3", risk: RiskLimits | None = None,
             estimator: FixedEstimator | None = None, cash: str = "1009",
             markets: Sequence[Market] | None = None, books: Sequence[OrderBook] | None = None,
             depth_fraction: str = "1") -> Rig:
    cfg = AppConfig(mode=mode, risk=risk or limits())
    cfg = cfg.model_copy(update={"strategy": cfg.strategy.model_copy(update={"id": strategy_id})})
    clk = clock or ReplayClock(T0)
    data = md or InMemoryMarketData()
    if md is None:
        for m in markets or [market()]:
            data.upsert_market(m)
        for b in books or [book(ts=clk.now())]:
            data.set_book(b)
    fees = cfg.fees.schedule()
    ven = venue or PaperVenue(mode=mode, clock=clk, fees=fees, book_source=data.get_orderbook,
                              market_source=data.get_market, starting_cash=D(cash),
                              depth_fraction=D(depth_fraction))
    db_path = tmp_path / db_name
    store = Store(db_path)
    kill = KillSwitch(tmp_path / "KILL_SWITCH")
    alerts = AlertSink(store)
    strat = strategy or make_strategy(cfg.strategy, cfg.risk)
    loop = TradingLoop(
        config=cfg, clock=clk, market_data=data,
        estimator=estimator or FixedEstimator(D(q_yes)), strategy=strat,
        risk=HardRiskManager(cfg.risk, fees, slippage_buffer=cfg.strategy.slippage_buffer),
        engine=ExecutionEngine(ven, store, clk), store=store, kill_switch=kill, alerts=alerts,
        submission_gate=default_submission_gate(mode))
    return Rig(cfg, clk, data, ven, store, loop, kill, alerts, db_path)


def replace_market(rig: Rig, **kw: object) -> None:
    rig.md.upsert_market(replace(rig.md.markets[TICKER], **kw))
