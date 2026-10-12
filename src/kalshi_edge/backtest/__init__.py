"""Event-time replay backtester (drives the real ``runtime.loop.TradingLoop``).

Synthetic datasets are engine validation only -- NOT evidence of real-market edge.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kalshi_edge.backtest.options import BacktestOptions, ForcedLossSpec
from kalshi_edge.backtest.report import latest_report_dir, render_summary
from kalshi_edge.config import AppConfig


@dataclass
class BacktestResult:
    metrics: dict[str, Any]
    out_dir: Path | None
    manifest: dict[str, Any] = field(default_factory=dict)


def run_backtest(cfg: AppConfig, *, out_dir: Path | None = None, label: str = "",
                 scenario: str = "favorite_underpriced",
                 options: BacktestOptions | None = None) -> BacktestResult:
    """Run one backtest.

    ``out_dir`` is the REPORTS ROOT: a run directory ``<out_dir>/<UTC timestamp>_<label>/`` is
    created and returned in ``BacktestResult.out_dir``. ``None`` computes the metrics only and
    writes nothing. ``cfg.backtest.dataset`` picks synthetic vs recorded data and
    ``cfg.strategy.id`` the strategy.
    """
    from kalshi_edge.backtest.engine import execute
    from kalshi_edge.backtest.metrics import compute_metrics
    from kalshi_edge.backtest.report import build_manifest, write_run

    started = datetime.now(UTC)
    opts = options or BacktestOptions()
    run = execute(cfg, opts, scenario=scenario, label=label or cfg.strategy.id.value)
    metrics = compute_metrics(run)
    if out_dir is None:
        manifest = build_manifest(run, metrics, started, datetime.now(UTC))
        return BacktestResult(metrics, None, manifest)
    path, manifest = write_run(run, metrics, Path(out_dir), started)
    return BacktestResult(metrics, path, manifest)


def prewarm(cfg: AppConfig, *, scenario: str, options: BacktestOptions) -> None:
    """Compute and cache the walk-forward calibration for a config (no replay)."""
    from kalshi_edge.backtest.engine import build_dataset, force_backtest_cfg, prepare_walk_forward

    cfg = force_backtest_cfg(cfg)
    prepare_walk_forward(cfg, options, build_dataset(cfg, scenario, options))


__all__ = [
    "BacktestOptions",
    "BacktestResult",
    "ForcedLossSpec",
    "latest_report_dir",
    "prewarm",
    "render_summary",
    "run_backtest",
]
