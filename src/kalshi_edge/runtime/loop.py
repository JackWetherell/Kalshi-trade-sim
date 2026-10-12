"""The single decision pipeline used by backtest, paper, demo and live modes.

    discovery -> eligibility -> independent estimate -> executable EV -> strategy proposal
    -> fresh book + ledger -> hard risk check -> persisted intent -> venue -> reconcile

Only the injected ``MarketDataProvider``, ``Clock`` and ``ExecutionVenue`` differ by mode.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_edge.config import AppConfig
from kalshi_edge.domain import (
    ZERO,
    AccountSnapshot,
    Market,
    Mode,
    OrderIntent,
    OrderState,
    Outcome,
    ProbabilityEstimate,
    SettlementResult,
    TimeInForce,
    TradeProposal,
)
from kalshi_edge.execution.engine import ExecutionEngine
from kalshi_edge.execution.reconcile import ReconciliationReport, reconcile
from kalshi_edge.execution.store import Store
from kalshi_edge.interfaces import (
    Clock,
    DecisionContext,
    MarketDataProvider,
    ProbabilityEstimator,
    Strategy,
)
from kalshi_edge.monitoring.alerts import AlertSink
from kalshi_edge.risk.exposure import ExposureItem, build_exposure, cluster_keys
from kalshi_edge.risk.killswitch import KillSwitch
from kalshi_edge.risk.manager import HardRiskManager, HealthStatus, RiskContext, RiskDecision
from kalshi_edge.risk.state import RiskState

log = logging.getLogger(__name__)

RISK_STATE_KEY = "risk_state"


@dataclass(slots=True)
class StepReport:
    ts: datetime
    markets_scanned: int = 0
    markets_eligible: int = 0
    proposals: int = 0
    approved: int = 0
    submitted: int = 0
    filled_contracts: Decimal = ZERO
    rejections: dict[str, int] = field(default_factory=dict)
    halted: str | None = None
    reconciled: bool = True
    equity: Decimal = ZERO

    def as_dict(self) -> dict[str, Any]:
        return {"ts": self.ts.isoformat(), "scanned": self.markets_scanned,
                "eligible": self.markets_eligible, "proposals": self.proposals,
                "approved": self.approved, "submitted": self.submitted,
                "filled_contracts": str(self.filled_contracts), "rejections": self.rejections,
                "halted": self.halted, "reconciled": self.reconciled,
                "equity": str(self.equity)}


class TradingLoop:
    def __init__(self, *, config: AppConfig, clock: Clock, market_data: MarketDataProvider,
                 estimator: ProbabilityEstimator | None, strategy: Strategy,
                 risk: HardRiskManager, engine: ExecutionEngine, store: Store,
                 kill_switch: KillSwitch, alerts: AlertSink,
                 submission_gate: Callable[[datetime], bool],
                 feed_health: Callable[[], bool] = lambda: True,
                 clock_skew: Callable[[], float] = lambda: 0.0) -> None:
        self.cfg = config
        self.mode: Mode = config.mode
        self.clock = clock
        self.md = market_data
        self.estimator = estimator
        self.strategy = strategy
        self.risk = risk
        self.engine = engine
        self.store = store
        self.kill = kill_switch
        self.alerts = alerts
        self.submission_gate = submission_gate
        self.feed_health = feed_health
        self.clock_skew = clock_skew
        self.config_hash = config.config_hash()
        self._market_cache: dict[str, Market] = {}
        saved = store.get(RISK_STATE_KEY)
        self.state = (RiskState.from_json(saved) if saved
                      else RiskState.fresh(config.strategy_capital, clock.now()))
        self.last_reconcile: ReconciliationReport | None = None
        self._consecutive_rejects = 0

    # ----------------------------------------------------------------- helpers
    @property
    def model_version(self) -> str:
        if self.estimator is None:
            return "none"
        return f"{self.estimator.model_id}@{self.estimator.model_version}"

    def lookup_market(self, ticker: str) -> Market | None:
        m = self._market_cache.get(ticker)
        if m is None:
            m = self.md.get_market(ticker)
            if m is not None:
                self._market_cache[ticker] = m
        return m

    def mark_equity(self, account: AccountSnapshot) -> Decimal:
        """Conservative mark: cash + open-order reserve + positions at best *bid* (else 0)."""
        value = account.cash + account.open_order_reserved
        for pos in account.positions:
            book = self.md.get_orderbook(pos.ticker)
            bid = book.best_bid(pos.outcome) if book is not None else None
            if bid is not None:
                value += min(pos.quantity, bid.quantity) * bid.price
        return value

    def _estimates(self, market: Market, now: datetime
                   ) -> dict[Outcome, ProbabilityEstimate | None]:
        out: dict[Outcome, ProbabilityEstimate | None] = {}
        for outcome in (Outcome.YES, Outcome.NO):
            est = None
            if self.estimator is not None:
                try:
                    est = self.estimator.estimate(market, outcome, now)
                except Exception:  # model failure => no estimate => no trade (fail closed)
                    log.exception("estimator failed for %s", market.ticker)
                    est = None
            out[outcome] = est
        return out

    def _persist_state(self) -> None:
        self.store.put(RISK_STATE_KEY, self.state.to_json())

    # --------------------------------------------------------------- lifecycle
    def startup(self) -> ReconciliationReport:
        rep = reconcile(self.engine)
        self.last_reconcile = rep
        if not rep.ok:
            self.alerts.alert(self.clock.now(), "reconcile_failed", {"issues": rep.issues})
        return rep

    def settle_resolved(self) -> int:
        """Paper/backtest only: settle virtual positions whose markets have resolved."""
        settle = getattr(self.engine.venue, "settle", None)
        if settle is None:
            return 0
        n = 0
        account = self.engine.venue.get_account()
        for pos in account.positions:
            m = self.md.get_market(pos.ticker)
            if m is None or m.result is None:
                continue
            payout = settle(pos.ticker, m.result)
            self.store.record_settlement(pos.ticker, m.result, self.clock.now(), payout)
            n += 1
        return n

    def settle_ticker(self, ticker: str, result: SettlementResult) -> Decimal:
        """Explicit settlement hook used by the backtest event feed."""
        settle = getattr(self.engine.venue, "settle", None)
        if settle is None:
            raise RuntimeError("venue does not support local settlement")
        payout: Decimal = settle(ticker, result)
        self.store.record_settlement(ticker, result, self.clock.now(), payout)
        return payout

    # -------------------------------------------------------------------- step
    def step(self) -> StepReport:
        """One pipeline pass. Always writes a ``step`` heartbeat event, even when halted."""
        report = StepReport(ts=self.clock.now())
        try:
            self._step(report)
        finally:
            self.store.log_event(report.ts, "step", report.as_dict())
        return report

    def _step(self, report: StepReport) -> None:
        now = report.ts
        self.settle_resolved()

        rec = reconcile(self.engine)
        self.last_reconcile = rep = rec
        report.reconciled = rep.ok
        if not rep.ok:
            self.alerts.alert(now, "reconcile_failed", {"issues": rep.issues[:20]})
        account = rep.account if rep.account is not None else None

        if account is not None:
            equity = self.mark_equity(account)
            report.equity = equity
            self.state.mark(equity, now)
            for stop in self.state.evaluate_stops(self.cfg.risk):
                self.alerts.alert(now, stop, {"equity": str(equity)})
                if stop == "max_drawdown":
                    self.kill.engage("max drawdown stop tripped", now=now)
                self.engine.cancel_all_open(stop)
        self._persist_state()

        kill_state = self.kill.state()
        if kill_state.engaged:
            report.halted = f"kill_switch:{kill_state.reason}"
            try:
                self.engine.cancel_all_open("kill_switch")  # never liquidates positions
            except Exception:
                log.exception("cancel on kill failed")
            return
        if account is None:
            report.halted = "no_account_snapshot"
            return
        if self.state.daily_halt or self.state.weekly_halt or self.state.drawdown_kill:
            report.halted = "loss_stop"
            return

        allow = set(self.cfg.risk.category_allowlist)
        try:
            markets = list(self.md.list_markets())
        except Exception as exc:
            self.alerts.alert(now, "stale_feed", {"error": str(exc)[:200]})
            report.halted = "market_discovery_failed"
            return
        for market in markets:
            report.markets_scanned += 1
            self._market_cache[market.ticker] = market
            if market.category not in allow:
                continue
            report.markets_eligible += 1
            book = self.md.get_orderbook(market.ticker)
            if book is None:
                continue
            ctx = DecisionContext(now=now, market=market, book=book,
                                  estimates=self._estimates(market, now), fees=self.risk.fees,
                                  strategy_equity=min(self.cfg.strategy_capital,
                                                      self.state.current_equity))
            proposals = self.strategy.evaluate(ctx)
            for prop in proposals:
                report.proposals += 1
                self._handle_proposal(prop, report)
        self._persist_state()

    def _handle_proposal(self, prop: TradeProposal, report: StepReport) -> None:
        now = self.clock.now()
        decision_id = uuid.uuid4().hex
        # Final pre-submit check on a freshly fetched book and ledger snapshot.
        fresh_book = self.md.get_orderbook(prop.market.ticker)
        try:
            account = self.engine.venue.get_account()
        except Exception as exc:
            account = None
            log.warning("account fetch failed: %s", exc)
        unresolved = self.store.unresolved_orders()
        if fresh_book is None or account is None:
            decision = RiskDecision(False, ZERO, ZERO, ("missing_fresh_book_or_account",))
        else:
            pending = [ExposureItem(o.ticker, cluster_keys(self.lookup_market(o.ticker),
                                                           o.ticker), o.max_cost, "pending")
                       for o in unresolved]
            exposure = build_exposure(account, self.lookup_market, pending)
            health = HealthStatus(
                reconciled=bool(self.last_reconcile and self.last_reconcile.ok),
                feed_ok=self.feed_health(), unresolved_orders=len(unresolved),
                clock_skew_seconds=self.clock_skew())
            rctx = RiskContext(
                now=now, mode=self.mode, book=fresh_book, account=account, exposure=exposure,
                risk_state=self.state, health=health, kill_engaged=self.kill.engaged,
                submission_permitted=self.submission_gate(now),
                strategy_capital=self.cfg.strategy_capital,
                account_equity=self.mark_equity(account),
                pending_cash=sum((o.max_cost for o in unresolved), ZERO))
            decision = self.risk.check(prop, rctx)
        self._record(decision_id, prop, decision)
        if not decision.approved:
            for r in decision.reasons:
                report.rejections[r] = report.rejections.get(r, 0) + 1
            return
        report.approved += 1
        intent = OrderIntent(
            client_order_id=f"ke-{self.mode.value}-{decision_id[:24]}",
            decision_id=decision_id, mode=self.mode, strategy_id=prop.strategy_id,
            ticker=prop.market.ticker, event_ticker=prop.market.event_ticker,
            outcome=prop.outcome, quantity=decision.quantity, limit_price=prop.limit_price,
            time_in_force=TimeInForce.IMMEDIATE_OR_CANCEL, max_cost=decision.max_loss,
            config_hash=self.config_hash, model_version=self.model_version, created_at=now)
        self.state.orders_today += 1  # counted at submission, whatever the outcome
        self._persist_state()
        result = self.engine.submit(intent)
        report.submitted += 1
        filled = sum((f.quantity for f in result.new_fills), ZERO)
        report.filled_contracts += filled
        self.alerts.alert(now, "order_submitted", {
            "client_order_id": intent.client_order_id, "ticker": intent.ticker,
            "outcome": intent.outcome.value, "qty": str(intent.quantity),
            "limit": str(intent.limit_price), "state": result.state.value,
            "filled": str(filled)})
        if result.state is OrderState.UNKNOWN:
            self.alerts.alert(now, "ambiguous_submit", {"client_order_id": intent.client_order_id})
        if result.state is OrderState.REJECTED:
            self._consecutive_rejects += 1
            if self._consecutive_rejects >= 3:
                self.alerts.alert(now, "repeated_rejects", {"count": self._consecutive_rejects})
        else:
            self._consecutive_rejects = 0

    def _record(self, decision_id: str, prop: TradeProposal, d: RiskDecision) -> None:
        est = prop.estimate
        payload = {
            "proposal": {"qty": prop.quantity, "limit": prop.limit_price,
                         "expected_price": prop.expected_price,
                         "fee_per_contract": prop.fee_per_contract,
                         "edge": prop.edge_per_contract, "ev": prop.ev_per_contract,
                         "book_ts": prop.book_ts, "rationale": prop.rationale},
            "estimate": None if est is None else {
                "q": est.q, "q_safe": est.q_safe, "model": est.model_id,
                "version": est.model_version, "independent": est.independent,
                "calibration": est.calibration_status.value, "as_of": est.as_of},
            "decision": {"qty": d.quantity, "max_loss": d.max_loss, **dict(d.metrics)},
        }
        self.store.record_decision(
            decision_id=decision_id, ts=self.clock.now(), mode=self.mode.value,
            strategy_id=prop.strategy_id.value, ticker=prop.market.ticker,
            outcome=prop.outcome.value, approved=d.approved, reasons=d.reasons,
            payload=payload, config_hash=self.config_hash, model_version=self.model_version)


def default_submission_gate(mode: Mode) -> Callable[[datetime], bool]:
    """Backtest/paper/demo may submit to their (non-production) venues. Live never may,
    unless the caller replaces this with a gate bound to a valid ArmingToken."""
    allowed = mode in (Mode.BACKTEST, Mode.PAPER, Mode.DEMO)
    return lambda _now: allowed


def run_steps(loop: TradingLoop, n: int) -> Sequence[StepReport]:
    return [loop.step() for _ in range(n)]
