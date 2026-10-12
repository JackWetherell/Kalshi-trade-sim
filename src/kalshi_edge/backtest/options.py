"""Backtest knobs that are not part of the shared ``AppConfig`` (internal, additive)."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal


@dataclass(frozen=True)
class ForcedLossSpec:
    """Stress overlay: force the largest open clusters to resolve AGAINST our positions.

    Every ``every_days`` (first at ``first_day``) the ``n`` largest open clusters are chosen
    by cost basis ("event": event clusters; "catalyst": category+date clusters, i.e. one
    weather system hitting every city that day). When those events later settle, the result
    is flipped so that our held side loses. Shock outcomes are NOT fed to calibration (they are
    "unexpected" by construction).
    """

    n: int
    mode: Literal["event", "catalyst"] = "event"
    every_days: float = 45.0
    first_day: float = 30.0


@dataclass(frozen=True)
class BacktestOptions:
    depth_fraction: Decimal = Decimal("1")
    prefilter: bool = True  # exact-equivalence candidate prefilter (see engine docs)
    # walk-forward calibration
    refit_days: int = 7
    fold_days: int = 45
    embargo_days: float = 2.0
    min_train_events: int = 40
    calib_stride_hours: int = 6
    model_version: str = "wf-isotonic-1"
    # stress overlay
    forced_losses: ForcedLossSpec | None = None
    # synthetic world overrides (SyntheticConfig fields), e.g. {"forecast_bias_f": 1.0}
    synthetic: dict[str, Any] = field(default_factory=dict)
    # reporting
    bootstrap_resamples: int = 2000
    write_plots: bool = True
    cache_dir: str | None = None  # walk-forward disk cache; None = in-process cache only
