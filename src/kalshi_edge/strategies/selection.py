"""Single candidate-selection path shared by strategies A/B/C/D."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from decimal import Decimal

from kalshi_edge.domain import (
    ExecutableQuote,
    Market,
    MarketStatus,
    OrderBook,
    Outcome,
    ProbabilityEstimate,
)
from kalshi_edge.interfaces import DecisionContext
from kalshi_edge.pricing import executable_quote


@dataclass(frozen=True, slots=True)
class Candidate:
    market: Market
    outcome: Outcome
    best_ask: Decimal
    quote: ExecutableQuote  # at the probe quantity
    estimate: ProbabilityEstimate | None


def cap_book(book: OrderBook, outcome: Outcome, max_ask: Decimal) -> OrderBook:
    """Drop ask levels for ``outcome`` priced above ``max_ask`` (ask a = NO bid at 1-a)."""
    opposite = book.bids(outcome.other)
    kept = tuple(lvl for lvl in opposite if (1 - lvl.price) <= max_ask)
    if outcome is Outcome.YES:
        return dataclasses.replace(book, no_bids=kept)
    return dataclasses.replace(book, yes_bids=kept)


def find_candidates(ctx: DecisionContext, *, min_price_exclusive: Decimal | None,
                    max_price: Decimal, probe_quantity: Decimal) -> list[Candidate]:
    """YES and NO purchase sides whose best ask p satisfies floor < p <= max_price."""
    market = ctx.market
    if market.status is not MarketStatus.OPEN or market.market_type != "binary":
        return []
    out: list[Candidate] = []
    for outcome in (Outcome.YES, Outcome.NO):
        level = ctx.book.best_ask(outcome)
        if level is None:
            continue
        p = level.price
        if min_price_exclusive is not None and p <= min_price_exclusive:
            continue
        if p > max_price:
            continue
        quote = executable_quote(ctx.book, outcome, probe_quantity, ctx.fees,
                                 market.fee_multiplier)
        if quote is None:
            continue
        out.append(Candidate(market, outcome, p, quote, ctx.estimates.get(outcome)))
    return out
