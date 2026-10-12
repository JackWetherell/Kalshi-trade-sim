"""C / positive-EV-80 and D / ablation (C without the 0.80 floor).

Trades only with an independent, ACCEPTED-calibration estimate whose conservative ``q_safe``
clears ``p + f + slippage + min_edge`` and, strictly, ``p + f``. NO TRADE is the normal output.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from kalshi_edge.config import RiskLimits, StrategyConfig
from kalshi_edge.domain import ZERO, ExecutableQuote, StrategyId, TradeProposal
from kalshi_edge.interfaces import DecisionContext
from kalshi_edge.strategies._common import PROBE_QUANTITY, build_proposal, size_to_budget
from kalshi_edge.strategies.selection import Candidate, find_candidates
from kalshi_edge.strategies.sizing import kelly_budget

_MAX_ITERATIONS = 6


class PositiveEV:
    strategy_id = StrategyId.POSITIVE_EV_80

    def __init__(self, cfg: StrategyConfig, risk: RiskLimits) -> None:
        self.cfg = cfg
        self.risk = risk

    def _floor(self) -> Decimal | None:
        return self.risk.min_price_exclusive

    def evaluate(self, ctx: DecisionContext) -> Sequence[TradeProposal]:
        if self.cfg.sizing == "zero":
            return []
        out: list[TradeProposal] = []
        for cand in find_candidates(ctx, min_price_exclusive=self._floor(),
                                    max_price=self.risk.max_price,
                                    probe_quantity=PROBE_QUANTITY):
            prop = self._propose(ctx, cand)
            if prop is not None:
                out.append(prop)
        return out

    # -- internals -----------------------------------------------------------------------
    def _edge(self, q_safe: Decimal, quote: ExecutableQuote) -> tuple[Decimal, Decimal, bool]:
        """Return (all_in_breakeven p+f, edge, passes) for a quote."""
        breakeven = quote.vwap + quote.fee_per_contract
        edge = q_safe - breakeven - self.cfg.slippage_buffer
        passes = q_safe > breakeven and edge >= self.risk.min_edge
        return breakeven, edge, passes

    def _budget(self, q_safe: Decimal, quote: ExecutableQuote, ctx: DecisionContext) -> Decimal:
        if self.cfg.sizing == "fixed":
            return self.cfg.fixed_risk
        cost = quote.vwap + quote.fee_per_contract + self.cfg.slippage_buffer
        return kelly_budget(q_safe, cost, ctx.strategy_equity, self.risk.kelly_fraction)

    def _propose(self, ctx: DecisionContext, cand: Candidate) -> TradeProposal | None:
        est = cand.estimate
        if est is None or est.outcome is not cand.outcome or not est.usable_for_live:
            return None
        q_safe = est.q_safe
        _, _, ok = self._edge(q_safe, cand.quote)
        if not ok:
            return None
        budget = self._budget(q_safe, cand.quote, ctx)
        quote: ExecutableQuote | None = None
        for _ in range(_MAX_ITERATIONS):
            if budget <= ZERO:
                return None
            quote = size_to_budget(ctx, cand.outcome, budget, self.risk.max_price,
                                   self.cfg.max_contracts)
            if quote is None:
                return None
            _, _, ok = self._edge(q_safe, quote)
            if not ok:
                return None
            new_budget = self._budget(q_safe, quote, ctx)
            if new_budget >= budget:
                break
            budget = new_budget
        else:
            return None
        assert quote is not None
        breakeven, edge, _ = self._edge(q_safe, quote)
        p, f = quote.vwap, quote.fee_per_contract
        ev = est.q - p - f
        rationale = (
            f"{self.strategy_id.value}: side={cand.outcome.value} ask={cand.best_ask} vwap={p} "
            f"fee/c={f} slippage={self.cfg.slippage_buffer} q={est.q} q_safe={q_safe} "
            f"breakeven={breakeven} edge={edge} min_edge={self.risk.min_edge} ev/c={ev} "
            f"sizing={self.cfg.sizing} budget={budget} qty={quote.fillable_quantity} "
            f"model={est.model_id}@{est.model_version}")
        return build_proposal(self.strategy_id, ctx, cand, quote, estimate=est, edge=edge,
                              ev=ev, rationale=rationale)


class PositiveEVAblation(PositiveEV):
    """D: identical to C but candidates need only p > cfg.ablation_min_price. Research only."""

    strategy_id = StrategyId.ABLATION

    def _floor(self) -> Decimal | None:
        return self.cfg.ablation_min_price
