"""Config validation: hard ceilings, unknown keys, live/prod consistency, strict booleans."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from kalshi_edge.config import (
    AppConfig,
    ExchangeConfig,
    FeeConfig,
    RiskLimits,
    StrategyConfig,
    load_config,
)
from kalshi_edge.domain import Mode, StrategyId

D = Decimal
ROOT = Path(__file__).resolve().parents[3]


def live_cfg(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"mode": "live", "exchange": {"environment": "prod"},
                            "allow_live_trading": True}
    base.update(over)
    return base


@pytest.mark.parametrize("field", ["per_entry_max_loss_frac"])
@pytest.mark.parametrize("val", ["0.03", "0", "-0.01"])
def test_per_entry_ceiling(field: str, val: str) -> None:
    with pytest.raises(ValidationError):
        RiskLimits(**{field: D(val)})


def test_per_entry_exactly_at_ceiling_ok_and_above_rejected() -> None:
    assert RiskLimits(per_entry_max_loss_frac=D("0.02"))
    with pytest.raises(ValidationError):
        RiskLimits(per_entry_max_loss_frac=D("0.0200001"))


@pytest.mark.parametrize(("kw", "val"), [
    ("kelly_fraction", "0.2"), ("kelly_fraction", "0.1000001"), ("kelly_fraction", "-0.01"),
    ("aggregate_open_loss_frac", "0.11"), ("cluster_loss_frac", "0.06"),
    ("daily_stop_frac", "0.04"), ("weekly_stop_frac", "0.07"), ("max_drawdown_frac", "0.11"),
    ("daily_stop_frac", "0"), ("min_price_exclusive", "0.79"), ("max_price", "1.0"),
    ("min_edge", "-0.01"),
])
def test_hard_ceilings_reject(kw: str, val: str) -> None:
    with pytest.raises(ValidationError):
        RiskLimits(**{kw: D(val)})


def test_orders_per_day_ceiling() -> None:
    with pytest.raises(ValidationError):
        RiskLimits(max_orders_per_day=11)
    with pytest.raises(ValidationError):
        RiskLimits(max_orders_per_day=-1)
    assert RiskLimits(max_orders_per_day=10)


def test_min_price_must_be_below_max_price() -> None:
    with pytest.raises(ValidationError):
        RiskLimits(min_price_exclusive=D("0.90"), max_price=D("0.90"))


def test_defaults_match_instruction_section5() -> None:
    r = RiskLimits()
    assert (r.per_entry_max_loss_frac, r.aggregate_open_loss_frac, r.cluster_loss_frac) == (
        D("0.01"), D("0.10"), D("0.05"))
    assert (r.daily_stop_frac, r.weekly_stop_frac, r.max_drawdown_frac) == (
        D("0.03"), D("0.06"), D("0.10"))
    assert r.max_orders_per_day == 10 and r.min_price_exclusive == D("0.80")
    assert r.min_edge == D("0.02") and r.kelly_fraction == D("0.10")
    assert r.category_allowlist == ()  # nothing eligible by default
    assert AppConfig().mode is Mode.PAPER and AppConfig().allow_live_trading is False


def test_unknown_keys_rejected_everywhere() -> None:
    for model, kw in [(AppConfig, {"surprise": 1}), (RiskLimits, {"disable_risk": True}),
                      (StrategyConfig, {"leverage": 3}), (ExchangeConfig, {"prod_url": "x"}),
                      (FeeConfig, {"free": True})]:
        with pytest.raises(ValidationError):
            model(**kw)
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"risk": {"per_entry_max_loss_frac": "0.01", "x": 1}})


def test_config_is_frozen() -> None:
    cfg = AppConfig()
    with pytest.raises(ValidationError):
        cfg.mode = Mode.LIVE  # type: ignore[misc]
    with pytest.raises(ValidationError):
        cfg.risk.kelly_fraction = D("0.5")  # type: ignore[misc]


def test_allow_live_trading_true_outside_live_rejected() -> None:
    for mode, env in [("paper", "none"), ("paper", "prod_public"), ("demo", "demo"),
                      ("backtest", "none")]:
        with pytest.raises(ValidationError):
            AppConfig.model_validate({"mode": mode, "allow_live_trading": True,
                                      "exchange": {"environment": env}})


@pytest.mark.parametrize("mode", ["paper", "demo", "backtest"])
def test_prod_environment_outside_live_rejected(mode: str) -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"mode": mode, "exchange": {"environment": "prod"}})


def test_live_requires_prod_env_and_c_strategy() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"mode": "live", "exchange": {"environment": "demo"}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"mode": "live", "exchange": {"environment": "none"}})
    for sid in ("naive-80", "scaled-80", "positive-ev-ablation"):
        with pytest.raises(ValidationError):
            AppConfig.model_validate(live_cfg(strategy={"id": sid}))
    ok = AppConfig.model_validate(live_cfg(strategy={"id": "positive-ev-80"}))
    assert ok.mode is Mode.LIVE and ok.strategy.id is StrategyId.POSITIVE_EV_80


def test_paper_and_demo_environment_consistency() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"mode": "paper", "exchange": {"environment": "demo"}})
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"mode": "demo", "exchange": {"environment": "prod_public"}})
    assert AppConfig.model_validate({"mode": "demo", "exchange": {"environment": "demo"}})


def test_live_config_without_opt_in_has_flag_false() -> None:
    cfg = AppConfig.model_validate({"mode": "live", "exchange": {"environment": "prod"}})
    assert cfg.allow_live_trading is False


@pytest.mark.parametrize("bad", ["true", "True", "yes", 1, "1", "on"])
def test_allow_live_trading_must_be_a_literal_boolean(bad: object) -> None:
    """Instruction §9: opt-in must be an explicit boolean set by the operator."""
    with pytest.raises(ValidationError):
        AppConfig.model_validate(live_cfg(allow_live_trading=bad))


def test_string_true_outside_live_is_rejected_either_way() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"mode": "paper", "allow_live_trading": "true"})


def test_config_hash_changes_with_any_risk_change_and_is_stable() -> None:
    a = AppConfig()
    assert a.config_hash() == AppConfig().config_hash()
    b = a.model_copy(update={"risk": RiskLimits(per_entry_max_loss_frac=D("0.005"))})
    assert a.config_hash() != b.config_hash()


def test_shipped_configs_load_and_none_is_armed() -> None:
    for name in ("paper.yaml", "backtest.yaml", "demo.yaml", "research.yaml"):
        cfg = load_config(ROOT / "configs" / name)
        assert cfg.allow_live_trading is False
        assert cfg.mode is not Mode.LIVE
    live = load_config(ROOT / "configs" / "live.example.yaml")
    assert live.mode is Mode.LIVE and live.allow_live_trading is False
    assert live.risk.category_allowlist == ()


def test_load_config_rejects_non_mapping_and_unknown_and_overrides(tmp_path: Path) -> None:
    p = tmp_path / "c.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ValueError):
        load_config(p)
    p.write_text("mode: paper\nrisk:\n  per_entry_max_loss_frac: '0.03'\n")
    with pytest.raises(ValidationError):
        load_config(p)
    p.write_text("mode: paper\nbogus: 1\n")
    with pytest.raises(ValidationError):
        load_config(p)
    p.write_text("mode: paper\n")
    cfg = load_config(p, overrides={"risk": {"min_edge": "0.03"}})
    assert cfg.risk.min_edge == D("0.03")
    # overrides cannot defeat ceilings either
    with pytest.raises(ValidationError):
        load_config(p, overrides={"risk": {"kelly_fraction": "0.2"}})
    # YAML "yes"/"on" parse to booleans under YAML 1.1; outside live it must still fail closed
    p.write_text("mode: paper\nallow_live_trading: yes\n")
    with pytest.raises(ValidationError):
        load_config(p)


def test_strategy_capital_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        AppConfig(strategy_capital=D(0))
    with pytest.raises(ValidationError):
        AppConfig(strategy_capital=D(-5))


def test_strategy_id_unknown_rejected() -> None:
    with pytest.raises(ValidationError):
        AppConfig.model_validate({"strategy": {"id": "martingale"}})


@pytest.mark.parametrize("kw", [{"taker_rate": D("-0.07")}, {"taker_rate": D("0")},
                                {"stress_multiplier": D("0")}])
def test_fee_config_rejects_nonsensical_rates(kw: dict[str, Decimal]) -> None:
    with pytest.raises(ValidationError):
        FeeConfig(**kw)


def test_fee_config_rejects_zero_rounding_increment() -> None:
    with pytest.raises(ValidationError):
        FeeConfig(rounding_increment=D("0"))


def test_live_canary_total_risk_has_a_hard_ceiling() -> None:
    with pytest.raises(ValidationError):
        RiskLimits(live_canary_total_risk=D("5000"))


@pytest.mark.parametrize("kw", [{"fee_reserve": D("-100")}, {"min_depth_notional": D("-1")},
                                {"max_book_age_seconds": 1e9}, {"max_spread": D("5")}])
def test_risk_limits_reject_check_weakening_values(kw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RiskLimits(**kw)
