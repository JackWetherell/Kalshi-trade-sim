"""Strategies A/B/C/D behind the ``Strategy`` protocol, plus the factory."""

from __future__ import annotations

from kalshi_edge.config import RiskLimits, StrategyConfig
from kalshi_edge.domain import StrategyId
from kalshi_edge.interfaces import Strategy
from kalshi_edge.strategies.naive import NaiveThreshold
from kalshi_edge.strategies.positive_ev import PositiveEV, PositiveEVAblation
from kalshi_edge.strategies.scaled import ProbabilityScaled
from kalshi_edge.strategies.selection import Candidate, cap_book, find_candidates
from kalshi_edge.strategies.sizing import (
    contracts_for_budget,
    kelly_budget,
    kelly_fraction_binary,
    raw_scaled_curve,
    scaled_risk_budget,
)


def make_strategy(strategy_cfg: StrategyConfig, risk: RiskLimits) -> Strategy:
    match strategy_cfg.id:
        case StrategyId.NAIVE_80:
            return NaiveThreshold(strategy_cfg, risk)
        case StrategyId.SCALED_80:
            return ProbabilityScaled(strategy_cfg, risk)
        case StrategyId.POSITIVE_EV_80:
            return PositiveEV(strategy_cfg, risk)
        case StrategyId.ABLATION:
            return PositiveEVAblation(strategy_cfg, risk)
    raise ValueError(f"unknown strategy id {strategy_cfg.id!r}")


__all__ = [
    "Candidate",
    "NaiveThreshold",
    "PositiveEV",
    "PositiveEVAblation",
    "ProbabilityScaled",
    "cap_book",
    "contracts_for_budget",
    "find_candidates",
    "kelly_budget",
    "kelly_fraction_binary",
    "make_strategy",
    "raw_scaled_curve",
    "scaled_risk_budget",
]
