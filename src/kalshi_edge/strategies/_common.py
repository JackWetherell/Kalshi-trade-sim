"""Helpers shared by the strategy implementations (not public API)."""

from __future__ import annotations

from decimal import Decimal

from kalshi_edge.domain import (
    ZERO,
    ExecutableQuote,
    OrderBook,
    Outcome,
    ProbabilityEstimate,
    StrategyId,
    TradeProposal,
)
from kalshi_edge.interfaces import DecisionContext
from kalshi_edge.pricing import executable_quote, is_on_grid
from kalshi_edge.strategies.selection import Candidate, cap_book
from kalshi_edge.strategies.sizing import contracts_for_budget

#: Quantity used to read the best-ask level and its per-contract fee.
PROBE_QUANTITY = Decimal("1")


def capped_book(ctx: DecisionContext, outcome: Outcome, max_price: Decimal) -> OrderBook:
    return cap_book(ctx.book, outcome, max_price)


def size_to_budget(ctx: DecisionContext, outcome: Outcome, budget: Decimal, max_price: Decimal,
                   max_contracts: Decimal) -> ExecutableQuote | None:
    """Quote for the largest on-step quantity within ``budget`` (levels > max_price excluded)."""
    book = capped_book(ctx, outcome, max_price)
    qty = contracts_for_budget(budget, book, outcome, ctx.fees, ctx.market.fee_multiplier,
                               ctx.market.quantity_step, max_contracts)
    if qty <= ZERO:
        return None
    return executable_quote(book, outcome, qty, ctx.fees, ctx.market.fee_multiplier)


def build_proposal(strategy_id: StrategyId, ctx: DecisionContext, cand: Candidate,
                   quote: ExecutableQuote, *, estimate: ProbabilityEstimate | None,
                   edge: Decimal | None, ev: Decimal | None, rationale: str
                   ) -> TradeProposal | None:
    """Assemble a proposal; drop it if the limit price is not on the market's grid."""
    if quote.fillable_quantity <= ZERO or quote.fillable_quantity != quote.requested_quantity:
        return None
    limit = quote.worst_price
    if not is_on_grid(limit, cand.market.price_ranges):
        return None
    return TradeProposal(
        strategy_id=strategy_id,
        market=cand.market,
        outcome=cand.outcome,
        quantity=quote.fillable_quantity,
        limit_price=limit,
        expected_price=quote.vwap,
        fee_per_contract=quote.fee_per_contract,
        estimate=estimate,
        edge_per_contract=edge,
        ev_per_contract=ev,
        book_ts=ctx.book.ts,
        rationale=rationale,
    )
