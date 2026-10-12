"""Replay market-data feed: what the trading loop may see at event time ``t``.

* a market is visible only once ``open_time <= t`` and while ``t < close_time``
  (closed markets keep their last book so open positions can still be marked conservatively);
* the decision book is the dataset book as of ``t - quote_delay`` re-stamped ``t`` (a delayed
  feed that does not know it is stale -- the worst case; a correctly time-stamped stale book
  would simply be rejected by the freshness check);
* ``spread_extra_ticks`` worsens the price of buying either outcome by that many grid ticks
  (both bid sides are lowered; total book width grows by twice that);
* the venue's *fill* book is the dataset book as of ``t + order_latency`` (no quote delay).
"""

from __future__ import annotations

import dataclasses
import heapq
from collections.abc import Sequence
from datetime import datetime, timedelta
from decimal import Decimal

from kalshi_edge.data.memory import InMemoryMarketData
from kalshi_edge.data.replay import MarketRecord, ReplayDataset
from kalshi_edge.domain import ZERO, Market, MarketStatus, OrderBook, PriceLevel, PriceRange
from kalshi_edge.risk.exposure import cluster_keys


@dataclasses.dataclass(frozen=True, slots=True)
class RiskGate:
    """Markets that the hard risk manager is *certain* to reject (single-reason shortcuts).

    Hiding them from the loop leaves every approved trade unchanged (any one rejection
    reason rejects the proposal; rejected proposals have no side effects) and only saves
    the cost of building and recording doomed proposals. Derived from static ceilings so
    it is conservative: see ``engine._risk_gate``.
    """

    block_all: bool = False
    blocked_clusters: frozenset[str] = frozenset()


class ReplayMarketData(InMemoryMarketData):
    """InMemoryMarketData whose ``list_markets`` returns only what the engine exposes."""

    def __init__(self) -> None:
        super().__init__()
        self.visible: dict[str, Market] = {}

    def list_markets(self) -> Sequence[Market]:
        return list(self.visible.values())


def step_down(price: Decimal, ranges: Sequence[PriceRange]) -> Decimal | None:
    """One grid tick below ``price`` (None if at/below the lowest valid price)."""
    for band in ranges:
        if band.start < price <= band.end:
            return price - band.step
    return None


def widen_book(book: OrderBook, ranges: Sequence[PriceRange], ticks: int) -> OrderBook:
    if ticks <= 0:
        return book

    def shift(levels: tuple[PriceLevel, ...]) -> tuple[PriceLevel, ...]:
        out: list[PriceLevel] = []
        for lvl in levels:
            p: Decimal | None = lvl.price
            for _ in range(ticks):
                p = step_down(p, ranges) if p is not None else None
            if p is not None and p > ZERO:
                out.append(PriceLevel(p, lvl.quantity))
        return tuple(out)

    return dataclasses.replace(book, yes_bids=shift(book.yes_bids), no_bids=shift(book.no_bids))


class ReplayFeed:
    def __init__(self, dataset: ReplayDataset, md: ReplayMarketData, *,
                 excluded_categories: Sequence[str], quote_delay: timedelta,
                 order_latency: timedelta, spread_extra_ticks: int, prefilter: bool,
                 price_floor: Decimal) -> None:
        self.ds = dataset
        self.md = md
        self.quote_delay = quote_delay
        self.latency = order_latency
        self.extra = spread_extra_ticks
        self.prefilter = prefilter
        self.floor = price_floor
        self.universe: list[MarketRecord] = [r for r in dataset.records
                                             if r.category not in set(excluded_categories)]
        self.by_ticker: dict[str, MarketRecord] = {r.market.ticker: r for r in self.universe}
        self._open_ptr = 0
        self._by_open = sorted(self.universe, key=lambda r: (r.market.open_time,
                                                             r.market.ticker))
        self._close_heap: list[tuple[datetime, str]] = []
        self._open: dict[str, MarketRecord] = {}
        self.max_listed_open_time: datetime | None = None  # look-ahead audit
        self._cluster_cache: dict[str, tuple[str, ...]] = {}

    # ----------------------------------------------------------------------- clock sync
    def advance(self, t: datetime, held: set[str], gate: RiskGate | None = None) -> None:
        """Bring the exposed universe and books to event time ``t``."""
        by_open = self._by_open
        while self._open_ptr < len(by_open) and by_open[self._open_ptr].market.open_time <= t:
            rec = by_open[self._open_ptr]
            self._open_ptr += 1
            self._open[rec.market.ticker] = rec
            self.md.upsert_market(rec.market)
            heapq.heappush(self._close_heap, (rec.market.close_time, rec.market.ticker))
            ot = rec.market.open_time
            if self.max_listed_open_time is None or ot > self.max_listed_open_time:
                self.max_listed_open_time = ot
        while self._close_heap and self._close_heap[0][0] <= t:
            _, tk = heapq.heappop(self._close_heap)
            rec = self._open.pop(tk)
            self.md.upsert_market(dataclasses.replace(rec.market, status=MarketStatus.CLOSED))
        t_eff = t - self.quote_delay
        visible: dict[str, Market] = {}
        for tk, rec in self._open.items():
            if self.prefilter and tk not in held:
                if gate is not None and (gate.block_all or self._blocked(rec, gate)):
                    continue
                top = self.ds.max_best_ask(tk, t_eff)
                if top is None or top + Decimal("0.01") * self.extra <= self.floor:
                    continue
            book = self._decision_book(tk, rec, t, t_eff)
            if book is None:
                continue
            self.md.set_book(book)
            visible[tk] = rec.market
        for tk in held:
            rec2 = self.by_ticker.get(tk)
            if rec2 is not None and tk in self._open and tk not in visible:
                book = self._decision_book(tk, rec2, t, t_eff)
                if book is not None:
                    self.md.set_book(book)
        self.md.visible = visible

    def _blocked(self, rec: MarketRecord, gate: RiskGate) -> bool:
        if not gate.blocked_clusters:
            return False
        keys = self._cluster_cache.get(rec.market.ticker)
        if keys is None:
            keys = cluster_keys(rec.market, rec.market.ticker)
            self._cluster_cache[rec.market.ticker] = keys
        return any(k in gate.blocked_clusters for k in keys)

    def _decision_book(self, tk: str, rec: MarketRecord, t: datetime, t_eff: datetime
                       ) -> OrderBook | None:
        book = self.ds.book_at(tk, t_eff)
        if book is None:
            return None
        book = widen_book(book, rec.market.price_ranges, self.extra)
        if not self.ds.restamp_books:
            return book
        return dataclasses.replace(book, ts=t, received_ts=t)

    def fill_book(self, ticker: str, now: datetime) -> OrderBook | None:
        """Book the venue matches against: content as of ``now + order_latency``."""
        rec = self.by_ticker.get(ticker)
        if rec is None:
            return None
        t_fill = now + self.latency
        book = self.ds.book_at(ticker, t_fill)
        if book is None:
            return None
        return widen_book(book, rec.market.price_ranges, self.extra)

    def settle(self, rec: MarketRecord, result_market: Market) -> None:
        self.md.upsert_market(result_market)
        self._open.pop(rec.market.ticker, None)
