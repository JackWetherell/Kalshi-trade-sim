"""A / naive-80: buy anything with p > floor at constant notional. Backtest-only control."""

from __future__ import annotations

from collections.abc import Sequence

from kalshi_edge.config import RiskLimits, StrategyConfig
from kalshi_edge.domain import StrategyId, TradeProposal
from kalshi_edge.interfaces import DecisionContext
from kalshi_edge.strategies._common import PROBE_QUANTITY, build_proposal, size_to_budget
from kalshi_edge.strategies.selection import find_candidates


class NaiveThreshold:
    strategy_id = StrategyId.NAIVE_80

    def __init__(self, cfg: StrategyConfig, risk: RiskLimits) -> None:
        self.cfg = cfg
        self.risk = risk

    def evaluate(self, ctx: DecisionContext) -> Sequence[TradeProposal]:
        out: list[TradeProposal] = []
        for cand in find_candidates(ctx, min_price_exclusive=self.risk.min_price_exclusive,
                                    max_price=self.risk.max_price,
                                    probe_quantity=PROBE_QUANTITY):
            budget = self.cfg.naive_notional
            quote = size_to_budget(ctx, cand.outcome, budget, self.risk.max_price,
                                   self.cfg.max_contracts)
            if quote is None:
                continue
            est = cand.estimate
            ev = None if est is None else est.q - quote.vwap - quote.fee_per_contract
            rationale = (f"A naive-80: ask={cand.best_ask} vwap={quote.vwap} "
                         f"fee/c={quote.fee_per_contract} budget={budget} "
                         f"qty={quote.fillable_quantity}"
                         " (backtest-only control; price > 0.80 is not evidence of edge)")
            prop = build_proposal(self.strategy_id, ctx, cand, quote, estimate=est, edge=None,
                                  ev=ev, rationale=rationale)
            if prop is not None:
                out.append(prop)
        return out
