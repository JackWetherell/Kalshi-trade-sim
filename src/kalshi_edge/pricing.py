"""Price-grid handling and executable-price computation from order-book depth."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from kalshi_edge.domain import (
    ONE,
    ZERO,
    ExecutableQuote,
    OrderBook,
    Outcome,
    PriceLevel,
    PriceRange,
)
from kalshi_edge.fees import FeeSchedule


class GridError(ValueError):
    """Raised when a price cannot be placed on a market's valid grid."""


def _band_for(price: Decimal, ranges: Sequence[PriceRange]) -> PriceRange:
    for band in ranges:
        if band.start <= price <= band.end:
            return band
    raise GridError(f"price {price} outside all price_ranges")


def is_on_grid(price: Decimal, ranges: Sequence[PriceRange]) -> bool:
    if not ranges:
        return False
    for band in ranges:
        if band.start <= price <= band.end and (price - band.start) % band.step == ZERO:
            return True
    return False


def snap(price: Decimal, ranges: Sequence[PriceRange], *, up: bool) -> Decimal:
    """Snap ``price`` to the nearest valid grid point (up or down). Fails closed."""
    if not ranges:
        raise GridError("market has no price_ranges; refusing to guess a tick size")
    band = _band_for(price, ranges)
    steps = (price - band.start) / band.step
    rounding = ROUND_CEILING if up else ROUND_FLOOR
    snapped = band.start + steps.to_integral_value(rounding=rounding) * band.step
    if snapped > band.end or snapped < band.start:
        raise GridError(f"snapped price {snapped} leaves band {band}")
    return snapped


def floor_to_step(quantity: Decimal, step: Decimal) -> Decimal:
    if step <= ZERO:
        raise ValueError("quantity step must be positive")
    return (quantity / step).to_integral_value(rounding=ROUND_FLOOR) * step


def walk_asks(book: OrderBook, outcome: Outcome, quantity: Decimal) -> list[PriceLevel]:
    """Consume ask levels for ``outcome`` cheapest-first; returns the partial fills."""
    remaining = quantity
    fills: list[PriceLevel] = []
    for level in book.asks(outcome):
        if remaining <= ZERO:
            break
        take = min(remaining, level.quantity)
        if take > ZERO:
            fills.append(PriceLevel(level.price, take))
            remaining -= take
    return fills


def executable_quote(book: OrderBook, outcome: Outcome, quantity: Decimal,
                     fees: FeeSchedule, fee_multiplier: Decimal = ONE) -> ExecutableQuote | None:
    """Depth-weighted taker price to buy ``quantity`` of ``outcome``; None if book empty."""
    if quantity <= ZERO:
        raise ValueError("quantity must be positive")
    if book.is_crossed_or_locked:
        return None
    fills = walk_asks(book, outcome, quantity)
    if not fills:
        return None
    filled = sum((f.quantity for f in fills), ZERO)
    notional = sum((f.price * f.quantity for f in fills), ZERO)
    return ExecutableQuote(
        ticker=book.ticker,
        outcome=outcome,
        requested_quantity=quantity,
        fillable_quantity=filled,
        vwap=notional / filled,
        worst_price=fills[-1].price,
        best_price=fills[0].price,
        fee_total=fees.fee_for_fills(fills, multiplier=fee_multiplier),
        book_ts=book.ts,
    )


def ask_depth_notional(book: OrderBook, outcome: Outcome, max_price: Decimal = ONE) -> Decimal:
    """Dollar notional available to buy ``outcome`` at or below ``max_price``."""
    return sum((lvl.price * lvl.quantity for lvl in book.asks(outcome) if lvl.price <= max_price),
               ZERO)


def spread(book: OrderBook, outcome: Outcome) -> Decimal | None:
    ask, bid = book.best_ask(outcome), book.best_bid(outcome)
    if ask is None or bid is None:
        return None
    return ask.price - bid.price
