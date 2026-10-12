#!/usr/bin/env python
"""Run the mandatory experiment suite of instruction.md section 7 on SEEDED SYNTHETIC worlds.

    SYNTHETIC -- engine validation, NOT evidence of real-market edge.

    uv run python scripts/run_experiments.py --config configs/backtest.yaml \
        --out reports/experiments_<date> [--profile full|quick] [--workers 4]

Per scenario it runs A, B, C (fixed stake and fractional Kelly), D, the perturbations
(fees x1.5, spread +1 tick, fill probability 0.8/0.5, depth haircut, quote delay, forecast
error +-1/+-3/+-5pp, 1-5 simultaneous forced-adverse event clusters, catalyst-clustered
losses, settlement delay, category exclusion) and the full sensitivity grid
(price threshold 0.80/0.85/0.90/0.95 x min edge 0/1/2/3pp for C; every cell is reported).
Walk-forward per category / event horizon / calendar month comes from each run's metrics.
"""

# ruff: noqa: E501
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import json
import multiprocessing as mp
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

PROFILES: dict[str, dict[str, Any]] = {
    # n_events/days chosen so the predeclared acceptance criteria (>=200 independent events
    # with prediction >= 0.80 in the validation fold) can be met; decision every 3h.
    "full": {"n_events": 2000, "days": 240, "decision_interval_minutes": 180},
    "quick": {"n_events": 700, "days": 100, "decision_interval_minutes": 360},
}
MAIN_SCENARIOS = ("efficient", "favorite_underpriced", "favorite_overpriced")
EXTRA_SCENARIOS = ("favorite_underpriced_mild",)
SYN = "SYNTHETIC — engine validation, NOT evidence of real-market edge"


def specs_for(scenario: str, core_only: bool) -> list[dict[str, Any]]:
    """Experiment specs: name, group, strategy, cfg overrides, option overrides."""
    out: list[dict[str, Any]] = []

    def add(name: str, group: str, strategy: str, cfg: dict[str, Any] | None = None,
            opts: dict[str, Any] | None = None, sizing: str | None = None) -> None:
        c: dict[str, Any] = {"strategy": {"id": strategy}}
        if sizing:
            c["strategy"]["sizing"] = sizing
        for k, v in (cfg or {}).items():
            c.setdefault(k, {}).update(v)
        out.append({"name": name, "group": group, "scenario": scenario, "strategy": strategy,
                    "cfg": c, "opts": opts or {}})

    add("A_naive", "core", "naive-80")
    add("B_scaled", "core", "scaled-80")
    add("C_fixed", "core", "positive-ev-80", sizing="fixed")
    add("C_kelly", "core", "positive-ev-80", sizing="kelly")
    add("D_ablation", "core", "positive-ev-ablation", sizing="kelly")
    if core_only:
        return out
    for strat, tag in (("positive-ev-80", "C"), ("naive-80", "A")):
        add(f"P_{tag}_fees_x1.5", "perturb", strat, {"fees": {"stress_multiplier": "1.5"}})
        add(f"P_{tag}_spread_plus1tick", "perturb", strat, {"backtest": {"spread_extra_ticks": 1}})
        add(f"P_{tag}_fillprob_0.8", "perturb", strat, {"backtest": {"fill_probability": "0.8"}})
    add("P_C_fillprob_0.5", "perturb", "positive-ev-80", {"backtest": {"fill_probability": "0.5"}})
    add("P_C_depth_0.5", "perturb", "positive-ev-80", None, {"depth_fraction": "0.5"})
    add("P_C_quote_delay_2h", "perturb", "positive-ev-80", {"backtest": {"quote_delay_seconds": 7200}})
    add("P_C_settle_delay_48h", "perturb", "positive-ev-80",
        {"backtest": {"settlement_delay_hours": 48}})
    for e in ("-5", "-3", "-1", "1", "3", "5"):
        add(f"P_C_forecast_error_{e}pp", "perturb", "positive-ev-80",
            {"backtest": {"forecast_error_pp": e}})
    for n in range(1, 6):
        add(f"S_C_forced_{n}_events", "stress", "positive-ev-80", None,
            {"forced_losses": {"n": n, "mode": "event", "every_days": 45, "first_day": 30}})
    add("S_A_forced_3_events", "stress", "naive-80", None,
        {"forced_losses": {"n": 3, "mode": "event", "every_days": 45, "first_day": 30}})
    add("S_C_catalyst_cluster_1day", "stress", "positive-ev-80", None,
        {"forced_losses": {"n": 1, "mode": "catalyst", "every_days": 45, "first_day": 30}})
    add("S_A_catalyst_cluster_1day", "stress", "naive-80", None,
        {"forced_losses": {"n": 1, "mode": "catalyst", "every_days": 45, "first_day": 30}})
    add("P_C_exclude_economics", "perturb", "positive-ev-80",
        {"backtest": {"excluded_categories": ["economics"]}})
    add("P_A_exclude_economics", "perturb", "naive-80",
        {"backtest": {"excluded_categories": ["economics"]}})
    add("P_A_exclude_weather", "perturb", "naive-80",
        {"backtest": {"excluded_categories": ["weather"]}})
    for thr in ("0.80", "0.85", "0.90", "0.95"):
        for edge in ("0", "0.01", "0.02", "0.03"):
            add(f"G_C_thr{thr}_edge{edge}", "grid", "positive-ev-80",
                {"risk": {"min_price_exclusive": thr, "min_edge": edge}})
    return out


def _worker(task: dict[str, Any]) -> dict[str, Any]:
    from decimal import Decimal

    from kalshi_edge.backtest import BacktestOptions, ForcedLossSpec, run_backtest
    from kalshi_edge.backtest.metrics import metrics_hash
    from kalshi_edge.config import load_config

    t0 = time.perf_counter()
    spec = task["spec"]
    cfg_over: dict[str, Any] = json.loads(json.dumps(task["base_overrides"]))
    for k, v in spec["cfg"].items():
        cfg_over.setdefault(k, {}).update(v)
    cfg = load_config(task["config"], overrides=cfg_over)
    o = dict(spec["opts"])
    opts_kwargs: dict[str, Any] = {"cache_dir": task["cache_dir"], "write_plots": task["plots"],
                                   "bootstrap_resamples": task["bootstrap"]}
    if "depth_fraction" in o:
        opts_kwargs["depth_fraction"] = Decimal(o["depth_fraction"])
    if "forced_losses" in o:
        opts_kwargs["forced_losses"] = ForcedLossSpec(**o["forced_losses"])
    label = f"{spec['scenario']}__{spec['name']}"
    if task.get("prewarm"):
        from kalshi_edge.backtest import prewarm

        prewarm(cfg, scenario=spec["scenario"], options=BacktestOptions(**opts_kwargs))
        return {"prewarm": label, "wall_s": time.perf_counter() - t0}
    res = run_backtest(cfg, out_dir=Path(task["out"]) / "runs", label=label,
                       scenario=spec["scenario"], options=BacktestOptions(**opts_kwargs))
    m = res.metrics
    cap, act, rk = m["capital"], m["activity"], m["risk"]
    boot = m.get("bootstrap_event_clustered") or {}
    ci = boot.get("total_pnl_ci95") or [None, None]
    per = m.get("by_period", {})
    est = (m.get("estimator") or {}).get("refit_status_counts", {})
    return {
        "name": spec["name"], "group": spec["group"], "scenario": spec["scenario"],
        "strategy": spec["strategy"], "n_trades": act["n_trades_settled"],
        "net_pnl": cap["net_pnl"], "roi_pct": cap["roi_on_designated_capital"] * 100,
        "final_bankroll": cap["final_bankroll"], "fees": cap["fees_paid"],
        "ret_on_deployed_pct": None if cap["return_on_deployed_capital"] is None
        else cap["return_on_deployed_capital"] * 100,
        "win_rate": act["win_rate"], "payoff": act["payoff_ratio"],
        "max_dd_usd": rk["max_drawdown_usd"], "max_dd_pct": rk["max_drawdown_pct"] * 100,
        "worst_day": rk.get("worst_day_pnl"), "worst_week": rk.get("worst_week_pnl"),
        "max_cluster_loss": rk["max_simultaneous_correlated_loss_event_cluster"],
        "pnl_ci_lo": ci[0], "pnl_ci_hi": ci[1],
        "pnl_insample": (per.get("in_sample") or {}).get("net_pnl"),
        "pnl_holdout": (per.get("holdout") or {}).get("net_pnl"),
        "n_insample": (per.get("in_sample") or {}).get("n_settled"),
        "n_holdout": (per.get("holdout") or {}).get("n_settled"),
        "halted_at": (act["halt"] or {}).get("first_halt_ts"),
        "refits_accepted": est.get("accepted", 0), "refits_total": sum(est.values()) if est else 0,
        "identity_ok": m["accounting"]["identity_ok"],
        "run_dir": str(res.out_dir), "wall_s": time.perf_counter() - t0,
        "metrics_sha256": metrics_hash(m),
    }


def write_index(rows: list[dict[str, Any]], out: Path) -> None:
    rows = sorted(rows, key=lambda r: (r["scenario"], r["group"], r["name"]))
    cols = list(rows[0].keys())
    with open(out / "index.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, cols)
        w.writeheader()
        w.writerows(rows)

    def f(v: Any, nd: int = 2) -> str:
        return "n/a" if v is None else (f"{v:,.{nd}f}" if isinstance(v, float) else str(v))

    lines = [f"# Experiment index\n\n> **{SYN}.**\n",
             "| scenario | experiment | trades | net P&L | ROI% | win | max DD $ | CI lo | CI hi "
             "| in-sample P&L | holdout P&L | halted | accepted refits |",
             "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append(
            f"| {r['scenario']} | {r['name']} | {r['n_trades']} | {f(r['net_pnl'])} | "
            f"{f(r['roi_pct'])} | {f(r['win_rate'], 3)} | {f(r['max_dd_usd'])} | "
            f"{f(r['pnl_ci_lo'])} | {f(r['pnl_ci_hi'])} | {f(r['pnl_insample'])} | "
            f"{f(r['pnl_holdout'])} | {(r['halted_at'] or '')[:10]} | "
            f"{r['refits_accepted']}/{r['refits_total']} |")
    lines.append("\n## Sensitivity grid (strategy C kelly): net P&L total [in-sample / holdout], "
                 "n trades\n")
    for sc in sorted({r["scenario"] for r in rows}):
        g = {r["name"]: r for r in rows if r["scenario"] == sc and r["group"] == "grid"}
        if not g:
            continue
        lines += [f"\n### {sc}\n", "| price threshold \\ min edge | 0 | 0.01 | 0.02 | 0.03 |",
                  "|---|---|---|---|---|"]
        for thr in ("0.80", "0.85", "0.90", "0.95"):
            cells = []
            for edge in ("0", "0.01", "0.02", "0.03"):
                r = g.get(f"G_C_thr{thr}_edge{edge}")
                cells.append("n/a" if r is None else
                             f"{f(r['net_pnl'])} [{f(r['pnl_insample'])} / {f(r['pnl_holdout'])}], "
                             f"n={r['n_trades']}")
            lines.append(f"| {thr} | " + " | ".join(cells) + " |")
    (out / "index.md").write_text("\n".join(lines) + "\n")


def write_breakdowns(rows: list[dict[str, Any]], out: Path) -> None:
    """Walk-forward per category / event horizon / month for the core runs."""
    lines = [f"# Walk-forward breakdowns (core runs)\n\n> **{SYN}.**\n"]
    for r in sorted(rows, key=lambda r: (r["scenario"], r["name"])):
        if r["group"] != "core":
            continue
        m = json.loads((Path(r["run_dir"]) / "metrics.json").read_text())
        lines.append(f"\n## {r['scenario']} / {r['name']}\n")
        for key, title in (("by_category", "category"), ("by_horizon_to_close", "horizon to close"),
                           ("by_entry_price", "entry price")):
            tab = m.get(key) or {}
            if not tab:
                continue
            lines += [f"\n*by {title}*\n", f"| {title} | trades | net P&L | win rate | return on cost |",
                      "|---|---|---|---|---|"]
            for k, v in tab.items():
                lines.append(f"| {k} | {v['n_trades']} | {v['net_pnl']:.2f} | "
                             f"{'n/a' if v['win_rate'] is None else f'{v['win_rate']:.3f}'} | "
                             f"{'n/a' if v['roi_on_cost'] is None else f'{v['roi_on_cost']:.4f}'} |")
        months = m.get("by_month") or {}
        if months:
            lines += ["\n*by calendar month (walk-forward periods)*\n",
                      "| month | trades | net P&L |", "|---|---|---|"]
            for k, v in sorted(months.items()):
                lines.append(f"| {k} | {v['n_trades']} | {v['net_pnl']:.2f} |")
    (out / "breakdowns.md").write_text("\n".join(lines) + "\n")


def curate(rows: list[dict[str, Any]], out: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("index.md", "index.csv", "breakdowns.md", "experiments_meta.json"):
        if (out / name).exists():
            shutil.copy2(out / name, dest / name)
    for r in rows:
        if r["group"] != "core":
            continue
        src = Path(r["run_dir"])
        d = dest / f"{r['scenario']}__{r['name']}"
        d.mkdir(exist_ok=True)
        for fn in ("summary.md", "metrics.json", "manifest.json", "equity_curve.png"):
            if (src / fn).exists():
                shutil.copy2(src / fn, d / fn)
        if r["name"] == "C_kelly":
            for fn in ("calibration.png", "drawdown.png", "returns_hist.png"):
                if (src / fn).exists():
                    shutil.copy2(src / fn, d / fn)
        if r["name"] == "B_scaled" and r["scenario"] == "favorite_underpriced":
            shutil.copy2(src / "sizing_curve_B.png", dest / "sizing_curve_B.png")
        if r["name"] == "A_naive":
            shutil.copy2(src / "ev_vs_price.png", d / "ev_vs_price.png")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--config", default="configs/backtest.yaml")
    ap.add_argument("--out", default=f"reports/experiments_{datetime.now(UTC):%Y%m%d}")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="full")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--scenarios", nargs="*", default=None)
    ap.add_argument("--no-plots-for-grid", action="store_true", default=True)
    ap.add_argument("--curate", default=None, help="copy curated subset to reports/committed/NAME")
    ap.add_argument("--only-core", action="store_true")
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    cache = str(out / ".wf_cache")
    base_overrides = {"backtest": PROFILES[args.profile]}
    scenarios = list(args.scenarios or (MAIN_SCENARIOS + EXTRA_SCENARIOS))
    tasks: list[dict[str, Any]] = []
    for sc in scenarios:
        core_only = args.only_core or sc in EXTRA_SCENARIOS
        for spec in specs_for(sc, core_only):
            tasks.append({"spec": spec, "config": args.config, "base_overrides": base_overrides,
                          "cache_dir": cache, "out": str(out), "bootstrap": 1000,
                          "plots": not (spec["group"] == "grid" and args.no_plots_for_grid)})
    # prewarm each distinct walk-forward key (scenario x {base, spread, quote delay, settle delay})
    pre_names = {"C_kelly", "P_C_spread_plus1tick", "P_C_quote_delay_2h", "P_C_settle_delay_48h"}
    pre = [dict(t, prewarm=True) for t in tasks if t["spec"]["name"] in pre_names]
    t0 = time.perf_counter()
    ctx = mp.get_context("fork")
    print(f"[{datetime.now(UTC):%H:%M:%S}] prewarm walk-forward caches: {len(pre)} keys", flush=True)
    with cf.ProcessPoolExecutor(args.workers, mp_context=ctx) as ex:
        list(ex.map(_worker, pre))
    print(f"prewarm done in {time.perf_counter() - t0:.0f}s; running {len(tasks)} experiments "
          f"on {args.workers} workers", flush=True)
    rows: list[dict[str, Any]] = []
    with cf.ProcessPoolExecutor(args.workers, mp_context=ctx) as ex:
        futs = {ex.submit(_worker, t): t for t in tasks}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            r = fut.result()
            rows.append(r)
            print(f"[{i}/{len(tasks)}] {r['scenario']}/{r['name']}: trades={r['n_trades']} "
                  f"net={r['net_pnl']:.2f} ({r['wall_s']:.0f}s)", flush=True)
    write_index(rows, out)
    write_breakdowns(rows, out)
    meta = {"label": SYN, "profile": args.profile, "profile_params": PROFILES[args.profile],
            "config": args.config, "n_experiments": len(rows), "workers": args.workers,
            "wall_seconds": time.perf_counter() - t0, "scenarios": scenarios,
            "finished_utc": datetime.now(UTC).isoformat()}
    (out / "experiments_meta.json").write_text(json.dumps(meta, indent=1))
    if args.curate:
        curate(rows, out, ROOT / "reports" / "committed" / args.curate)
    print(f"done: {len(rows)} experiments in {time.perf_counter() - t0:.0f}s -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
