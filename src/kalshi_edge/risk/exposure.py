"""Worst-case exposure aggregation across positions, resting orders and pending intents.

For long-only binary positions the worst case of each item is losing its entire cash
outlay (cost basis incl. fees; for orders: remaining qty * limit price + fee allowance).
Exposure is aggregated globally and per correlation cluster:

* ``event:<event_ticker>`` — all markets/legs of one event (both YES and NO legs count;
  hedges are NOT netted, which is conservative).
* ``catalyst:<category>:<settlement-date>`` — markets in one category resolving on the
  same date (e.g. several cities' temperatures driven by one weather system).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from kalshi_edge.domain import ZERO, AccountSnapshot, Market

MarketLookup = Callable[[str], Market | None]

#: Per-contract fee allowance for resting orders whose fills are not yet known.
RESTING_FEE_ALLOWANCE = Decimal("0.02")


def cluster_keys(market: Market | None, ticker: str) -> tuple[str, ...]:
    if market is None:
        return (f"event:UNKNOWN:{ticker}", "catalyst:UNKNOWN")
    when = market.expected_expiration_time or market.close_time
    return (f"event:{market.event_ticker}", f"catalyst:{market.category}:{when.date().isoformat()}")


@dataclass(frozen=True, slots=True)
class ExposureItem:
    ticker: str
    clusters: tuple[str, ...]
    max_loss: Decimal
    kind: str  # "position" | "order" | "pending"


@dataclass(slots=True)
class Exposure:
    items: list[ExposureItem] = field(default_factory=list)

    @property
    def total(self) -> Decimal:
        return sum((i.max_loss for i in self.items), ZERO)

    def by_cluster(self) -> dict[str, Decimal]:
        out: dict[str, Decimal] = defaultdict(lambda: ZERO)
        for item in self.items:
            for c in item.clusters:
                out[c] += item.max_loss
        return dict(out)

    def cluster_total(self, key: str) -> Decimal:
        return sum((i.max_loss for i in self.items if key in i.clusters), ZERO)

    def add(self, item: ExposureItem) -> None:
        self.items.append(item)

    def worst_simultaneous_loss(self, n_clusters: int) -> Decimal:
        """Loss if the ``n`` largest event clusters all resolve against us."""
        events = sorted((v for k, v in self.by_cluster().items() if k.startswith("event:")),
                        reverse=True)
        return sum(events[:n_clusters], ZERO)


def build_exposure(account: AccountSnapshot, lookup: MarketLookup,
                   pending: Iterable[ExposureItem] = ()) -> Exposure:
    exp = Exposure()
    for pos in account.positions:
        if pos.quantity <= ZERO:
            continue
        exp.add(ExposureItem(pos.ticker, cluster_keys(lookup(pos.ticker), pos.ticker),
                             pos.max_loss, "position"))
    for order in account.open_orders:
        if not order.state.is_open or order.remaining_quantity <= ZERO:
            continue
        worst = order.remaining_quantity * (order.limit_price + RESTING_FEE_ALLOWANCE)
        exp.add(ExposureItem(order.ticker, cluster_keys(lookup(order.ticker), order.ticker),
                             worst, "order"))
    for item in pending:
        exp.add(item)
    return exp
