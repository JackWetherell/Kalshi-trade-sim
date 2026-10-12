"""Builders shared by strategy tests (deterministic, no network)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

from kalshi_edge.domain import (
    CalibrationStatus,
    Market,
    MarketStatus,
    OrderBook,
    Outcome,
    PriceLevel,
    PriceRange,
    ProbabilityEstimate,
)
from kalshi_edge.fees import FeeSchedule
from kalshi_edge.interfaces import DecisionContext

D = Decimal
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
GRID = (PriceRange(D("0.01"), D("0.99"), D("0.01")),)


def make_market(**kw: object) -> Market:
    base: dict[str, object] = dict(
        ticker="T-1", event_ticker="E-1", series_ticker="S", category="weather", title="t",
        status=MarketStatus.OPEN, market_type="binary", open_time=NOW, close_time=NOW,
        price_ranges=GRID)
    base.update(kw)
    return Market(**base)  # type: ignore[arg-type]


def make_book(yes_asks: list[tuple[str, str]] | None = None,
              no_asks: list[tuple[str, str]] | None = None) -> OrderBook:
    """Build a book from desired ASK levels. YES ask a <=> NO bid 1-a (and vice versa)."""
    no_bids = sorted((PriceLevel(D(1) - D(p), D(q)) for p, q in (yes_asks or [])),
                     key=lambda lv: lv.price)
    yes_bids = sorted((PriceLevel(D(1) - D(p), D(q)) for p, q in (no_asks or [])),
                      key=lambda lv: lv.price)
    return OrderBook("T-1", tuple(yes_bids), tuple(no_bids), NOW)


def estimate(outcome: Outcome = Outcome.YES, q: str = "0.97", q_safe: str = "0.95",
             independent: bool = True,
             status: CalibrationStatus = CalibrationStatus.ACCEPTED) -> ProbabilityEstimate:
    return ProbabilityEstimate("T-1", outcome, D(q), D(q_safe), "m", "1", independent, status, NOW)


def make_ctx(book: OrderBook, estimates: Mapping[Outcome, ProbabilityEstimate | None]
             | None = None, market: Market | None = None,
             equity: str = "1009") -> DecisionContext:
    return DecisionContext(now=NOW, market=market or make_market(), book=book,
                           estimates=estimates or {}, fees=FeeSchedule(),
                           strategy_equity=D(equity))
