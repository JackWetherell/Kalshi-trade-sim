"""Market-implied baseline: q = the market's own price. NOT independent, never tradeable.

Used only as a benchmark; after fees it is expected to be negative. ``independent=False`` and
``CalibrationStatus.NONE`` guarantee strategy C/D never act on it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal

from kalshi_edge.domain import (
    CalibrationStatus,
    Market,
    OrderBook,
    Outcome,
    ProbabilityEstimate,
)

BookLookup = Callable[[str], OrderBook | None]


class MarketImpliedEstimator:
    model_id = "market-implied"
    model_version = "1"

    def __init__(self, book_lookup: BookLookup | None = None, *, use_mid: bool = False) -> None:
        self._lookup = book_lookup
        self.use_mid = use_mid

    def estimate_from_book(self, market: Market, book: OrderBook, outcome: Outcome,
                           as_of: datetime) -> ProbabilityEstimate | None:
        ask = book.best_ask(outcome)
        if ask is None:
            return None
        q: Decimal = ask.price
        if self.use_mid:
            bid = book.best_bid(outcome)
            if bid is None:
                return None
            q = (ask.price + bid.price) / 2
        return ProbabilityEstimate(
            ticker=market.ticker, outcome=outcome, q=q, q_safe=q, model_id=self.model_id,
            model_version=self.model_version, independent=False,
            calibration_status=CalibrationStatus.NONE, as_of=book.ts, category=market.category,
            details={"basis": "mid" if self.use_mid else "best_ask"},
        )

    def estimate(self, market: Market, outcome: Outcome, as_of: datetime
                 ) -> ProbabilityEstimate | None:
        if self._lookup is None:
            return None
        book = self._lookup(market.ticker)
        if book is None or book.ts > as_of:
            return None
        return self.estimate_from_book(market, book, outcome, as_of)
