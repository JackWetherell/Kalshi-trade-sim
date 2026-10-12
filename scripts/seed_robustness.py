#!/usr/bin/env python
# ruff: noqa: E501
"""Repeat the core strategies A/B/C/D over several world seeds (SYNTHETIC; engine validation).

    uv run python scripts/seed_robustness.py --out reports/seeds_<date> --seeds 5

A single synthetic seed can flatter or punish a strategy by luck; this reports the spread.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import multiprocessing as mp
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import run_experiments as rx


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="configs/backtest.yaml")
    ap.add_argument("--out", required=True)
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--profile", default="full")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    tasks = []
    for sc in rx.MAIN_SCENARIOS:
        for spec in rx.specs_for(sc, True):
            for i in range(args.seeds):
                s = json.loads(json.dumps(spec))
                s["name"] = f"{spec['name']}__seed{i}"
                bo = {"backtest": dict(rx.PROFILES[args.profile], seed=20261012 + 1000 * i)}
                tasks.append({"spec": s, "config": args.config, "base_overrides": bo,
                              "cache_dir": str(out / ".wf_cache"), "out": str(out),
                              "bootstrap": 300, "plots": False})
    t0 = time.perf_counter()
    rows = []
    with cf.ProcessPoolExecutor(args.workers, mp_context=mp.get_context("fork")) as ex:
        for i, r in enumerate(ex.map(rx._worker, tasks), 1):
            rows.append(r)
            print(f"[{i}/{len(tasks)}] {r['scenario']}/{r['name']} net={r['net_pnl']:.1f}", flush=True)
    lines = [f"# Seed robustness ({args.seeds} seeds)\n", f"> **{rx.SYN}.**\n",
             "| scenario | strategy | mean net P&L | sd | min | max | seeds with P&L>0 | mean trades |",
             "|---|---|---|---|---|---|---|---|"]
    for sc in rx.MAIN_SCENARIOS:
        for base in ("A_naive", "B_scaled", "C_fixed", "C_kelly", "D_ablation"):
            v = [r for r in rows if r["scenario"] == sc and r["name"].startswith(base + "__")]
            p = [r["net_pnl"] for r in v]
            lines.append(f"| {sc} | {base} | {statistics.mean(p):.1f} | "
                         f"{statistics.pstdev(p):.1f} | {min(p):.1f} | {max(p):.1f} | "
                         f"{sum(1 for x in p if x > 0)}/{len(p)} | "
                         f"{statistics.mean(r['n_trades'] for r in v):.0f} |")
    (out / "seed_robustness.md").write_text("\n".join(lines) + "\n")
    (out / "seed_robustness.json").write_text(json.dumps(rows, indent=1, default=str))
    print(f"done in {time.perf_counter() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
