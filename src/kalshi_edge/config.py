"""Validated configuration. Unknown keys are rejected (fail closed).

Hard ceilings encoded here can only be raised by changing reviewed code, not YAML.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from kalshi_edge.domain import Mode, StrategyId
from kalshi_edge.fees import FeeSchedule

# Absolute ceilings (Section 5 of instruction.md). Raising any requires code review.
HARD_MAX_PER_ENTRY_LOSS_FRAC = Decimal("0.02")
HARD_MAX_AGGREGATE_LOSS_FRAC = Decimal("0.10")
HARD_MAX_CLUSTER_LOSS_FRAC = Decimal("0.05")
HARD_MAX_KELLY_FRACTION = Decimal("0.10")
HARD_MAX_ORDERS_PER_DAY = 10


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskLimits(_Strict):
    per_entry_max_loss_frac: Decimal = Decimal("0.01")
    aggregate_open_loss_frac: Decimal = Decimal("0.10")
    cluster_loss_frac: Decimal = Decimal("0.05")
    daily_stop_frac: Decimal = Decimal("0.03")
    weekly_stop_frac: Decimal = Decimal("0.06")
    max_drawdown_frac: Decimal = Decimal("0.10")
    max_orders_per_day: int = 10
    min_price_exclusive: Decimal = Decimal("0.80")
    max_price: Decimal = Decimal("0.97")
    min_edge: Decimal = Decimal("0.02")
    kelly_fraction: Decimal = Decimal("0.10")
    min_depth_notional: Decimal = Decimal("25")
    max_spread: Decimal = Decimal("0.05")
    max_book_age_seconds: float = 3.0
    max_forecast_age_seconds: float = 6 * 3600.0
    min_seconds_to_close: float = 2 * 3600.0
    max_clock_skew_seconds: float = 2.0
    fee_reserve: Decimal = Decimal("1.00")  # cash kept back for fee overruns
    category_allowlist: tuple[str, ...] = ()  # empty => nothing is eligible
    live_canary_total_risk: Decimal = Decimal("5")

    @model_validator(mode="after")
    def _ceilings(self) -> RiskLimits:
        checks = [
            (Decimal("0") < self.per_entry_max_loss_frac <= HARD_MAX_PER_ENTRY_LOSS_FRAC,
             "per_entry_max_loss_frac"),
            (Decimal("0") < self.aggregate_open_loss_frac <= HARD_MAX_AGGREGATE_LOSS_FRAC,
             "aggregate_open_loss_frac"),
            (Decimal("0") < self.cluster_loss_frac <= HARD_MAX_CLUSTER_LOSS_FRAC,
             "cluster_loss_frac"),
            (Decimal("0") <= self.kelly_fraction <= HARD_MAX_KELLY_FRACTION, "kelly_fraction"),
            (0 <= self.max_orders_per_day <= HARD_MAX_ORDERS_PER_DAY, "max_orders_per_day"),
            (Decimal("0") < self.daily_stop_frac <= Decimal("0.03"), "daily_stop_frac"),
            (Decimal("0") < self.weekly_stop_frac <= Decimal("0.06"), "weekly_stop_frac"),
            (Decimal("0") < self.max_drawdown_frac <= Decimal("0.10"), "max_drawdown_frac"),
            (Decimal("0.80") <= self.min_price_exclusive < self.max_price <= Decimal("0.99"),
             "price band"),
            (self.min_edge >= Decimal("0"), "min_edge"),
        ]
        bad = [name for ok, name in checks if not ok]
        if bad:
            raise ValueError(f"risk limits outside hard ceilings: {bad}")
        return self


class StrategyConfig(_Strict):
    id: StrategyId = StrategyId.POSITIVE_EV_80
    # A: constant notional per trade (dollars of cash outlay)
    naive_notional: Decimal = Decimal("10")
    # B: risk budget interpolated between min and max as p goes 0.80 -> 1.00
    scaled_min_risk: Decimal = Decimal("2")
    scaled_max_risk: Decimal = Decimal("10")
    # C/D sizing
    sizing: Literal["kelly", "fixed", "zero"] = "kelly"
    fixed_risk: Decimal = Decimal("5")
    slippage_buffer: Decimal = Decimal("0.005")  # per contract, beyond the depth-walked VWAP
    ablation_min_price: Decimal = Decimal("0.02")  # D considers any price above this
    # Upper bound on contracts per proposal before risk caps (guards absurd sizes)
    max_contracts: Decimal = Decimal("500")


class FeeConfig(_Strict):
    taker_rate: Decimal = Decimal("0.07")
    maker_rate: Decimal = Decimal("0.0175")
    rounding_increment: Decimal = Decimal("0.01")
    stress_multiplier: Decimal = Decimal("1")

    def schedule(self) -> FeeSchedule:
        return FeeSchedule(self.taker_rate, self.maker_rate, self.rounding_increment,
                           self.stress_multiplier)


class ExchangeConfig(_Strict):
    # "prod_public": read-only public production market data (paper mode).
    # "demo": Kalshi demo environment (mock funds). "prod": production trading (live only).
    environment: Literal["none", "prod_public", "demo", "prod"] = "none"
    request_timeout_seconds: float = 10.0
    max_requests_per_second: float = 5.0
    demo_key_id_env: str = "KALSHI_DEMO_KEY_ID"
    demo_private_key_path_env: str = "KALSHI_DEMO_PRIVATE_KEY_PATH"
    markets_page_limit: int = 200
    series_allowlist: tuple[str, ...] = ()


class PathsConfig(_Strict):
    state_dir: Path = Path("var")
    db_file: str = "kalshi_edge.sqlite3"
    kill_switch_file: str = "KILL_SWITCH"
    lock_file: str = "trader.lock"
    data_dir: Path = Path("data")
    reports_dir: Path = Path("reports")

    @property
    def db_path(self) -> Path:
        return self.state_dir / self.db_file

    @property
    def kill_switch_path(self) -> Path:
        return self.state_dir / self.kill_switch_file

    @property
    def lock_path(self) -> Path:
        return self.state_dir / self.lock_file


class BacktestConfig(_Strict):
    dataset: Literal["synthetic", "recorded"] = "synthetic"
    recorded_path: Path | None = None
    seed: int = 20261012
    n_events: int = 600
    start: str = "2025-01-01"
    days: int = 360
    decision_interval_minutes: int = 60
    order_latency_seconds: float = 2.0
    fill_probability: Decimal = Decimal("1")
    quote_delay_seconds: float = 0.0
    spread_extra_ticks: int = 0
    forecast_error_pp: Decimal = Decimal("0")
    settlement_delay_hours: float = 0.0
    excluded_categories: tuple[str, ...] = ()
    holdout_fraction: Decimal = Decimal("0.3")


class AppConfig(_Strict):
    mode: Mode = Mode.PAPER
    strategy_capital: Decimal = Field(default=Decimal("1009"), gt=0)
    allow_live_trading: bool = False
    strategy: StrategyConfig = StrategyConfig()
    risk: RiskLimits = RiskLimits()
    fees: FeeConfig = FeeConfig()
    exchange: ExchangeConfig = ExchangeConfig()
    paths: PathsConfig = PathsConfig()
    backtest: BacktestConfig = BacktestConfig()
    paper_scan_interval_seconds: float = 60.0

    @field_validator("allow_live_trading")
    @classmethod
    def _bool_strict(cls, v: object) -> bool:
        if v is not True and v is not False:
            raise ValueError("allow_live_trading must be a literal boolean")
        return v

    @model_validator(mode="after")
    def _mode_consistency(self) -> AppConfig:
        env = self.exchange.environment
        if self.mode is Mode.LIVE:
            if env != "prod":
                raise ValueError("live mode requires exchange.environment == 'prod'")
            if self.strategy.id is not StrategyId.POSITIVE_EV_80:
                raise ValueError("only positive-ev-80 may be configured for live mode")
        else:
            if env == "prod":
                raise ValueError("exchange.environment 'prod' is only valid in live mode")
            if self.allow_live_trading:
                raise ValueError("allow_live_trading may only be true in a live config")
        if self.mode is Mode.DEMO and env != "demo":
            raise ValueError("demo mode requires exchange.environment == 'demo'")
        if self.mode is Mode.PAPER and env not in ("prod_public", "none"):
            raise ValueError("paper mode reads public production data only")
        return self

    def config_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


def load_config(path: str | Path, *, overrides: dict[str, object] | None = None) -> AppConfig:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"config {path} must be a mapping")
    if overrides:
        raw = _deep_merge(raw, overrides)
    return AppConfig.model_validate(raw)


def _deep_merge(base: dict[str, object], extra: dict[str, object]) -> dict[str, object]:
    out = dict(base)
    for k, v in extra.items():
        cur = out.get(k)
        if isinstance(cur, dict) and isinstance(v, dict):
            out[k] = _deep_merge(cur, v)
        else:
            out[k] = v
    return out
