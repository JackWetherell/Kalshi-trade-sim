"""Typed domain contracts shared by backtest, paper, demo and live modes.

All money, prices, quantities and fees are ``decimal.Decimal`` (dollars / contracts).
Never construct these from binary floats; use ``D("0.85")`` or strings from the API.

Conventions
-----------
* A binary contract pays $1 if its outcome occurs, else $0.
* ``Outcome.YES`` / ``Outcome.NO`` is the outcome *purchased* (we only open long positions).
* Kalshi order books publish **bids only** for YES and NO. The ask to *buy* YES at price
  ``a`` is implied by a NO bid at ``1 - a`` and vice versa (see ``OrderBook.asks``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

ZERO = Decimal("0")
ONE = Decimal("1")


def D(value: str | int | Decimal) -> Decimal:
    """Construct a Decimal, refusing floats (which silently carry binary error)."""
    if isinstance(value, float):
        raise TypeError("refusing to build a Decimal from a float; pass a string")
    return Decimal(value)


class Mode(StrEnum):
    BACKTEST = "backtest"
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"


class Outcome(StrEnum):
    YES = "yes"
    NO = "no"

    @property
    def other(self) -> Outcome:
        return Outcome.NO if self is Outcome.YES else Outcome.YES


class StrategyId(StrEnum):
    NAIVE_80 = "naive-80"  # A: backtest-only control
    SCALED_80 = "scaled-80"  # B: backtest-only control
    POSITIVE_EV_80 = "positive-ev-80"  # C: only live-eligible strategy
    ABLATION = "positive-ev-ablation"  # D: C without the 0.80 floor, research only


#: Strategies that may ever be routed to a live/production venue.
LIVE_ELIGIBLE_STRATEGIES: frozenset[StrategyId] = frozenset({StrategyId.POSITIVE_EV_80})


class MarketStatus(StrEnum):
    """Normalized lifecycle. Anything not ``OPEN`` is untradeable."""

    UNOPENED = "unopened"
    OPEN = "open"
    PAUSED = "paused"
    CLOSED = "closed"
    SETTLED = "settled"
    UNKNOWN = "unknown"


class SettlementResult(StrEnum):
    YES = "yes"
    NO = "no"
    VOID = "void"  # refunded / cancelled / scalar or otherwise nonstandard -> excluded


@dataclass(frozen=True, slots=True)
class PriceRange:
    """One band of a market's valid price grid: prices in [start, end] in steps of ``step``."""

    start: Decimal
    end: Decimal
    step: Decimal


@dataclass(frozen=True, slots=True)
class PriceLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class Market:
    ticker: str
    event_ticker: str
    series_ticker: str
    category: str
    title: str
    status: MarketStatus
    market_type: str  # only "binary" is supported
    open_time: datetime
    close_time: datetime
    price_ranges: tuple[PriceRange, ...]
    fee_multiplier: Decimal = ONE
    quantity_step: Decimal = ONE  # 1 unless the market supports fractional contracts
    expected_expiration_time: datetime | None = None
    rules_primary: str = ""
    rules_verified: bool = False  # set only by an explicit allowlist / rule check
    can_close_early: bool = False
    strike: Mapping[str, Any] = field(default_factory=dict)  # e.g. floor/cap/strike_type
    result: SettlementResult | None = None
    settlement_ts: datetime | None = None
    source: str = ""  # provenance (endpoint / file) for audit
    schema_version: str = ""


@dataclass(frozen=True, slots=True)
class OrderBook:
    """Snapshot of resting bids. Each side is sorted ascending by price (best bid last),
    matching Kalshi's ``orderbook_fp`` convention."""

    ticker: str
    yes_bids: tuple[PriceLevel, ...]
    no_bids: tuple[PriceLevel, ...]
    ts: datetime  # authoritative time the snapshot represents
    received_ts: datetime | None = None
    seq: int | None = None
    source: str = ""

    def bids(self, outcome: Outcome) -> tuple[PriceLevel, ...]:
        return self.yes_bids if outcome is Outcome.YES else self.no_bids

    def best_bid(self, outcome: Outcome) -> PriceLevel | None:
        levels = self.bids(outcome)
        return levels[-1] if levels else None

    def asks(self, outcome: Outcome) -> tuple[PriceLevel, ...]:
        """Levels available to BUY ``outcome``, best (cheapest) first.

        Buying YES at price ``a`` matches a resting NO bid at ``1 - a``.
        """
        opposite = self.bids(outcome.other)
        return tuple(PriceLevel(ONE - lvl.price, lvl.quantity) for lvl in reversed(opposite))

    def best_ask(self, outcome: Outcome) -> PriceLevel | None:
        asks = self.asks(outcome)
        return asks[0] if asks else None

    @property
    def is_crossed_or_locked(self) -> bool:
        yb, nb = self.best_bid(Outcome.YES), self.best_bid(Outcome.NO)
        return yb is not None and nb is not None and yb.price + nb.price >= ONE


class CalibrationStatus(StrEnum):
    NONE = "none"  # no calibration attempted / market-implied baseline
    INSUFFICIENT = "insufficient"  # too few independent resolved events
    REJECTED = "rejected"  # failed calibration acceptance criteria
    ACCEPTED = "accepted"  # passed predeclared out-of-sample criteria


@dataclass(frozen=True, slots=True)
class ProbabilityEstimate:
    """Probability that ``outcome`` of ``ticker`` pays $1.

    ``q_safe`` is a conservative (lower) bound used for all trade decisions.
    ``independent`` is False for anything derived from the Kalshi price itself.
    """

    ticker: str
    outcome: Outcome
    q: Decimal
    q_safe: Decimal
    model_id: str
    model_version: str
    independent: bool
    calibration_status: CalibrationStatus
    as_of: datetime  # latest timestamp of information used
    category: str = ""
    n_calibration_events: int = 0
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("q", "q_safe"):
            v = getattr(self, name)
            if not isinstance(v, Decimal) or not (ZERO <= v <= ONE):
                raise ValueError(f"{name} must be a Decimal in [0, 1], got {v!r}")
        if self.q_safe > self.q:
            raise ValueError("q_safe must not exceed q")

    @property
    def usable_for_live(self) -> bool:
        return self.independent and self.calibration_status is CalibrationStatus.ACCEPTED


@dataclass(frozen=True, slots=True)
class ExecutableQuote:
    """Result of walking the ask side of a book for a given quantity."""

    ticker: str
    outcome: Outcome
    requested_quantity: Decimal
    fillable_quantity: Decimal
    vwap: Decimal  # average price per contract over the fillable quantity (pre-fee)
    worst_price: Decimal  # highest ask level touched -> natural limit price
    best_price: Decimal
    fee_total: Decimal  # estimated taker fees for the fillable quantity
    book_ts: datetime

    @property
    def cash_outlay(self) -> Decimal:
        return self.vwap * self.fillable_quantity + self.fee_total

    @property
    def fee_per_contract(self) -> Decimal:
        if self.fillable_quantity == ZERO:
            return ZERO
        return self.fee_total / self.fillable_quantity


@dataclass(frozen=True, slots=True)
class TradeProposal:
    """A strategy's *request* to buy. The risk manager decides the final quantity."""

    strategy_id: StrategyId
    market: Market
    outcome: Outcome
    quantity: Decimal  # desired contracts (already rounded down to quantity_step)
    limit_price: Decimal  # worst acceptable price per contract (on-grid)
    expected_price: Decimal  # VWAP estimate used for EV
    fee_per_contract: Decimal
    estimate: ProbabilityEstimate | None
    edge_per_contract: Decimal | None  # q_safe - p - f - slippage (None for A/B)
    ev_per_contract: Decimal | None  # q - p - f using point q (None if no estimate)
    book_ts: datetime
    rationale: str = ""

    @property
    def max_loss(self) -> Decimal:
        """Worst case for a long binary: all cash paid including fees (limit-price bound)."""
        return self.quantity * (self.limit_price + self.fee_per_contract)

    @property
    def live_eligible(self) -> bool:
        return (
            self.strategy_id in LIVE_ELIGIBLE_STRATEGIES
            and self.estimate is not None
            and self.estimate.usable_for_live
        )


class TimeInForce(StrEnum):
    FILL_OR_KILL = "fill_or_kill"
    IMMEDIATE_OR_CANCEL = "immediate_or_cancel"
    GOOD_TILL_CANCELED = "good_till_canceled"


class OrderState(StrEnum):
    INTENT = "intent"  # persisted locally, not yet sent
    SUBMITTING = "submitting"  # send in flight; outcome unknown until acked or queried
    UNKNOWN = "unknown"  # ambiguous (timeout); MUST query before any retry
    RESTING = "resting"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELED = "canceled"
    REJECTED = "rejected"
    EXPIRED = "expired"

    @property
    def is_terminal(self) -> bool:
        return self in {OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED,
                        OrderState.EXPIRED}

    @property
    def is_open(self) -> bool:
        return not self.is_terminal


@dataclass(frozen=True, slots=True)
class OrderIntent:
    client_order_id: str
    decision_id: str
    mode: Mode
    strategy_id: StrategyId
    ticker: str
    event_ticker: str
    outcome: Outcome
    quantity: Decimal
    limit_price: Decimal
    time_in_force: TimeInForce
    max_cost: Decimal  # quantity * limit_price + max fees: cash reserved
    config_hash: str
    model_version: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    client_order_id: str
    exchange_order_id: str
    ticker: str
    outcome: Outcome
    quantity: Decimal
    price: Decimal
    fee: Decimal
    is_taker: bool
    ts: datetime


@dataclass(frozen=True, slots=True)
class OrderStatus:
    client_order_id: str
    exchange_order_id: str | None
    ticker: str
    outcome: Outcome
    state: OrderState
    quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    limit_price: Decimal
    ts: datetime


@dataclass(frozen=True, slots=True)
class Position:
    ticker: str
    event_ticker: str
    category: str
    outcome: Outcome
    quantity: Decimal
    cost_basis: Decimal  # total cash paid including fees

    @property
    def max_loss(self) -> Decimal:
        return self.cost_basis


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    """Authoritative (venue-reported) account state at ``ts``."""

    ts: datetime
    cash: Decimal  # available (unlocked) cash
    positions: tuple[Position, ...]
    open_orders: tuple[OrderStatus, ...]
    source: str = ""

    @property
    def open_order_reserved(self) -> Decimal:
        return sum(
            (o.remaining_quantity * o.limit_price for o in self.open_orders if o.state.is_open),
            ZERO,
        )


@dataclass(frozen=True, slots=True)
class Settlement:
    ticker: str
    result: SettlementResult
    ts: datetime


@dataclass(frozen=True, slots=True)
class ForecastObservation:
    """One externally sourced forecast value, stored with full provenance.

    ``issued_at`` is when the source published it; ``collected_at`` when we obtained it.
    A backtest at time ``t`` may only use observations with ``collected_at <= t``
    (or ``issued_at <= t`` for archives with verified publication timestamps).
    """

    source: str
    key: str  # e.g. "NWS:KNYC:max_temp_f:2026-10-12"
    issued_at: datetime
    collected_at: datetime
    valid_for: datetime  # the event time the forecast refers to
    values: Mapping[str, Decimal]  # e.g. {"mean": D("71"), "sd": D("3.2")}
    revision: int = 0
    license: str = ""
