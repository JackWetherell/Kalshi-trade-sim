"""Run artifacts: manifest.json, metrics.json, trades.csv, PNG plots and summary.md."""
# ruff: noqa: E501

from __future__ import annotations

import importlib.metadata as md
import json
import platform
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from kalshi_edge.backtest.engine import RunData
from kalshi_edge.backtest.metrics import (
    clean,
    equity_series,
    metrics_hash,
    trades_frame,
)
from kalshi_edge.strategies import raw_scaled_curve

BLUE, ORANGE, GREY, RED = "#2a6fdb", "#d9731a", "#8a8f98", "#c0392b"
SYN_BANNER = "SYNTHETIC — engine validation, NOT evidence of real-market edge"


def _git(*args: str) -> str:
    try:
        root = Path(__file__).resolve().parents[3]
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                             timeout=10, check=False)
        return out.stdout.strip()
    except Exception:
        return ""


def build_manifest(run: RunData, metrics: dict[str, Any], started: datetime,
                   finished: datetime) -> dict[str, Any]:
    cfg = run.cfg
    pkgs = {}
    for name in ("pydantic", "numpy", "pandas", "matplotlib", "pyarrow", "kalshi-edge"):
        try:
            pkgs[name] = md.version(name)
        except md.PackageNotFoundError:
            pkgs[name] = "unknown"
    ds = run.dataset
    return dict(clean({
        "git_commit": _git("rev-parse", "HEAD") or "unknown",
        "git_dirty": bool(_git("status", "--porcelain")),
        "python": sys.version.split()[0], "platform": platform.platform(),
        "packages": pkgs,
        "config_hash": cfg.config_hash(),
        "config": cfg.model_dump(mode="json"),
        "backtest_options": {k: (str(v) if isinstance(v, Decimal) else v)
                             for k, v in vars(run.opts).items() if k != "forced_losses"}
        | {"forced_losses": None if run.opts.forced_losses is None
           else vars(run.opts.forced_losses)},
        "seed": cfg.backtest.seed,
        "label": run.label, "strategy": cfg.strategy.id.value, "scenario": run.scenario,
        "dataset": {"kind": ds.kind, "scenario": ds.scenario, "provenance": ds.provenance,
                    "label": run.final.get("provenance_label")},
        "universe": {"n_events": len(run.universe_events), "n_markets": run.n_markets_universe},
        "excluded_rows": ds.excluded,
        "model_version": (metrics.get("estimator") or {}).get("model"),
        "window": {"start": ds.start, "end": ds.end, "holdout_start": run.holdout_start},
        "run": {"started_utc": started, "finished_utc": finished, "timings_s": run.timings},
        "metrics_sha256": metrics_hash(metrics),
        "evidence_verdict": metrics.get("evidence_verdict"),
    }))


# ------------------------------------------------------------------------------- plots
def _style(ax: Any) -> None:
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.grid(color="#e3e5e8", linewidth=0.6)
    ax.set_axisbelow(True)


def _banner(fig: Any, run: RunData) -> None:
    if run.dataset.kind == "synthetic":
        fig.text(0.01, 0.005, SYN_BANNER, fontsize=7, color=RED, ha="left", va="bottom")


def make_plots(run: RunData, metrics: dict[str, Any], out: Path) -> list[str]:
    files: list[str] = []
    title = f"{run.cfg.strategy.id.value} | {run.scenario} | {run.label}"
    eq = equity_series(run)
    df = trades_frame(run)
    cap = float(run.cfg.strategy_capital)

    def save(fig: Any, name: str) -> None:
        _banner(fig, run)
        fig.tight_layout(rect=(0, 0.02, 1, 1))
        fig.savefig(out / name, dpi=110)
        plt.close(fig)
        files.append(name)

    if not eq.empty:
        fig, ax = plt.subplots(figsize=(8, 3.6))
        ax.plot(eq.index, eq.values, color=BLUE, linewidth=1.4)
        ax.axhline(cap, color=GREY, linewidth=0.8, linestyle="--")
        ax.axvline(pd.Timestamp(run.holdout_start), color=ORANGE, linewidth=1, linestyle=":")
        ax.set_title(f"Equity (marked) — {title}", fontsize=9)
        ax.set_ylabel("USD")
        _style(ax)
        save(fig, "equity_curve.png")
        peak = eq.cummax()
        fig, ax = plt.subplots(figsize=(8, 3.0))
        ax.fill_between(eq.index, -(peak - eq).values, 0, color=RED, alpha=0.35, linewidth=0)
        ax.set_title("Drawdown from peak (USD)", fontsize=9)
        _style(ax)
        save(fig, "drawdown.png")
    s = df[df["pnl"].notna()] if not df.empty else df
    if len(s):
        fig, ax = plt.subplots(figsize=(6.5, 3.6))
        bins = np.linspace(0.80, 1.0, 11)
        s2 = s.assign(b=pd.cut(s["vwap"], bins))
        g = s2.groupby("b", observed=True).apply(
            lambda x: pd.Series({"real": x["pnl"].sum() / x["qty"].sum(), "n": len(x),
                                 "mid": x["vwap"].mean(),
                                 "est": (x["ev_est_pc"] * x["qty"]).sum() / x["qty"].sum()
                                 if x["ev_est_pc"].notna().any() else np.nan}),
            include_groups=False)
        ax.plot(g["mid"], g["real"], marker="o", color=BLUE, label="realised net P&L / contract")
        if g["est"].notna().any():
            ax.plot(g["mid"], g["est"], marker="s", color=ORANGE, label="estimated EV / contract")
        ax.axhline(0, color=GREY, linewidth=0.8)
        ax.set_xlabel("entry price (VWAP)")
        ax.set_ylabel("USD per contract")
        ax.set_title("EV vs market price", fontsize=9)
        ax.legend(fontsize=7, frameon=False)
        _style(ax)
        save(fig, "ev_vs_price.png")
        fig, ax = plt.subplots(figsize=(6.5, 3.6))
        ret = (s["pnl"] / s["cost"]).to_numpy()
        ax.hist(ret, bins=40, color=BLUE, alpha=0.85)
        ax.axvline(0, color=GREY, linewidth=0.8)
        ax.set_xlabel("return per trade (net P&L / cash outlay)")
        ax.set_title(f"Returns histogram (n={len(ret)})", fontsize=9)
        _style(ax)
        save(fig, "returns_hist.png")
    est = (metrics.get("estimator") or {}).get("evaluation")
    if est:
        fig, ax = plt.subplots(figsize=(5.4, 4.2))
        ax.plot([0, 1], [0, 1], color=GREY, linewidth=0.8, linestyle="--")
        for name, color in (("in_sample", BLUE), ("holdout", ORANGE)):
            rb = (est.get(name) or {}).get("reliability_full")
            if rb:
                pts = [(b["mean_pred"], b["freq"]) for b in rb if b["n"] >= 30]
                if pts:
                    ax.plot(*zip(*pts, strict=True), marker="o", color=color, label=name)
        ax.set_xlabel("predicted P(purchased outcome) (calibrated, walk-forward)")
        ax.set_ylabel("realised frequency")
        ax.set_title("Calibration (reliability)", fontsize=9)
        ax.legend(fontsize=7, frameon=False)
        _style(ax)
        save(fig, "calibration.png")
    cfg = run.cfg.strategy
    grid = [Decimal(str(round(0.80 + i * 0.005, 3))) for i in range(41)]
    curve = raw_scaled_curve(grid, cfg.scaled_min_risk, cfg.scaled_max_risk)
    fig, ax = plt.subplots(figsize=(5.4, 3.4))
    ax.plot([float(p) for p, _ in curve], [float(b) for _, b in curve], color=BLUE)
    ax.set_xlabel("ask price p")
    ax.set_ylabel("risk budget (USD)")
    ax.set_title("Strategy B raw sizing curve (never live)", fontsize=9)
    _style(ax)
    save(fig, "sizing_curve_B.png")
    return files


# ------------------------------------------------------------------------------- summary
def _fmt(x: Any, nd: int = 2) -> str:
    if x is None:
        return "n/a"
    if isinstance(x, float):
        return f"{x:,.{nd}f}"
    return str(x)


def build_summary(run: RunData, m: dict[str, Any]) -> str:
    c, a, r = m["capital"], m["activity"], m["risk"]
    lines = []
    if m["dataset_kind"] == "synthetic":
        lines += [f"> **{SYN_BANNER}.** Results below are not predictions.", ""]
    lines += [
        f"# Backtest: {m['strategy']} | scenario `{m['scenario']}` | {run.label}", "",
        f"*Verdict:* **{m['evidence_verdict']}**", "",
        "## Headline", "",
        "| metric | value |", "|---|---|",
        f"| designated capital | ${_fmt(c['designated'])} |",
        f"| final bankroll | ${_fmt(c['final_bankroll'])} |",
        f"| net P&L (after fees) | ${_fmt(c['net_pnl'])} |",
        f"| fees paid | ${_fmt(c['fees_paid'])} |",
        f"| ROI on designated capital | {_fmt(c['roi_on_designated_capital'] * 100)}% |",
        f"| return on deployed capital (P&L / total cash outlay) | "
        f"{_fmt(None if c['return_on_deployed_capital'] is None else c['return_on_deployed_capital'] * 100)}% |",
        f"| realized / unresolved P&L | ${_fmt(c['realized_pnl'])} / "
        f"${_fmt(c['unresolved_pnl_marked'])} |",
        f"| trades settled / orders filled / submitted | {a['n_trades_settled']} / "
        f"{a['n_orders_filled']} / {a['n_orders_submitted']} |",
        f"| order fill ratio / contract fill ratio | {_fmt(a['order_fill_ratio'], 3)} / "
        f"{_fmt(a['contract_fill_ratio'], 3)} |",
        f"| win rate / payoff ratio | {_fmt(a['win_rate'], 3)} / {_fmt(a['payoff_ratio'], 3)} |",
        f"| avg capital lock (hours) | {_fmt(a['avg_capital_lock_hours'], 1)} |",
        f"| max drawdown | ${_fmt(r['max_drawdown_usd'])} ({_fmt(r['max_drawdown_pct'] * 100)}%) |",
        f"| worst day / worst week | ${_fmt(r.get('worst_day_pnl'))} / ${_fmt(r.get('worst_week_pnl'))} |",
        f"| max simultaneous correlated loss (largest open event cluster) | "
        f"${_fmt(r['max_simultaneous_correlated_loss_event_cluster'])} |",
        f"| halted | {a['halt']} |", ""]
    b = m.get("bootstrap_event_clustered") or {}
    if b.get("total_pnl_ci95"):
        lines += ["## Event-clustered bootstrap (resamples events, not rows)", "",
                  f"* total P&L 95% CI: [{_fmt(b['total_pnl_ci95'][0])}, {_fmt(b['total_pnl_ci95'][1])}]"
                  f" over {b['n_universe_events']} universe events ({b['n_traded_events']} traded)",
                  f"* mean P&L per traded event 95% CI: [{_fmt(b['mean_pnl_per_traded_event_ci95'][0], 3)}, "
                  f"{_fmt(b['mean_pnl_per_traded_event_ci95'][1], 3)}]",
                  f"* bootstrap P(total P&L > 0): {_fmt(b['p_total_pnl_gt_0'], 3)}", ""]
    ev = m.get("ev", {})
    if ev.get("n_trades_with_estimate"):
        lines += ["## Expected vs realized EV", "",
                  f"* expected EV total ${_fmt(ev['expected_ev_total'])} vs realized "
                  f"${_fmt(ev['realized_pnl_same_trades'])} ({ev['n_trades_with_estimate']} trades)",
                  f"* avg q {_fmt(ev['avg_q_estimate'], 4)}, avg breakeven (p+fee) "
                  f"{_fmt(ev['avg_breakeven_price_plus_fee'], 4)}, realised win frequency "
                  f"{_fmt(ev['realized_win_frequency'], 4)}", ""]
    est = m.get("estimator") or {}
    if est.get("evaluation"):
        lines += ["## Estimator (walk-forward) vs market-implied", "",
                  f"refits: {est['refit_status_counts']}; first accepted: "
                  f"{est['first_accepted_refit']}", "",
                  "| period | rows | events | Brier model (cal.) | Brier market ask | Brier raw |",
                  "|---|---|---|---|---|---|"]
        for p in ("in_sample", "holdout"):
            e = est["evaluation"].get(p) or {}
            lines.append(f"| {p} | {e.get('n_rows')} | {e.get('n_events')} | "
                         f"{_fmt(e.get('brier_model_calibrated'), 4)} | "
                         f"{_fmt(e.get('brier_market_implied_ask'), 4)} | "
                         f"{_fmt(e.get('brier_model_raw'), 4)} |")
        lines += ["", "Reliability (calibrated predictions >= 0.80, holdout):", "",
                  "| bin | rows | events | mean pred | freq |", "|---|---|---|---|---|"]
        for bn in (est["evaluation"].get("holdout") or {}).get("reliability_high", []):
            lines.append(f"| {bn['lo']:.2f}-{bn['hi']:.2f} | {bn['n']} | {bn['n_events']} | "
                         f"{_fmt(bn['mean_pred'], 4)} | {_fmt(bn['freq'], 4)} |")
        lines.append("")
    lines += ["## In-sample period vs untouched holdout", "",
              "| period | trades | net P&L | win rate | payoff | return on cost |",
              "|---|---|---|---|---|---|"]
    for p in ("in_sample", "holdout"):
        e = m["by_period"].get(p) or {}
        lines.append(f"| {p} | {e.get('n_settled')} | {_fmt(e.get('net_pnl'))} | "
                     f"{_fmt(e.get('win_rate'), 3)} | {_fmt(e.get('payoff_ratio'), 3)} | "
                     f"{_fmt(e.get('return_on_cost'), 4)} |")
    lines += ["", "## By category", "", "| category | trades | net P&L | win rate |", "|---|---|---|---|"]
    for k, v in (m.get("by_category") or {}).items():
        lines.append(f"| {k} | {v['n_trades']} | {_fmt(v['net_pnl'])} | {_fmt(v['win_rate'], 3)} |")
    lines += ["", "## Accounting identity", "",
              f"final cash = start - costs - fees + payouts: residual "
              f"{m['accounting']['identity_residual']} (ok={m['accounting']['identity_ok']})",
              "", f"Universe: {m['universe']['n_events']} events, {m['universe']['n_markets']} markets; "
              f"excluded rows: {m['universe']['excluded_rows'] or 'none'}",
              f"Timings (s): {run.timings}"]
    return "\n".join(lines) + "\n"


def write_run(run: RunData, metrics: dict[str, Any], reports_root: Path, started: datetime
              ) -> tuple[Path, dict[str, Any]]:
    stamp = started.strftime("%Y%m%dT%H%M%SZ")
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in run.label) or "run"
    reports_root.mkdir(parents=True, exist_ok=True)
    out = reports_root / f"{stamp}_{safe}"
    n = 1
    while True:
        try:
            out.mkdir()
            break
        except FileExistsError:
            n += 1
            out = reports_root / f"{stamp}_{safe}-{n}"
    df = trades_frame(run)
    df.to_csv(out / "trades.csv", index=False)
    (out / "metrics.json").write_text(json.dumps(metrics, indent=1, sort_keys=True))
    if run.opts.write_plots:
        make_plots(run, metrics, out)
    manifest = build_manifest(run, metrics, started, datetime.now(UTC))
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    (out / "summary.md").write_text(build_summary(run, metrics))
    return out, manifest


def latest_report_dir(reports_root: Path) -> Path | None:
    root = Path(reports_root)
    if not root.is_dir():
        return None
    cands = sorted((p for p in root.iterdir()
                    if p.is_dir() and (p / "manifest.json").exists()), key=lambda p: p.name)
    return cands[-1] if cands else None


def render_summary(run_dir: Path) -> str:
    run_dir = Path(run_dir)
    sm = run_dir / "summary.md"
    if sm.exists():
        return sm.read_text()
    mj = run_dir / "metrics.json"
    if mj.exists():
        m = json.loads(mj.read_text())
        return (f"# {run_dir.name}\n\n{m.get('label', '')}\n\nverdict: {m.get('evidence_verdict')}\n\n"
                f"net P&L: {m.get('capital', {}).get('net_pnl')}\n")
    return f"(no summary in {run_dir})\n"
