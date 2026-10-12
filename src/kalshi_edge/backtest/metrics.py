"""Metrics for one backtest run (pure functions of ``RunData``) and event-clustered bootstrap.

All dollar figures are net of fees. ``trades`` are one row per filled order.
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from typing import Any

import numpy as np
import pandas as pd

from kalshi_edge.backtest.engine import RunData
from kalshi_edge.backtest.walkforward import evaluate_estimator
from kalshi_edge.domain import ZERO


def _f(x: Any) -> float | None:
    if x is None:
        return None
    if isinstance(x, float) and math.isnan(x):
        return None
    return float(x)


def clean(obj: Any) -> Any:
    """JSON-safe recursive conversion (Decimal->str-free float, datetime->iso, nan->None)."""
    if isinstance(obj, dict):
        return {str(k): clean(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [clean(v) for v in obj]
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        v = float(obj)
        return None if math.isnan(v) or math.isinf(v) else v
    if isinstance(obj, float):
        return None if math.isnan(obj) or math.isinf(obj) else obj
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


def metrics_hash(metrics: dict[str, Any]) -> str:
    blob = json.dumps(clean(metrics), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def trades_frame(run: RunData) -> pd.DataFrame:
    rows = []
    for t in run.trades:
        rows.append({k: (float(v) if isinstance(v, Decimal) else v) for k, v in t.items()})
    df = pd.DataFrame(rows)
    if df.empty:
        cols = ["strategy", "ticker", "event", "category", "outcome", "qty", "vwap", "fee",
                "cost", "q", "q_safe", "ev_est_pc", "edge_est_pc", "breakeven", "horizon_h",
                "result", "pnl", "lock_h", "period", "fill_ts", "settle_ts"]
        return pd.DataFrame(columns=cols)
    return df.sort_values(["fill_ts", "ticker"]).reset_index(drop=True)


# ----------------------------------------------------------------------------- bootstrap
def event_bootstrap(event_ids: list[str], pnl: list[float], universe_events: list[str],
                    *, n_resample: int, seed: int) -> dict[str, Any]:
    """Resample EVENTS (not rows). Untraded universe events contribute 0.

    Returns CIs for total P&L over the universe and for mean P&L per universe event / per
    traded event. Every trade of a sampled event moves together.
    """
    per_event: dict[str, float] = defaultdict(float)
    for e, p in zip(event_ids, pnl, strict=True):
        per_event[e] += p
    uni = sorted(set(universe_events) | set(per_event))
    vals = np.array([per_event.get(e, 0.0) for e in uni])
    traded = np.array([per_event[e] for e in sorted(per_event)])
    rng = np.random.default_rng(seed)
    out: dict[str, Any] = {"n_universe_events": len(uni), "n_traded_events": len(traded),
                           "n_resamples": n_resample, "seed": seed}
    if len(uni) == 0 or len(traded) == 0:
        out.update({"total_pnl_ci95": None, "mean_pnl_per_event_ci95": None,
                    "mean_pnl_per_traded_event_ci95": None, "p_total_pnl_gt_0": None})
        return out
    idx = rng.integers(0, len(vals), size=(n_resample, len(vals)))
    totals = vals[idx].sum(axis=1)
    idx2 = rng.integers(0, len(traded), size=(n_resample, len(traded)))
    means_traded = traded[idx2].mean(axis=1)
    lo, hi = np.percentile(totals, [2.5, 97.5])
    lo2, hi2 = np.percentile(means_traded, [2.5, 97.5])
    out.update({
        "total_pnl_ci95": [float(lo), float(hi)],
        "mean_pnl_per_event_ci95": [float(lo / len(vals)), float(hi / len(vals))],
        "mean_pnl_per_traded_event_ci95": [float(lo2), float(hi2)],
        "p_total_pnl_gt_0": float((totals > 0).mean()),
        "ci_excludes_zero": bool(lo > 0 or hi < 0),
    })
    return out


# ----------------------------------------------------------------------------- helpers
def _streaks(losses: list[bool]) -> dict[str, Any]:
    runs: list[int] = []
    cur = 0
    for lost in losses:
        if lost:
            cur += 1
        else:
            if cur:
                runs.append(cur)
            cur = 0
    if cur:
        runs.append(cur)
    return {"max_losing_streak": max(runs) if runs else 0,
            "mean_losing_streak": float(np.mean(runs)) if runs else 0.0,
            "n_streaks_ge_3": sum(1 for r in runs if r >= 3),
            "n_streaks": len(runs),
            "loss_fraction": float(np.mean(losses)) if losses else None}


def _drawdown(eq: pd.Series) -> dict[str, Any]:
    if eq.empty:
        return {"max_drawdown_usd": 0.0, "max_drawdown_pct": 0.0, "longest_underwater_days": 0.0}
    vals = eq.to_numpy(dtype=float)
    peak = np.maximum.accumulate(vals)
    dd = peak - vals
    i = int(np.argmax(dd))
    pct = dd / np.where(peak > 0, peak, 1.0)
    under = peak > vals + 1e-9
    longest = 0.0
    start_t = None
    idx = eq.index
    for k, u in enumerate(under):
        if u and start_t is None:
            start_t = idx[k]
        if not u and start_t is not None:
            longest = max(longest, (idx[k] - start_t).total_seconds() / 86400.0)
            start_t = None
    if start_t is not None:
        longest = max(longest, (idx[-1] - start_t).total_seconds() / 86400.0)
    return {"max_drawdown_usd": float(dd[i]), "max_drawdown_pct": float(pct.max()),
            "longest_underwater_days": float(longest)}


def equity_series(run: RunData) -> pd.Series:
    pts: dict[datetime, float] = {}
    for ts, eq in sorted(run.equity_points, key=lambda x: x[0]):
        pts[ts] = float(eq)
    if not pts:
        return pd.Series(dtype=float)
    s = pd.Series(pts)
    s.index = pd.DatetimeIndex(s.index)
    return s


def _group_stats(df: pd.DataFrame, by: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if df.empty:
        return out
    for key, g in df.groupby(by, dropna=False, observed=True):
        s = g[g["pnl"].notna()]
        out[str(key)] = {
            "n_trades": len(g), "n_settled": len(s),
            "net_pnl": float(s["pnl"].sum()), "cost": float(g["cost"].sum()),
            "win_rate": float((s["pnl"] > 0).mean()) if len(s) else None,
            "roi_on_cost": float(s["pnl"].sum() / s["cost"].sum()) if len(s) and
            s["cost"].sum() > 0 else None}
    return out


def _slice_stats(df: pd.DataFrame, capital: float) -> dict[str, Any]:
    s = df[df["pnl"].notna()]
    if s.empty:
        return {"n_trades": len(df), "n_settled": 0, "net_pnl": 0.0}
    cost = float(s["cost"].sum())
    wins = s[s["pnl"] > 0]["pnl"]
    losses = s[s["pnl"] <= 0]["pnl"]
    return {
        "n_trades": len(df), "n_settled": len(s), "net_pnl": float(s["pnl"].sum()),
        "roi_on_capital": float(s["pnl"].sum()) / capital,
        "return_on_cost": float(s["pnl"].sum()) / cost if cost > 0 else None,
        "win_rate": float((s["pnl"] > 0).mean()),
        "payoff_ratio": (float(wins.mean() / abs(losses.mean()))
                         if len(wins) and len(losses) and losses.mean() != 0 else None),
        "n_events": int(s["event"].nunique())}


def compute_metrics(run: RunData) -> dict[str, Any]:
    cfg = run.cfg
    cap = float(cfg.strategy_capital)
    df = trades_frame(run)
    settled = df[df["pnl"].notna()] if not df.empty else df
    fin = run.final
    ds = run.dataset
    m: dict[str, Any] = {
        "label": fin["provenance_label"],
        "dataset_kind": ds.kind, "scenario": run.scenario, "strategy": cfg.strategy.id.value,
        "sizing": cfg.strategy.sizing,
        "evidence_verdict": ("NO EVIDENCE YET: synthetic data is engine validation only"
                             if ds.kind == "synthetic" else
                             "INCONCLUSIVE unless sample/provenance criteria in docs/RESULTS.md met"),
    }
    # ---- capital & P&L
    net = float(settled["pnl"].sum()) if not settled.empty else 0.0
    total_cost = float(df["cost"].sum()) if not df.empty else 0.0
    fees = float(df["fee"].sum()) if not df.empty else 0.0
    m["capital"] = {
        "designated": cap, "final_cash": float(fin["venue_cash"]),
        "final_bankroll": float(fin["venue_cash"] + fin["unresolved_mark"]),
        "net_pnl": net, "fees_paid": fees, "gross_pnl_before_fees": net + fees,
        "roi_on_designated_capital": net / cap,
        "total_capital_deployed_cost": total_cost,
        "return_on_deployed_capital": net / total_cost if total_cost > 0 else None,
        "realized_pnl": net,
        "unresolved_cost_at_end": float(fin["unresolved_cost"]),
        "unresolved_pnl_marked": float(fin["unresolved_mark"] - fin["unresolved_cost"]),
        "n_unresolved_positions": fin["n_unresolved_positions"],
    }
    # time-weighted deployed capital
    if not settled.empty and run.steps:
        t0 = run.steps[0]["ts"]
        t1 = max(settled["settle_ts"])
        hours = max((t1 - t0).total_seconds() / 3600.0, 1.0)
        avg_dep = float((settled["cost"] * settled["lock_h"]).sum() / hours)
        m["capital"]["avg_deployed_capital"] = avg_dep
        m["capital"]["pnl_per_avg_deployed_capital"] = net / avg_dep if avg_dep > 0 else None
    # ---- accounting identity (Decimal)
    expected_cash = fin["start_cash"] - fin["total_cost_incl_fees"] + fin["total_payouts"]
    unresolved_cost = fin["unresolved_cost"]
    diff = fin["venue_cash"] - expected_cash
    m["accounting"] = {
        "start_cash": float(fin["start_cash"]), "total_cost_incl_fees": float(fin["total_cost_incl_fees"]),
        "total_fees": float(fin["total_fees"]), "total_payouts": float(fin["total_payouts"]),
        "expected_cash_identity": float(expected_cash), "venue_cash": float(fin["venue_cash"]),
        "unresolved_cost_basis": float(unresolved_cost),
        "identity_residual": float(diff), "identity_ok": diff == ZERO,
        "venue_realized_pnl": float(fin["venue_realized_pnl"]),
        "reconcile_ok_at_end": fin["reconcile_ok"],
    }
    # ---- activity
    n_sub = len(run.orders)
    n_fill = sum(1 for o in run.orders if o["filled"] > 0)
    req = sum(float(o["qty"]) for o in run.orders)
    got = sum(float(o["filled"]) for o in run.orders)
    wins = settled[settled["pnl"] > 0]["pnl"] if not settled.empty else pd.Series(dtype=float)
    loss = settled[settled["pnl"] <= 0]["pnl"] if not settled.empty else pd.Series(dtype=float)
    m["activity"] = {
        "n_steps": len(run.steps), "n_orders_submitted": n_sub, "n_orders_filled": n_fill,
        "order_fill_ratio": n_fill / n_sub if n_sub else None,
        "contract_fill_ratio": got / req if req else None,
        "n_trades_settled": len(settled),
        "win_rate": float((settled["pnl"] > 0).mean()) if len(settled) else None,
        "avg_win": float(wins.mean()) if len(wins) else None,
        "avg_loss": float(loss.mean()) if len(loss) else None,
        "payoff_ratio": (float(wins.mean() / abs(loss.mean()))
                         if len(wins) and len(loss) and loss.mean() != 0 else None),
        "avg_capital_lock_hours": float(settled["lock_h"].mean()) if len(settled) else None,
        "cost_weighted_lock_hours": (float((settled["lock_h"] * settled["cost"]).sum()
                                           / settled["cost"].sum()) if len(settled) else None),
        "n_decisions_recorded": fin["n_decisions"],
        "steps_halted": sum(1 for s in run.steps if s["halted"]),
        "halt": run.halts,
        "rejections_top": dict(sorted(run.rejections.items(), key=lambda kv: -kv[1])[:8]),
    }
    # ---- EV: expected vs realized, edge, frequency
    ev = settled[settled["ev_est_pc"].notna()] if not settled.empty else settled
    if len(ev):
        exp_total = float((ev["ev_est_pc"] * ev["qty"]).sum())
        m["ev"] = {
            "n_trades_with_estimate": len(ev),
            "expected_ev_total": exp_total, "realized_pnl_same_trades": float(ev["pnl"].sum()),
            "expected_ev_per_contract": exp_total / float(ev["qty"].sum()),
            "realized_pnl_per_contract": float(ev["pnl"].sum() / ev["qty"].sum()),
            "avg_q_estimate": float(ev["q"].mean()), "avg_breakeven_price_plus_fee":
            float(ev["breakeven"].mean()),
            "realized_win_frequency": float((ev["pnl"] > 0).mean()),
            "avg_edge_estimate": (float(ev["edge_est_pc"].mean()) if ev["edge_est_pc"].notna().any()
                                  else None)}
    else:
        m["ev"] = {"n_trades_with_estimate": 0}
    if len(settled):
        m["ev"]["avg_entry_price"] = float(settled["vwap"].mean())
        m["ev"]["avg_breakeven_all_trades"] = float(settled["breakeven"].mean())
        m["ev"]["realized_win_frequency_all_trades"] = float((settled["pnl"] > 0).mean())
    # ---- equity-based risk
    eq = equity_series(run)
    m["risk"] = _drawdown(eq)
    if not eq.empty:
        day = eq.groupby(eq.index.date).last()
        day = pd.concat([pd.Series([cap]), day.reset_index(drop=True)])
        dpnl = day.diff().dropna()
        wk = eq.groupby([eq.index.isocalendar().year, eq.index.isocalendar().week]).last()
        wk = pd.concat([pd.Series([cap]), wk.reset_index(drop=True)])
        wpnl = wk.diff().dropna()
        m["risk"].update({"worst_day_pnl": float(dpnl.min()), "worst_week_pnl": float(wpnl.min()),
                          "best_day_pnl": float(dpnl.max()),
                          "min_equity": float(eq.min()), "max_equity": float(eq.max())})
    em = run.exposure_max
    m["risk"].update({
        "max_simultaneous_correlated_loss_event_cluster": float(em["max_event_cluster_exposure"]),
        "max_catalyst_cluster_exposure": float(em["max_catalyst_cluster_exposure"]),
        "max_total_open_exposure": float(em["max_total_exposure"]),
        "max_simultaneous_correlated_loss_pct_capital":
        float(em["max_event_cluster_exposure"]) / cap})
    if len(settled):
        order = settled.sort_values("settle_ts")
        m["risk"]["streaks"] = _streaks([bool(x) for x in (order["pnl"] <= 0).tolist()])
    # ---- bootstrap (event-clustered)
    if len(settled):
        m["bootstrap_event_clustered"] = event_bootstrap(
            settled["event"].tolist(), settled["pnl"].tolist(), run.universe_events,
            n_resample=run.opts.bootstrap_resamples, seed=run.cfg.backtest.seed)
    else:
        m["bootstrap_event_clustered"] = {"n_traded_events": 0, "total_pnl_ci95": None}
    # ---- breakdowns
    m["by_category"] = _group_stats(df, "category") if not df.empty else {}
    m["by_outcome_side"] = _group_stats(df, "outcome") if not df.empty else {}
    if not df.empty:
        hb = pd.cut(df["horizon_h"], [0, 6, 12, 24, 1e9], labels=["<6h", "6-12h", "12-24h", ">=24h"])
        df2 = df.assign(horizon_bucket=hb.astype(str))
        m["by_horizon_to_close"] = _group_stats(df2, "horizon_bucket")
        pb = pd.cut(df["vwap"], [0.0, 0.80, 0.85, 0.90, 0.95, 1.0],
                    labels=["<=0.80", "0.80-0.85", "0.85-0.90", "0.90-0.95", "0.95-1.00"])
        m["by_entry_price"] = _group_stats(df.assign(price_bin=pb.astype(str)), "price_bin")
        mon = df["fill_ts"].map(lambda t: t.strftime("%Y-%m"))
        m["by_month"] = _group_stats(df.assign(month=mon), "month")
        m["by_market_top"] = _market_extremes(df)
    m["by_period"] = {p: _slice_stats(df[df["period"] == p], cap) if not df.empty else {}
                      for p in ("in_sample", "holdout")}
    m["by_period"]["holdout_start"] = run.holdout_start.isoformat()
    # ---- estimator / walk-forward
    if run.wf is not None:
        wf_eval = evaluate_estimator(run.wf, run.holdout_start)
        statuses = [d["status"] for d in run.wf.diagnostics]
        m["estimator"] = {
            "model": f"weather-threshold-normal + wf-isotonic ({run.opts.model_version})",
            "n_refits": len(statuses),
            "refit_status_counts": {s: statuses.count(s) for s in sorted(set(statuses))},
            "first_accepted_refit": next((d["refit_ts"] for d in run.wf.diagnostics
                                          if d["status"] == "accepted"), None),
            "evaluation": wf_eval,
            "forecast_error_pp_applied": float(run.cfg.backtest.forecast_error_pp),
        }
        m["walk_forward_refits"] = run.refit_log
    else:
        m["estimator"] = {"model": None, "note": "no external-signal estimator for this dataset"}
    m["universe"] = {"n_events": len(run.universe_events), "n_markets": run.n_markets_universe,
                     "excluded_rows": run.dataset.excluded}
    if run.forced:
        m["stress_forced_losses"] = {"n_flips": sum(1 for f in run.forced if "forced_result" in f),
                                     "triggers": [f for f in run.forced if f.get("trigger")]}
    m["lookahead_audit"] = run.lookahead_audit
    return clean(m)  # type: ignore[no-any-return]


def _market_extremes(df: pd.DataFrame) -> dict[str, Any]:
    g = df[df["pnl"].notna()].groupby("ticker")["pnl"].sum().sort_values()
    return {"n_markets_traded": int(df["ticker"].nunique()),
            "worst5": {k: float(v) for k, v in g.head(5).items()},
            "best5": {k: float(v) for k, v in g.tail(5).items()}}
