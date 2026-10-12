"""Backtest engine invariants. All data here is SYNTHETIC (engine validation only)."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import numpy as np
import pytest

from kalshi_edge.backtest import BacktestOptions, ForcedLossSpec, run_backtest
from kalshi_edge.backtest.engine import execute
from kalshi_edge.backtest.feed import ReplayFeed, ReplayMarketData
from kalshi_edge.backtest.metrics import compute_metrics, event_bootstrap, metrics_hash
from kalshi_edge.data.synthetic import SyntheticConfig, SyntheticWorld
from kalshi_edge.domain import SettlementResult


def test_determinism_same_seed_same_metrics_hash(small_cfg_factory: Any) -> None:
    cfg = small_cfg_factory("naive-80")
    a = run_backtest(cfg, scenario="favorite_underpriced")
    b = run_backtest(cfg, scenario="favorite_underpriced")
    assert a.metrics["activity"]["n_orders_submitted"] > 0
    assert metrics_hash(a.metrics) == metrics_hash(b.metrics)
    other = run_backtest(small_cfg_factory("naive-80", seed=7), scenario="favorite_underpriced")
    assert metrics_hash(other.metrics) != metrics_hash(a.metrics)


def test_accounting_identity_and_labels(small_cfg_factory: Any) -> None:
    cfg = small_cfg_factory("scaled-80")
    res = run_backtest(cfg, scenario="favorite_overpriced")
    acc = res.metrics["accounting"]
    assert acc["identity_ok"] and acc["identity_residual"] == 0
    # final cash = start - costs(incl. fees) + payouts
    assert acc["venue_cash"] == pytest.approx(
        acc["start_cash"] - acc["total_cost_incl_fees"] + acc["total_payouts"])
    assert "NO EVIDENCE YET" in res.metrics["evidence_verdict"]
    assert "SYNTHETIC" in res.metrics["label"]


def test_void_refunds_cost_but_not_fees(small_cfg_factory: Any) -> None:
    cfg = small_cfg_factory("naive-80")
    run = execute(cfg, BacktestOptions(synthetic={"void_rate": 1.0}), scenario="efficient",
                  label="void")
    assert run.trades
    assert all(t["result"] == "void" for t in run.trades)
    m = compute_metrics(run)
    assert m["capital"]["net_pnl"] == pytest.approx(-m["capital"]["fees_paid"])
    assert m["accounting"]["identity_ok"]


def test_no_lookahead_markets_and_forecasts(small_cfg_factory: Any) -> None:
    cfg = small_cfg_factory("naive-80")
    run = execute(cfg, BacktestOptions(), scenario="efficient", label="la")
    assert run.lookahead_audit["steps_checked"] > 0
    assert run.lookahead_audit["violations"] == 0
    ds = run.dataset
    assert isinstance(ds, SyntheticWorld)
    prov = ds.forecasts
    assert prov is not None
    rec = next(r for r in ds.records if r.category == "weather")
    key = f"{ds.station_map[rec.market.series_ticker]}:max_temp_f:{rec.market.strike['event_date']}"
    mid = rec.market.open_time + timedelta(hours=10)
    obs = prov.observations(key, mid)
    assert obs and all(o.collected_at <= mid for o in obs)
    later = prov.observations(key, rec.market.close_time)
    assert len(later) > len(obs)
    # nothing published before open
    assert prov.observations(key, rec.market.open_time - timedelta(minutes=1)) == []


def test_feed_lists_only_opened_markets() -> None:
    world = SyntheticWorld(SyntheticConfig(n_events=40, days=20))
    md = ReplayMarketData()
    feed = ReplayFeed(world, md, excluded_categories=(), quote_delay=timedelta(0),
                      order_latency=timedelta(seconds=2), spread_extra_ticks=0,
                      prefilter=False, price_floor=Decimal("0.8"))
    assert world.start is not None
    for h in range(0, 24 * 10, 7):
        t = world.start + timedelta(hours=h)
        feed.advance(t, set())
        for m in md.list_markets():
            assert m.open_time <= t < m.close_time
            assert md.get_orderbook(m.ticker) is not None
    t_last = world.start + timedelta(hours=7 * ((24 * 10 - 1) // 7))
    assert all(m.open_time <= t_last for m in md.markets.values())
    future = [r for r in world.records if r.market.open_time > t_last]
    assert future and all(md.get_market(r.market.ticker) is None for r in future)


def test_latency_uses_later_book() -> None:
    world = SyntheticWorld(SyntheticConfig(n_events=30, days=15, book_minutes=60))
    md = ReplayMarketData()
    feed = ReplayFeed(world, md, excluded_categories=(), quote_delay=timedelta(0),
                      order_latency=timedelta(minutes=30), spread_extra_ticks=0,
                      prefilter=False, price_floor=Decimal("0.8"))
    rec = world.records[0]
    now = rec.market.open_time + timedelta(hours=5, minutes=45)
    fill = feed.fill_book(rec.market.ticker, now)
    assert fill == world.book_at(rec.market.ticker, now + timedelta(minutes=30))
    # latency crossed an hour boundary: content equals the next hour's, not the decision hour's
    nxt = world.book_at(rec.market.ticker, now + timedelta(minutes=30))
    assert nxt is not None and fill is not None
    assert (fill.yes_bids, fill.no_bids) == (nxt.yes_bids, nxt.no_bids)


def test_prefilter_and_replay_objects_are_output_equivalent(small_cfg_factory: Any) -> None:
    cfg = small_cfg_factory("naive-80", n_events=200, days=45)
    a = execute(cfg, BacktestOptions(prefilter=True), scenario="favorite_underpriced", label="a")
    b = execute(cfg, BacktestOptions(prefilter=False), scenario="favorite_underpriced", label="b")
    key = ("fill_ts", "ticker", "outcome", "qty", "vwap", "fee", "pnl")
    ta = [tuple(t[k] for k in key) for t in a.trades]
    tb = [tuple(t[k] for k in key) for t in b.trades]
    assert ta and ta == tb
    assert a.final["venue_cash"] == b.final["venue_cash"]


def test_c_does_not_trade_without_accepted_calibration(small_cfg_factory: Any) -> None:
    cfg = small_cfg_factory("positive-ev-80")
    res = run_backtest(cfg, scenario="favorite_underpriced")
    # 150 events / 40 days can never satisfy the predeclared 200-independent-event criterion
    assert res.metrics["activity"]["n_orders_submitted"] == 0
    assert "accepted" not in res.metrics["estimator"]["refit_status_counts"]


def test_walk_forward_labels_known_before_refit(small_cfg_factory: Any) -> None:
    cfg = small_cfg_factory("naive-80", n_events=700, days=90, decision_interval_minutes=360)
    run = execute(cfg, BacktestOptions(fold_days=20), scenario="favorite_underpriced", label="wf")
    assert run.wf is not None
    checked = 0
    for d in run.wf.diagnostics:
        if "train_max_label_epoch" in d and "val_min_label_epoch" in d:
            assert d["val_max_label_epoch"] < d["refit_epoch"]
            assert d["train_max_label_epoch"] < d["val_min_label_epoch"]
            checked += 1
    assert checked > 0


def test_forced_adverse_flips_results(small_cfg_factory: Any) -> None:
    cfg = small_cfg_factory("naive-80", n_events=200, days=60)
    opts = BacktestOptions(forced_losses=ForcedLossSpec(n=2, every_days=10, first_day=10))
    run = execute(cfg, opts, scenario="efficient", label="forced")
    flips = [f for f in run.forced if "forced_result" in f]
    assert flips
    for t in run.trades:
        if t["ticker"] in {f["ticker"] for f in flips} and t["result"] != "void":
            assert t["pnl"] < 0


def test_bootstrap_resamples_events_not_rows() -> None:
    # event A: 100 trades of +1 (total +100); event B: 1 trade of -100.
    ids = ["A"] * 100 + ["B"]
    pnl = [1.0] * 100 + [-100.0]
    out = event_bootstrap(ids, pnl, ["A", "B"], n_resample=500, seed=1)
    lo, hi = out["total_pnl_ci95"]
    # whole-event resampling of 2 events can only total {-200, 0, +200}
    assert lo == -200.0 and hi == 200.0
    # determinism
    assert out == event_bootstrap(ids, pnl, ["A", "B"], n_resample=500, seed=1)
    assert out["n_traded_events"] == 2
    # untraded universe events contribute zeros
    out2 = event_bootstrap(ids, pnl, ["A", "B", "C", "D"], n_resample=200, seed=1)
    assert out2["n_universe_events"] == 4
    totals_possible = np.arange(-400, 401, 100)
    assert out2["total_pnl_ci95"][0] in totals_possible


def test_settlement_result_enum_roundtrip() -> None:
    assert SettlementResult("void") is SettlementResult.VOID
