"""Protocols that keep decision logic identical across backtest/paper/demo/live.

Only the ``ExecutionVenue``, ``Clock`` and ``MarketDataProvider`` implementations differ
between modes. Strategy and risk code must depend on these protocols only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from kalshi_edge.domain import (
    AccountSnapshot,
    Fill,
    ForecastObservation,
    Market,
    Mode,
    OrderBook,
    OrderIntent,
    OrderStatus,
    Outcome,
    ProbabilityEstimate,
    StrategyId,
    TradeProposal,
)
from kalshi_edge.fees import FeeSchedule


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime:
        """Timezone-aware UTC 'current' time (wall clock or replay event time)."""
        ...


@runtime_checkable
class MarketDataProvider(Protocol):
    def list_markets(self) -> Sequence[Market]: ...

    def get_market(self, ticker: str) -> Market | None: ...

    def get_orderbook(self, ticker: str) -> OrderBook | None: ...


@runtime_checkable
class ExternalForecastProvider(Protocol):
    source: str

    def observations(self, key: str, as_of: datetime) -> Sequence[ForecastObservation]:
        """All observations for ``key`` that were available at ``as_of`` (no look-ahead)."""
        ...


@runtime_checkable
class ProbabilityEstimator(Protocol):
    model_id: str
    model_version: str

    def estimate(self, market: Market, outcome: Outcome, as_of: datetime
                 ) -> ProbabilityEstimate | None:
        """Return None when no trustworthy estimate exists (=> no trade)."""
        ...


@dataclass(frozen=True, slots=True)
class DecisionContext:
    now: datetime
    market: Market
    book: OrderBook
    estimates: Mapping[Outcome, ProbabilityEstimate | None]
    fees: FeeSchedule
    strategy_equity: Decimal  # designated strategy capital marked conservatively
    extras: Mapping[str, object] = field(default_factory=dict)


@runtime_checkable
class Strategy(Protocol):
    strategy_id: StrategyId

    def evaluate(self, ctx: DecisionContext) -> Sequence[TradeProposal]:
        """Return zero or more buy proposals. Empty is the normal 'NO TRADE' output."""
        ...


@runtime_checkable
class ExecutionVenue(Protocol):
    mode: Mode

    def submit(self, intent: OrderIntent) -> OrderStatus: ...

    def cancel(self, client_order_id: str) -> OrderStatus | None: ...

    def get_order(self, client_order_id: str) -> OrderStatus | None:
        """Look up an order by client id (used to resolve ambiguous submissions)."""
        ...

    def list_open_orders(self) -> Sequence[OrderStatus]: ...

    def get_fills(self, since: datetime | None = None) -> Sequence[Fill]: ...

    def get_account(self) -> AccountSnapshot: ...
