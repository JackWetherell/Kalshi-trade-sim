"""B / scaled-80: risk budget grows with p between 0.80 and 1.00. Backtest-only control."""

from __future__ import annotations

from collections.abc import Sequence

from kalshi_edge.config import RiskLimits, StrategyConfig
from kalshi_edge.domain import StrategyId, TradeProposal
from kalshi_edge.interfaces import DecisionContext
from kalshi_edge.strategies._common import PROBE_QUANTITY, build_proposal, size_to_budget
from kalshi_edge.strategies.selection import find_candidates
from kalshi_edge.strategies.sizing import scaled_risk_budget


class ProbabilityScaled:
    strategy_id = StrategyId.SCALED_80

    def __init__(self, cfg: StrategyConfig, risk: RiskLimits) -> None:
        self.cfg = cfg
        self.risk = risk

    def evaluate(self, ctx: DecisionContext) -> Sequence[TradeProposal]:
        out: list[TradeProposal] = []
        for cand in find_candidates(ctx, min_price_exclusive=self.risk.min_price_exclusive,
                                    max_price=self.risk.max_price,
                                    probe_quantity=PROBE_QUANTITY):
            budget = scaled_risk_budget(cand.best_ask, self.cfg.scaled_min_risk,
                                        self.cfg.scaled_max_risk)
            quote = size_to_budget(ctx, cand.outcome, budget, self.risk.max_price,
                                   self.cfg.max_contracts)
            if quote is None:
                continue
            est = cand.estimate
            ev = None if est is None else est.q - quote.vwap - quote.fee_per_contract
            rationale = (f"B scaled-80: ask={cand.best_ask} budget={budget} "
                         f"vwap={quote.vwap} fee/c={quote.fee_per_contract} "
                         f"qty={quote.fillable_quantity} (backtest-only control)")
            prop = build_proposal(self.strategy_id, ctx, cand, quote, estimate=est, edge=None,
                                  ev=ev, rationale=rationale)
            if prop is not None:
                out.append(prop)
        return out
