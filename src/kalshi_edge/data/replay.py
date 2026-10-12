"""Dataset abstraction consumed by the backtest engine (synthetic worlds or recorded books).

A ``ReplayDataset`` is a *read-only, time-indexed* view of a market universe:

* ``records``: every market that ever existed in the window (including ones that later voided
  or settled; no survivorship filter) together with its eventual result and result timestamp;
* ``book_at(ticker, t)``: the order book as known at ``t`` (never later information);
* ``forecasts``: optional external-signal provider that honours ``collected_at <= as_of``.

The engine, not the dataset, decides what is visible at ``t`` (listing time, close time,
settlement delay, quote delay).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from kalshi_edge.domain import Market, OrderBook, Outcome, SettlementResult
from kalshi_edge.interfaces import ExternalForecastProvider


@dataclass(frozen=True, slots=True)
class MarketRecord:
    market: Market  # as listed: status OPEN, result None (the eventual result is separate)
    result: SettlementResult | None
    result_ts: datetime | None  # when the result became known (before any settlement delay)
    event_key: str
    category: str
    horizon_hours: float = 0.0  # open -> close, for reporting


@dataclass(slots=True)
class ReplayDataset(ABC):
    kind: str = "unknown"
    scenario: str = ""
    label: str = ""
    start: datetime | None = None
    end: datetime | None = None
    records: list[MarketRecord] = field(default_factory=list)
    forecasts: ExternalForecastProvider | None = None
    station_map: dict[str, str] = field(default_factory=dict)
    provenance: dict[str, Any] = field(default_factory=dict)
    excluded: dict[str, int] = field(default_factory=dict)
    restamp_books: bool = True  # synthetic: books are 'as of now'; recorded: keep snapshot ts

    @abstractmethod
    def book_at(self, ticker: str, t: datetime) -> OrderBook | None:
        """Book content as of ``t`` (``ts`` = ``t``); None if none exists yet."""

    @abstractmethod
    def decision_times(self, interval: timedelta) -> list[datetime]:
        """Times at which the trading loop is stepped."""

    def max_best_ask(self, ticker: str, t: datetime) -> Decimal | None:
        """Upper bound of the best ask over both sides at ``t`` (None if no book).

        Used only for the exact-equivalence candidate prefilter; the default is exact.
        """
        book = self.book_at(ticker, t)
        if book is None:
            return None
        best: Decimal | None = None
        for outcome in (Outcome.YES, Outcome.NO):
            lvl = book.best_ask(outcome)
            if lvl is not None:
                best = lvl.price if best is None else max(best, lvl.price)
        return best
