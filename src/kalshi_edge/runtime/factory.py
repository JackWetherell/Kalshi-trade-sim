"""Wire a ``TradingLoop`` for paper / demo / live. (Backtests are wired in ``backtest``.)

Only the market-data provider, clock and execution venue differ between modes; the
strategy, estimator, risk manager, execution engine and loop are identical.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from kalshi_edge.config import AppConfig
from kalshi_edge.domain import Market, Mode, OrderBook
from kalshi_edge.execution.engine import ExecutionEngine
from kalshi_edge.execution.paper import PaperVenue
from kalshi_edge.execution.store import Store
from kalshi_edge.interfaces import ExecutionVenue, MarketDataProvider, ProbabilityEstimator
from kalshi_edge.monitoring.alerts import AlertSink
from kalshi_edge.risk.killswitch import KillSwitch
from kalshi_edge.risk.manager import HardRiskManager
from kalshi_edge.runtime.arming import ArmingToken
from kalshi_edge.runtime.clock import WallClock
from kalshi_edge.runtime.loop import TradingLoop, default_submission_gate
from kalshi_edge.strategies import make_strategy

PAPER_VENUE_KEY = "paper_venue_state"


class RulesAllowlistProvider:
    """Marks ``rules_verified`` only for operator-reviewed series (fail closed otherwise)."""

    def __init__(self, inner: MarketDataProvider, verified_series: tuple[str, ...]) -> None:
        self.inner = inner
        self.verified = frozenset(verified_series)

    def _tag(self, m: Market | None) -> Market | None:
        if m is None:
            return None
        return replace(m, rules_verified=m.series_ticker in self.verified)

    def list_markets(self) -> Sequence[Market]:
        return [mm for mm in (self._tag(m) for m in self.inner.list_markets()) if mm is not None]

    def get_market(self, ticker: str) -> Market | None:
        return self._tag(self.inner.get_market(ticker))

    def get_orderbook(self, ticker: str) -> OrderBook | None:
        return self.inner.get_orderbook(ticker)


@dataclass(slots=True)
class Runtime:
    loop: TradingLoop
    store: Store
    venue: ExecutionVenue
    market_data: MarketDataProvider

    def persist(self) -> None:
        if isinstance(self.venue, PaperVenue):
            self.store.put(PAPER_VENUE_KEY, self.venue.snapshot())


def build_estimator(cfg: AppConfig) -> ProbabilityEstimator | None:
    """Independent estimator for C/D in paper/demo/live.

    No live external-forecast feed is wired in this build (the build environment could not
    reach any forecast source), and no calibrator has passed acceptance on real outcomes.
    Returning None means strategy C produces NO TRADE — the correct, fail-closed outcome.
    """
    del cfg
    return None


def build_runtime(cfg: AppConfig, *, authorization: ArmingToken | None = None,
                  market_data: MarketDataProvider | None = None,
                  venue: ExecutionVenue | None = None,
                  estimator: ProbabilityEstimator | None = None) -> Runtime:
    clock = WallClock()
    cfg.paths.state_dir.mkdir(parents=True, exist_ok=True)
    store = Store(cfg.paths.db_path)
    fees = cfg.fees.schedule()
    gate: Callable[[datetime], bool]

    if cfg.mode is Mode.PAPER:
        if market_data is None:
            from kalshi_edge.exchange.rest import PublicMarketData

            market_data = PublicMarketData(cfg.exchange)
        if venue is None:
            pv = PaperVenue(mode=Mode.PAPER, clock=clock, fees=fees,
                            book_source=market_data.get_orderbook,
                            market_source=market_data.get_market,
                            starting_cash=cfg.strategy_capital,
                            depth_fraction=Decimal("0.5"))
            saved = store.get(PAPER_VENUE_KEY)
            if saved:
                pv.restore(saved)
            venue = pv
        gate = default_submission_gate(Mode.PAPER)
    elif cfg.mode is Mode.DEMO:
        if market_data is None or venue is None:
            from kalshi_edge.exchange.hosts import DEMO_REST_BASE
            from kalshi_edge.exchange.rest import PublicMarketData
            from kalshi_edge.exchange.venues import DemoExecutionVenue

            market_data = market_data or PublicMarketData(cfg.exchange, base_url=DEMO_REST_BASE)
            venue = venue or DemoExecutionVenue(cfg.exchange, clock)
        gate = default_submission_gate(Mode.DEMO)
    elif cfg.mode is Mode.LIVE:
        if not cfg.allow_live_trading:
            raise PermissionError("live config does not set allow_live_trading: true")
        if authorization is None or not authorization.is_valid(clock.now()):
            raise PermissionError("live mode requires a fresh interactive arming token")
        token = authorization
        if market_data is None or venue is None:
            from kalshi_edge.exchange.rest import PublicMarketData
            from kalshi_edge.exchange.venues import LiveExecutionVenue

            market_data = market_data or PublicMarketData(cfg.exchange)
            venue = venue or LiveExecutionVenue(cfg.exchange, clock, token)
        gate = token.is_valid
    else:
        raise ValueError("use `kalshi-edge backtest` for backtest mode")

    if venue is None or market_data is None:  # pragma: no cover - defensive
        raise RuntimeError("venue/market data not constructed")
    market_data = RulesAllowlistProvider(market_data, cfg.exchange.rules_verified_series)
    est = estimator if estimator is not None else build_estimator(cfg)
    engine = ExecutionEngine(venue, store, clock)
    loop = TradingLoop(
        config=cfg, clock=clock, market_data=market_data, estimator=est,
        strategy=make_strategy(cfg.strategy, cfg.risk),
        risk=HardRiskManager(cfg.risk, fees, slippage_buffer=cfg.strategy.slippage_buffer),
        engine=engine, store=store, kill_switch=KillSwitch(cfg.paths.kill_switch_path),
        alerts=AlertSink(store), submission_gate=gate)
    return Runtime(loop, store, venue, market_data)
