"""Event-time replay driving the REAL ``runtime.loop.TradingLoop``.

    SYNTHETIC datasets are engine validation only -- NOT evidence of real-market edge.

What is simulated (and how)
---------------------------
* Time: a ``ReplayClock`` is advanced through a merged, time-ordered timeline of
  settlements < walk-forward refits < decision steps. The decision pipeline (discovery,
  eligibility, estimate, strategy, hard risk, persisted intent, venue, reconcile) is exactly
  the paper/live ``TradingLoop``; only the market data, clock and venue are replay objects.
* Visibility: a market is exposed only once ``open_time <= t``; books are as of
  ``t - quote_delay``; forecasts only if ``collected_at <= t`` (provider-enforced).
* Execution: ``PaperVenue`` IOC orders walk the displayed *ask* depth of the book as of
  ``t + order_latency`` (``feed.fill_book``; the decision book and the fill book can differ),
  with optional ``depth_fraction`` and whole-order ``fill_probability`` haircuts. Fees use the
  same ``FeeSchedule`` (rounded up per fill) as sizing and risk.
* Settlement: ``loop.settle_ticker`` at ``result_ts + settlement_delay``; VOID refunds cost
  (fees are not refunded -- PaperVenue's assumption).
* Estimator (C/D, and informational for A/B): ``WeatherThresholdEstimator`` with an isotonic
  calibrator refit walk-forward (``backtest.walkforward``); status ACCEPTED only when the
  predeclared out-of-sample acceptance criteria pass on a held-out recent fold.
* Equity: the loop's conservative mark (cash + positions at best bid). After a market closes,
  its last book is kept for marking until settlement.

Exact-equivalence prefilter (``BacktestOptions.prefilter``): strategies A/B/C (floor 0.80) can
only act where some side's best ask exceeds the floor, so markets where neither does are
simply not listed to the loop (D uses its own lower floor). Markets with open positions are
always refreshed. Tests assert identical results with and without the prefilter.
"""
# ruff: noqa: E501

from __future__ import annotations

import dataclasses
import json
import logging
import tempfile
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from kalshi_edge.backtest.feed import ReplayFeed, ReplayMarketData, RiskGate
from kalshi_edge.backtest.options import BacktestOptions
from kalshi_edge.backtest.replay_objects import ReplayStore, ReplayVenue
from kalshi_edge.backtest.walkforward import (
    WalkForwardResult,
    cache_key,
    get_walk_forward,
)
from kalshi_edge.config import AppConfig
from kalshi_edge.data.replay import MarketRecord, ReplayDataset
from kalshi_edge.data.synthetic import SYNTHETIC_LABEL, SyntheticConfig, SyntheticWorld
from kalshi_edge.domain import (
    ZERO,
    Market,
    MarketStatus,
    Mode,
    Outcome,
    ProbabilityEstimate,
    SettlementResult,
    StrategyId,
)
from kalshi_edge.execution import ExecutionEngine, PaperVenue, Store
from kalshi_edge.models import IsotonicCalibrator, WeatherThresholdEstimator
from kalshi_edge.monitoring.alerts import AlertSink
from kalshi_edge.risk import HardRiskManager, KillSwitch
from kalshi_edge.risk.exposure import cluster_keys
from kalshi_edge.runtime.clock import ReplayClock
from kalshi_edge.runtime.loop import TradingLoop, default_submission_gate
from kalshi_edge.strategies import make_strategy

_SETTLE, _REFIT, _STEP = 0, 1, 2
_WORLD_CACHE: dict[str, SyntheticWorld] = {}


# ----------------------------------------------------------------------------- estimator
class WalkForwardEstimator:
    """ProbabilityEstimator whose calibrator is swapped at walk-forward refit times.

    ``error_pp`` (stress) shifts q and q_safe by that many percentage points AFTER calibration,
    modelling post-calibration model drift; it is applied at trade time only.
    """

    model_id = "weather-threshold-normal+wf-isotonic"

    def __init__(self, inner: WeatherThresholdEstimator, error_pp: Decimal = ZERO) -> None:
        self.inner = inner
        self.error = error_pp / Decimal(100)
        self.model_version = inner.model_version
        self.status = "none"

    def set_calibrator(self, cal: IsotonicCalibrator | None) -> None:
        self.inner.calibrator = cal
        self.status = "none" if cal is None else cal.status.value

    def estimate(self, market: Market, outcome: Outcome, as_of: datetime
                 ) -> ProbabilityEstimate | None:
        est = self.inner.estimate(market, outcome, as_of)
        if est is None or self.error == ZERO:
            return est
        q = min(Decimal(1), max(ZERO, est.q + self.error))
        qs = est.q_safe
        if qs > ZERO:
            qs = min(q, max(ZERO, qs + self.error))
        return dataclasses.replace(est, q=q, q_safe=min(qs, q))


# ----------------------------------------------------------------------------- run data
@dataclass
class RunData:
    cfg: AppConfig
    opts: BacktestOptions
    scenario: str
    label: str
    dataset: ReplayDataset
    wf: WalkForwardResult | None
    holdout_start: datetime
    steps: list[dict[str, Any]] = field(default_factory=list)
    equity_points: list[tuple[datetime, Decimal]] = field(default_factory=list)
    trades: list[dict[str, Any]] = field(default_factory=list)
    orders: list[dict[str, Any]] = field(default_factory=list)
    rejections: dict[str, int] = field(default_factory=dict)
    settlements: list[dict[str, Any]] = field(default_factory=list)
    final: dict[str, Any] = field(default_factory=dict)
    halts: dict[str, Any] = field(default_factory=dict)
    forced: list[dict[str, Any]] = field(default_factory=list)
    universe_events: list[str] = field(default_factory=list)
    n_markets_universe: int = 0
    excluded: dict[str, int] = field(default_factory=dict)
    timings: dict[str, float] = field(default_factory=dict)
    exposure_max: dict[str, Any] = field(default_factory=dict)
    refit_log: list[dict[str, Any]] = field(default_factory=list)
    lookahead_audit: dict[str, Any] = field(default_factory=dict)


def force_backtest_cfg(cfg: AppConfig) -> AppConfig:
    if cfg.mode is Mode.LIVE or cfg.allow_live_trading:
        raise ValueError("refusing to run a backtest from a live configuration")
    if cfg.mode is Mode.BACKTEST:
        return cfg
    data = cfg.model_dump()
    data["mode"] = Mode.BACKTEST.value
    return AppConfig.model_validate(data)


def build_dataset(cfg: AppConfig, scenario: str, opts: BacktestOptions) -> ReplayDataset:
    bt = cfg.backtest
    if bt.dataset == "synthetic":
        scfg = SyntheticConfig(seed=bt.seed, n_events=bt.n_events, start=bt.start, days=bt.days,
                               scenario=scenario, **opts.synthetic)
        key = json.dumps(vars(scfg), sort_keys=True, default=str)
        world = _WORLD_CACHE.get(key)
        if world is None:
            if len(_WORLD_CACHE) > 4:
                _WORLD_CACHE.clear()
            world = SyntheticWorld(scfg)
            _WORLD_CACHE[key] = world
        return world
    from kalshi_edge.data.recorded import RecordedDataset

    path = bt.recorded_path or (cfg.paths.data_dir / "recorded")
    return RecordedDataset.load(Path(path))


def _floor_for(cfg: AppConfig) -> Decimal:
    if cfg.strategy.id is StrategyId.ABLATION:
        return cfg.strategy.ablation_min_price
    return cfg.risk.min_price_exclusive


def prepare_walk_forward(cfg: AppConfig, opts: BacktestOptions, ds: ReplayDataset
                         ) -> WalkForwardResult | None:
    """Build (or load from cache) the walk-forward calibrators for this dataset/config."""
    bt = cfg.backtest
    rcfg = cfg.risk
    if ds.forecasts is None or not ds.station_map:
        return None
    key = cache_key(ds.provenance.get("config", {}), opts,
                    quote_delay_s=bt.quote_delay_seconds, extra_ticks=bt.spread_extra_ticks,
                    settle_delay_h=bt.settlement_delay_hours,
                    interval_min=bt.decision_interval_minutes,
                    max_forecast_age_s=rcfg.max_forecast_age_seconds,
                    min_seconds_to_close=rcfg.min_seconds_to_close)
    raw_est = WeatherThresholdEstimator(
        ds.forecasts, None, station_map=ds.station_map,
        max_forecast_age=timedelta(seconds=rcfg.max_forecast_age_seconds),
        model_version=opts.model_version)
    return get_walk_forward(
        ds, opts, key=key, estimator=raw_est,
        quote_delay=timedelta(seconds=bt.quote_delay_seconds),
        extra_ticks=bt.spread_extra_ticks,
        settle_delay=timedelta(hours=bt.settlement_delay_hours),
        interval=timedelta(minutes=bt.decision_interval_minutes),
        min_seconds_to_close=rcfg.min_seconds_to_close)


def execute(cfg: AppConfig, opts: BacktestOptions, *, scenario: str, label: str) -> RunData:
    """Run one backtest and return raw run data (no files written)."""
    t_wall0 = time.perf_counter()
    cfg = force_backtest_cfg(cfg)
    bt = cfg.backtest
    ds = build_dataset(cfg, scenario, opts)
    assert ds.start is not None and ds.end is not None
    t_data = time.perf_counter()
    hold_frac = float(bt.holdout_fraction)
    span = (ds.end - ds.start).total_seconds()
    holdout_start = ds.start + timedelta(seconds=span * (1.0 - hold_frac))

    rcfg = cfg.risk
    fees = cfg.fees.schedule()
    interval = timedelta(minutes=bt.decision_interval_minutes)
    quote_delay = timedelta(seconds=bt.quote_delay_seconds)
    latency = timedelta(seconds=bt.order_latency_seconds)
    settle_delay = timedelta(hours=bt.settlement_delay_hours)

    # ---- estimator + walk-forward calibration (weather only)
    estimator: WalkForwardEstimator | None = None
    wf = prepare_walk_forward(cfg, opts, ds)
    if wf is not None and ds.forecasts is not None:
        inner = WeatherThresholdEstimator(
            ds.forecasts, None, station_map=ds.station_map,
            max_forecast_age=timedelta(seconds=rcfg.max_forecast_age_seconds),
            model_version=opts.model_version)
        estimator = WalkForwardEstimator(inner, bt.forecast_error_pp)
    t_wf = time.perf_counter()

    # ---- wiring (same objects as paper mode, replay data/clock/venue)
    clock = ReplayClock(ds.start)
    md = ReplayMarketData()
    feed = ReplayFeed(ds, md, excluded_categories=bt.excluded_categories,
                      quote_delay=quote_delay, order_latency=latency,
                      spread_extra_ticks=bt.spread_extra_ticks, prefilter=opts.prefilter,
                      price_floor=_floor_for(cfg))
    venue = ReplayVenue(mode=Mode.BACKTEST, clock=clock, fees=fees,
                       book_source=lambda tk: feed.fill_book(tk, clock.now()),
                       market_source=md.get_market, starting_cash=cfg.strategy_capital,
                       depth_fraction=opts.depth_fraction, fill_probability=bt.fill_probability,
                       seed=bt.seed)
    store = ReplayStore(":memory:")
    tmpdir = tempfile.TemporaryDirectory(prefix="ke-bt-")
    alert_log = logging.getLogger("kalshi_edge")
    old_level = alert_log.level
    alert_log.setLevel(logging.CRITICAL)
    try:
        loop = TradingLoop(
            config=cfg, clock=clock, market_data=md, estimator=estimator,
            strategy=make_strategy(cfg.strategy, rcfg),
            risk=HardRiskManager(rcfg, fees, slippage_buffer=cfg.strategy.slippage_buffer),
            engine=ExecutionEngine(venue, store, clock),  # type: ignore[arg-type]
            store=store,
            kill_switch=KillSwitch(Path(tmpdir.name) / "KILL_SWITCH"), alerts=AlertSink(store),
            submission_gate=default_submission_gate(Mode.BACKTEST))
        loop.startup()
        run = RunData(cfg, opts, scenario, label, ds, wf, holdout_start)
        _drive(run, loop, venue, feed, md, store, estimator, wf, clock, interval, settle_delay)
        run.timings = {"data_s": t_data - t_wall0, "walk_forward_s": t_wf - t_data,
                       "replay_s": time.perf_counter() - t_wf,
                       "total_s": time.perf_counter() - t_wall0}
        _collect(run, loop, venue, feed, store, settle_delay)
    finally:
        alert_log.setLevel(old_level)
        store.close()
        tmpdir.cleanup()
    run.timings["total_s"] = time.perf_counter() - t_wall0
    return run


# ----------------------------------------------------------------------------- main loop
def _drive(run: RunData, loop: TradingLoop, venue: PaperVenue, feed: ReplayFeed,
           md: ReplayMarketData, store: Store, estimator: WalkForwardEstimator | None,
           wf: WalkForwardResult | None, clock: ReplayClock, interval: timedelta,
           settle_delay: timedelta) -> None:
    ds = run.dataset
    cfg = run.cfg
    opts = run.opts
    timeline: list[tuple[datetime, int, str, Any]] = []
    for t in ds.decision_times(interval):
        timeline.append((t, _STEP, "", None))
    for rec in feed.universe:
        if rec.result is not None and rec.result_ts is not None:
            timeline.append((rec.result_ts + settle_delay, _SETTLE, rec.market.ticker, rec))
    if wf is not None:
        for j, t in enumerate(wf.refit_ts):
            timeline.append((t, _REFIT, f"{j:06d}", j))
    timeline.sort(key=lambda x: (x[0], x[1], x[2]))
    held: set[str] = set()
    acct_last = venue.get_account()
    forced_events: set[str] = set()
    eff_results: dict[str, SettlementResult] = {}
    run.lookahead_audit = {"steps_checked": 0, "violations": 0}
    run.universe_events = sorted({r.event_key for r in feed.universe})
    run.n_markets_universe = len(feed.universe)
    fl = opts.forced_losses
    assert ds.start is not None
    next_trigger = None if fl is None else ds.start + timedelta(days=fl.first_day)
    exposure_by_event_max = ZERO
    exposure_by_cat_max = ZERO
    total_exposure_max = ZERO
    first_halt: tuple[datetime, str] | None = None
    status_now = "none"

    for ts, kind, _tb, payload in timeline:
        clock.set(ts)
        if kind == _REFIT:
            assert wf is not None and estimator is not None
            cal = wf.calibrators[payload]
            estimator.set_calibrator(cal)
            status_now = estimator.status
            run.refit_log.append({**wf.diagnostics[payload], "deployed": status_now})
        elif kind == _SETTLE:
            mrec: MarketRecord = payload
            tk = mrec.market.ticker
            result = mrec.result
            assert result is not None
            if tk in held and mrec.event_key in forced_events:
                result = _adverse_result(venue, tk, result)
                run.forced.append({"ts": ts.isoformat(), "ticker": tk,
                                   "true_result": mrec.result.value if mrec.result else None,
                                   "forced_result": result.value})
            eff_results[tk] = result
            settled_market = replace(mrec.market, status=MarketStatus.SETTLED, result=result,
                                     settlement_ts=ts)
            feed.settle(mrec, settled_market)
            if tk in held:
                loop.settle_ticker(tk, result)
                acct = venue.get_account()
                held = {p.ticker for p in acct.positions}
                acct_last = acct
                run.equity_points.append((ts, loop.mark_equity(acct)))
        else:
            feed.advance(ts, held, _risk_gate(cfg, loop, acct_last, ts, feed.floor, md)
                         if opts.prefilter else None)
            if feed.max_listed_open_time is not None:
                run.lookahead_audit["steps_checked"] += 1
                if feed.max_listed_open_time > ts:
                    run.lookahead_audit["violations"] += 1
            rep = loop.step()
            acct = venue.get_account()
            acct_last = acct
            held = {p.ticker for p in acct.positions}
            exposure = sum((p.cost_basis for p in acct.positions), ZERO)
            by_event: dict[str, Decimal] = defaultdict(lambda: ZERO)
            by_cat: dict[str, Decimal] = defaultdict(lambda: ZERO)
            for p in acct.positions:
                by_event[p.event_ticker] += p.cost_basis
                m = md.get_market(p.ticker)
                keys = cluster_keys(m, p.ticker)
                by_cat[keys[1]] += p.cost_basis
            if by_event:
                exposure_by_event_max = max(exposure_by_event_max, max(by_event.values()))
                exposure_by_cat_max = max(exposure_by_cat_max, max(by_cat.values()))
            total_exposure_max = max(total_exposure_max, exposure)
            if rep.halted and first_halt is None:
                first_halt = (ts, rep.halted)
            for k, v in rep.rejections.items():
                run.rejections[k] = run.rejections.get(k, 0) + v
            run.steps.append({
                "ts": ts, "equity": rep.equity, "cash": acct.cash, "exposure": exposure,
                "n_positions": len(acct.positions), "scanned": rep.markets_scanned,
                "eligible": rep.markets_eligible, "proposals": rep.proposals,
                "approved": rep.approved, "submitted": rep.submitted,
                "filled": rep.filled_contracts, "halted": rep.halted or "",
                "reconciled": rep.reconciled, "calib_status": status_now})
            if rep.equity:
                run.equity_points.append((ts, rep.equity))
            if fl is not None and next_trigger is not None and ts >= next_trigger:
                chosen = _pick_clusters(acct, md, fl.n, fl.mode)
                if chosen:
                    forced_events |= set(chosen["events"])
                    run.forced.append({"ts": ts.isoformat(), "trigger": True, **{
                        k: (str(v) if isinstance(v, Decimal) else v)
                        for k, v in chosen.items() if k != "events"},
                        "events": sorted(chosen["events"])})
                next_trigger = next_trigger + timedelta(days=fl.every_days)
    run.exposure_max = {"max_event_cluster_exposure": exposure_by_event_max,
                        "max_catalyst_cluster_exposure": exposure_by_cat_max,
                        "max_total_exposure": total_exposure_max}
    if first_halt is not None:
        run.halts = {"first_halt_ts": first_halt[0].isoformat(), "reason": first_halt[1]}
    st = loop.state
    run.halts["latched"] = {"daily_halt": st.daily_halt, "weekly_halt": st.weekly_halt,
                            "drawdown_kill": st.drawdown_kill}
    run.settlements = list(venue.settlement_log)
    run.final["eff_results"] = eff_results


def _risk_gate(cfg: AppConfig, loop: TradingLoop, acct: Any, ts: datetime, floor: Decimal,
               md: ReplayMarketData) -> RiskGate:
    """Single-reason certain-rejection shortcuts, derived from static ceilings only.

    ``base`` in the risk manager is <= strategy_capital, so room computed with the capital
    ceiling is an upper bound: if even that cannot fit one contract (cost > ``floor``), the
    proposal is rejected for certain. ``orders_today`` is only trusted on the same UTC day
    (the loop rolls it inside ``step`` after this check).
    """
    r = cfg.risk
    cap = cfg.strategy_capital
    st = loop.state
    if st.day == ts.date().isoformat() and st.orders_today >= r.max_orders_per_day:
        return RiskGate(block_all=True)
    total = ZERO
    by_cluster: dict[str, Decimal] = defaultdict(lambda: ZERO)
    for p in acct.positions:
        total += p.cost_basis
        for k in cluster_keys(md.get_market(p.ticker), p.ticker):
            by_cluster[k] += p.cost_basis
    if cap * r.aggregate_open_loss_frac - total < floor:
        return RiskGate(block_all=True)
    if acct.cash - r.fee_reserve < floor:
        return RiskGate(block_all=True)
    limit = cap * r.cluster_loss_frac
    blocked = frozenset(k for k, v in by_cluster.items() if limit - v < floor)
    return RiskGate(False, blocked)


def _adverse_result(venue: PaperVenue, ticker: str, true_result: SettlementResult
                    ) -> SettlementResult:
    pos = [p for p in venue.get_account().positions if p.ticker == ticker]
    if not pos:
        return true_result
    biggest = max(pos, key=lambda p: p.cost_basis)
    return SettlementResult.NO if biggest.outcome is Outcome.YES else SettlementResult.YES


def _pick_clusters(acct: Any, md: ReplayMarketData, n: int, mode: str
                   ) -> dict[str, Any] | None:
    agg: dict[str, Decimal] = defaultdict(lambda: ZERO)
    members: dict[str, set[str]] = defaultdict(set)
    for p in acct.positions:
        keys = cluster_keys(md.get_market(p.ticker), p.ticker)
        k = keys[0] if mode == "event" else keys[1]
        agg[k] += p.cost_basis
        members[k].add(p.event_ticker)
    if not agg:
        return None
    top = sorted(agg.items(), key=lambda kv: (-kv[1], kv[0]))[:n]
    events: set[str] = set()
    for k, _ in top:
        events |= members[k]
    return {"clusters": [k for k, _ in top], "exposure": sum((v for _, v in top), ZERO),
            "events": events}


# ----------------------------------------------------------------------------- collection
def _collect(run: RunData, loop: TradingLoop, venue: PaperVenue, feed: ReplayFeed,
             store: Store, settle_delay: timedelta) -> None:
    eff: dict[str, SettlementResult] = run.final.pop("eff_results")
    conn = store._conn  # read-only SELECTs; Store has no decision-listing accessor
    decisions = {}
    for row in conn.execute("SELECT decision_id, payload FROM decisions WHERE approved=1"):
        decisions[row["decision_id"]] = json.loads(row["payload"])
    n_decisions = conn.execute("SELECT COUNT(*) AS n FROM decisions").fetchone()["n"]
    fills_by_order: dict[str, list[Any]] = defaultdict(list)
    for f in store.fills():
        fills_by_order[f.client_order_id].append(f)
    total_cost = ZERO
    total_fee = ZERO
    total_payout = ZERO
    for o in store.orders():
        fl = fills_by_order.get(o.client_order_id, [])
        filled_q = sum((f.quantity for f in fl), ZERO)
        run.orders.append({"client_order_id": o.client_order_id, "ticker": o.ticker,
                           "qty": o.quantity, "filled": filled_q, "state": o.state.value,
                           "ts": o.created_at})
        if not fl:
            continue
        rec = feed.by_ticker[o.ticker]
        notional = sum((f.quantity * f.price for f in fl), ZERO)
        fee = sum((f.fee for f in fl), ZERO)
        ts_fill = min(f.ts for f in fl)
        result = eff.get(o.ticker)
        payout: Decimal | None
        if result is None:
            payout = None
        elif result is SettlementResult.VOID:
            payout = notional
        else:
            payout = filled_q if result.value == o.outcome.value else ZERO
        dec = decisions.get(o.decision_id, {})
        est = dec.get("estimate") or {}
        q = Decimal(est["q"]) if est.get("q") is not None else None
        q_safe = Decimal(est["q_safe"]) if est.get("q_safe") is not None else None
        vwap = notional / filled_q
        fee_pc = fee / filled_q
        pnl = None if payout is None else payout - notional - fee
        total_cost += notional + fee
        total_fee += fee
        if payout is not None:
            total_payout += payout
        settle_ts = None if result is None or rec.result_ts is None else rec.result_ts + settle_delay
        run.trades.append({
            "strategy": o.strategy_id, "ticker": o.ticker, "event": o.event_ticker,
            "category": rec.category, "outcome": o.outcome.value, "qty": filled_q,
            "req_qty": o.quantity, "vwap": vwap, "fee": fee, "fee_pc": fee_pc,
            "cost": notional + fee, "q": q, "q_safe": q_safe,
            "ev_est_pc": None if q is None else q - vwap - fee_pc,
            "edge_est_pc": None if q_safe is None else q_safe - vwap - fee_pc
            - run.cfg.strategy.slippage_buffer,
            "breakeven": vwap + fee_pc,
            "calib_status": est.get("calibration"), "fill_ts": ts_fill,
            "horizon_h": (rec.market.close_time - ts_fill).total_seconds() / 3600.0,
            "result": None if result is None else result.value,
            "true_result": None if rec.result is None else rec.result.value,
            "settle_ts": settle_ts, "payout": payout, "pnl": pnl,
            "lock_h": None if settle_ts is None else (settle_ts - ts_fill).total_seconds() / 3600.0,
            "period": "holdout" if ts_fill >= run.holdout_start else "in_sample",
        })
    acct = venue.get_account()
    unresolved_cost = sum((p.cost_basis for p in acct.positions), ZERO)
    run.final.update({
        "start_cash": run.cfg.strategy_capital, "venue_cash": venue.cash,
        "total_cost_incl_fees": total_cost, "total_fees": total_fee,
        "total_payouts": total_payout, "venue_realized_pnl": venue.realized_pnl,
        "venue_fees_paid": venue.fees_paid, "n_decisions": int(n_decisions),
        "unresolved_cost": unresolved_cost, "n_unresolved_positions": len(acct.positions),
        "unresolved_mark": loop.mark_equity(acct) - acct.cash,
        "final_equity_mark": loop.mark_equity(acct),
        "risk_state": loop.state.to_json(),
        "reconcile_ok": bool(loop.last_reconcile and loop.last_reconcile.ok),
        "reconcile_issues": (loop.last_reconcile.issues[:5] if loop.last_reconcile else []),
        "provenance_label": (SYNTHETIC_LABEL if run.dataset.kind == "synthetic"
                             else "recorded data"),
    })
