"""End-to-end: market -> estimate -> strategy C -> risk -> paper venue -> settlement."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from kalshi_edge.config import AppConfig, RiskLimits
from kalshi_edge.data.memory import InMemoryMarketData
from kalshi_edge.domain import (
    CalibrationStatus,
    Market,
    MarketStatus,
    Mode,
    OrderBook,
    Outcome,
    PriceLevel,
    PriceRange,
    ProbabilityEstimate,
    SettlementResult,
    StrategyId,
)
from kalshi_edge.execution import ExecutionEngine, PaperVenue, Store
from kalshi_edge.monitoring.alerts import AlertSink
from kalshi_edge.risk import HardRiskManager, KillSwitch
from kalshi_edge.runtime.clock import ReplayClock
from kalshi_edge.runtime.loop import TradingLoop, default_submission_gate
from kalshi_edge.strategies import make_strategy

T0 = datetime(2026, 1, 5, 12, tzinfo=UTC)


class FixedEstimator:
    model_id = "fixed"
    model_version = "test"

    def __init__(self, q_yes: D, status: CalibrationStatus = CalibrationStatus.ACCEPTED) -> None:
        self.q_yes = q_yes
        self.status = status

    def estimate(self, market: Market, outcome: Outcome, as_of: datetime
                 ) -> ProbabilityEstimate:
        q = self.q_yes if outcome is Outcome.YES else D(1) - self.q_yes
        return ProbabilityEstimate(market.ticker, outcome, q, q, self.model_id,
                                   self.model_version, True, self.status, as_of)


def _market() -> Market:
    return Market(ticker="KXHIGHNY-26JAN05-T40", event_ticker="KXHIGHNY-26JAN05",
                  series_ticker="KXHIGHNY", category="weather", title="NYC high > 40",
                  status=MarketStatus.OPEN, market_type="binary", open_time=T0 - timedelta(days=1),
                  close_time=T0 + timedelta(hours=10),
                  price_ranges=(PriceRange(D("0"), D("1"), D("0.01")),), rules_verified=True)


def _book(ts: datetime) -> OrderBook:
    # YES bids up to 0.85; NO bids up to 0.13 => YES ask 0.87
    return OrderBook("KXHIGHNY-26JAN05-T40",
                     yes_bids=(PriceLevel(D("0.84"), D("100")), PriceLevel(D("0.85"), D("50"))),
                     no_bids=(PriceLevel(D("0.11"), D("200")), PriceLevel(D("0.13"), D("100"))),
                     ts=ts)


def _build(tmp_path: Path, q_yes: D, strategy: StrategyId = StrategyId.POSITIVE_EV_80,
           status: CalibrationStatus = CalibrationStatus.ACCEPTED):
    cfg = AppConfig(mode=Mode.BACKTEST, risk=RiskLimits(category_allowlist=("weather",)))
    cfg = cfg.model_copy(update={"strategy": cfg.strategy.model_copy(update={"id": strategy})})
    clock = ReplayClock(T0)
    md = InMemoryMarketData()
    md.upsert_market(_market())
    md.set_book(_book(T0))
    fees = cfg.fees.schedule()
    venue = PaperVenue(mode=Mode.BACKTEST, clock=clock, fees=fees, book_source=md.get_orderbook,
                       market_source=md.get_market, starting_cash=cfg.strategy_capital)
    store = Store(tmp_path / "s.sqlite3")
    loop = TradingLoop(config=cfg, clock=clock, market_data=md,
                       estimator=FixedEstimator(q_yes, status),
                       strategy=make_strategy(cfg.strategy, cfg.risk),
                       risk=HardRiskManager(cfg.risk, fees,
                                            slippage_buffer=cfg.strategy.slippage_buffer),
                       engine=ExecutionEngine(venue, store, clock), store=store,
                       kill_switch=KillSwitch(tmp_path / "KILL"), alerts=AlertSink(store),
                       submission_gate=default_submission_gate(Mode.BACKTEST))
    return loop, venue, md, store


def test_positive_ev_trades_within_caps_and_settles(tmp_path: Path) -> None:
    loop, venue, md, store = _build(tmp_path, D("0.95"))
    rep = loop.step()
    assert rep.submitted == 1, rep.as_dict()
    acct = venue.get_account()
    assert len(acct.positions) == 1
    pos = acct.positions[0]
    assert pos.outcome is Outcome.YES
    # Per-entry cap: 1% of $1009 = $10.09 of cost basis incl. fees.
    assert pos.cost_basis <= D("10.09")
    md.upsert_market(replace(_market(), status=MarketStatus.SETTLED, result=SettlementResult.YES))
    loop.clock.advance(timedelta(hours=11))  # type: ignore[attr-defined]
    md.set_book(_book(loop.clock.now()))
    loop.step()
    assert venue.get_account().positions == ()
    assert venue.cash > D("1009")
    assert store.settled_tickers() == {"KXHIGHNY-26JAN05-T40"}


def test_no_trade_when_q_at_market(tmp_path: Path) -> None:
    loop, venue, *_ = _build(tmp_path, D("0.87"))
    rep = loop.step()
    assert rep.submitted == 0
    assert venue.get_account().positions == ()


def test_uncalibrated_estimate_never_trades(tmp_path: Path) -> None:
    loop, _venue, *_ = _build(tmp_path, D("0.99"), status=CalibrationStatus.INSUFFICIENT)
    assert loop.step().submitted == 0


def test_kill_switch_blocks_everything(tmp_path: Path) -> None:
    loop, _venue, *_ = _build(tmp_path, D("0.95"))
    loop.kill.engage("test")
    rep = loop.step()
    assert rep.halted and rep.halted.startswith("kill_switch")
    assert rep.submitted == 0


def test_naive_baseline_runs_in_backtest(tmp_path: Path) -> None:
    loop, _venue, *_ = _build(tmp_path, D("0.5"), strategy=StrategyId.NAIVE_80)
    rep = loop.step()
    assert rep.submitted == 1, rep.as_dict()
