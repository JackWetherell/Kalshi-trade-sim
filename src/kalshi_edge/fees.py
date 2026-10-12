"""Kalshi trading-fee model (conservative).

Documented general formula (verify against https://kalshi.com/fee-schedule before any
live use; see docs/ADR.md for the verification status):

    fee = round_up(rate * multiplier * C * P * (1 - P))

* ``rate`` = 0.07 for takers, 0.0175 for makers (maker fees only on some markets).
* ``multiplier`` = series/market specific fee multiplier (default 1).
* ``C`` = contracts, ``P`` = price in dollars.
* Rounding: Kalshi rounds fees *up*. The exact rounding unit and whether rounding is
  applied per order, per fill or with an accumulator changed with sub-penny pricing.
  We therefore round **each fill up to ``rounding_increment``** (default $0.01), which
  is never cheaper than rounding the aggregate, i.e. conservative for EV estimates.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal

from kalshi_edge.domain import ONE, ZERO, PriceLevel


def round_up(amount: Decimal, increment: Decimal) -> Decimal:
    if increment <= ZERO:
        raise ValueError("increment must be positive")
    units = (amount / increment).to_integral_value(rounding=ROUND_CEILING)
    return units * increment


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    taker_rate: Decimal = Decimal("0.07")
    maker_rate: Decimal = Decimal("0.0175")
    rounding_increment: Decimal = Decimal("0.01")
    stress_multiplier: Decimal = ONE  # >1 for fee-stress backtests (e.g. 1.5)

    def fee(self, count: Decimal, price: Decimal, *, maker: bool = False,
            multiplier: Decimal = ONE) -> Decimal:
        """Fee in dollars for a single fill of ``count`` contracts at ``price``."""
        if count < ZERO:
            raise ValueError("count must be non-negative")
        if not (ZERO <= price <= ONE):
            raise ValueError(f"price must be in [0, 1], got {price}")
        if count == ZERO:
            return ZERO
        rate = self.maker_rate if maker else self.taker_rate
        raw = rate * multiplier * self.stress_multiplier * count * price * (ONE - price)
        return round_up(raw, self.rounding_increment)

    def fee_for_fills(self, fills: Iterable[PriceLevel], *, maker: bool = False,
                      multiplier: Decimal = ONE) -> Decimal:
        """Sum of per-fill rounded fees (>= fee of the aggregate; conservative)."""
        return sum((self.fee(f.quantity, f.price, maker=maker, multiplier=multiplier)
                    for f in fills), ZERO)
