"""Pure, deterministic sizing functions (Decimal only).

``budget`` is always *risk dollars*: the all-in cash outlay (depth-walked price * quantity plus
per-fill rounded fees). For a long binary the outlay is also the maximum loss.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

from kalshi_edge.domain import ONE, ZERO, OrderBook, Outcome
from kalshi_edge.fees import FeeSchedule
from kalshi_edge.pricing import walk_asks

SCALE_FLOOR = Decimal("0.80")
SCALE_WIDTH = Decimal("0.20")


def cash_outlay(book: OrderBook, outcome: Outcome, quantity: Decimal, fees: FeeSchedule,
                fee_multiplier: Decimal) -> Decimal:
    """All-in cost of buying ``quantity`` by walking the asks (fees rounded per fill)."""
    if quantity <= ZERO:
        return ZERO
    fills = walk_asks(book, outcome, quantity)
    notional = sum((f.price * f.quantity for f in fills), ZERO)
    return notional + fees.fee_for_fills(fills, multiplier=fee_multiplier)


def contracts_for_budget(budget: Decimal, book: OrderBook, outcome: Outcome, fees: FeeSchedule,
                         fee_multiplier: Decimal, quantity_step: Decimal,
                         max_contracts: Decimal) -> Decimal:
    """Largest multiple of ``quantity_step`` whose all-in outlay is <= ``budget``.

    Limited by displayed ask depth and ``max_contracts``. Outlay is monotone non-decreasing in
    quantity, so a binary search over whole steps is exact.
    """
    if quantity_step <= ZERO:
        raise ValueError("quantity_step must be positive")
    if budget <= ZERO or max_contracts <= ZERO or book.is_crossed_or_locked:
        return ZERO
    depth = sum((lvl.quantity for lvl in book.asks(outcome)), ZERO)
    hi = int(min(depth, max_contracts) // quantity_step)
    lo = 0
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if cash_outlay(book, outcome, mid * quantity_step, fees, fee_multiplier) <= budget:
            lo = mid
        else:
            hi = mid - 1
    return lo * quantity_step


def scaled_risk_budget(p: Decimal, min_risk: Decimal, max_risk: Decimal) -> Decimal:
    """B sizing: ``min + clip((p-0.80)/0.20, 0, 1) * (max-min)`` (risk dollars)."""
    score = (p - SCALE_FLOOR) / SCALE_WIDTH
    score = min(max(score, ZERO), ONE)
    return min_risk + score * (max_risk - min_risk)


def raw_scaled_curve(points: Iterable[Decimal], min_risk: Decimal = Decimal("2"),
                     max_risk: Decimal = Decimal("10")) -> list[tuple[Decimal, Decimal]]:
    """(p, budget) pairs of the raw, never-live B curve, for plotting."""
    return [(p, scaled_risk_budget(p, min_risk, max_risk)) for p in points]


def kelly_fraction_binary(q: Decimal, c: Decimal) -> Decimal:
    """Full-Kelly bankroll fraction for a $1/$0 binary at all-in cost ``c``: (q-c)/(1-c)."""
    if c >= ONE:
        return ZERO
    return max(ZERO, (q - c) / (ONE - c))


def kelly_budget(q_safe: Decimal, all_in_cost: Decimal, equity: Decimal,
                 kelly_multiplier: Decimal) -> Decimal:
    """Risk dollars = equity * kelly_multiplier * full-Kelly fraction (uses conservative q)."""
    if equity <= ZERO or kelly_multiplier <= ZERO:
        return ZERO
    return equity * kelly_multiplier * kelly_fraction_binary(q_safe, all_in_cost)
