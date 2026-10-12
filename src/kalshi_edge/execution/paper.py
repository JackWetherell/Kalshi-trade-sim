"""Local virtual exchange used by ``paper`` and ``backtest`` modes.

It has **no network access** and no route to any exchange order endpoint. Fills are
simulated conservatively against the most recent order-book snapshot supplied by
``book_source`` (live public data in paper mode, replayed snapshots in backtests):

* Buy orders only take displayed ask liquidity at or below the limit price, and only a
  configurable fraction (``depth_fraction``) of each displayed level (queue/adverse
  selection haircut).
* ``fill_probability`` < 1 randomly rejects whole orders (seeded RNG -> deterministic).
* Fees use the same ``FeeSchedule`` as the strategy (rounded up per fill).
* Resting (GTC) remainders are not filled by the simulator (no queue model) — they sit
  until cancelled; this understates fills rather than overstating them.
"""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal

from kalshi_edge.domain import (
    ONE,
    ZERO,
    AccountSnapshot,
    Fill,
    Market,
    Mode,
    OrderBook,
    OrderIntent,
    OrderState,
    OrderStatus,
    Outcome,
    Position,
    SettlementResult,
    TimeInForce,
)
from kalshi_edge.execution.errors import AmbiguousSubmission, OrderRejected
from kalshi_edge.fees import FeeSchedule
from kalshi_edge.interfaces import Clock
from kalshi_edge.pricing import floor_to_step


@dataclass(slots=True)
class _Pos:
    ticker: str
    event_ticker: str
    category: str
    outcome: Outcome
    quantity: Decimal
    cost_basis: Decimal


class PaperVenue:
    """Implements ``interfaces.ExecutionVenue`` without any network access."""

    def __init__(self, *, mode: Mode, clock: Clock, fees: FeeSchedule,
                 book_source: Callable[[str], OrderBook | None],
                 market_source: Callable[[str], Market | None], starting_cash: Decimal,
                 depth_fraction: Decimal = ONE, fill_probability: Decimal = ONE,
                 seed: int = 0) -> None:
        if mode not in (Mode.PAPER, Mode.BACKTEST):
            raise ValueError("PaperVenue is only for paper/backtest modes")
        self.mode: Mode = mode
        self._clock = clock
        self._fees = fees
        self._books = book_source
        self._markets = market_source
        self.cash = starting_cash
        self.depth_fraction = depth_fraction
        self.fill_probability = fill_probability
        self._rng = random.Random(seed)
        self._orders: dict[str, OrderStatus] = {}
        self._fills: list[Fill] = []
        self._positions: dict[tuple[str, Outcome], _Pos] = {}
        self._reserved: dict[str, Decimal] = {}
        self._fill_seq = 0
        self.realized_pnl = ZERO
        self.fees_paid = ZERO
        self.settlement_log: list[dict[str, object]] = []
        # Fault injection for chaos tests: "timeout_after_accept" | "timeout_before_accept"
        self.fault: str | None = None

    # ------------------------------------------------------------------ submit
    def submit(self, intent: OrderIntent) -> OrderStatus:
        if intent.mode is not self.mode:
            raise OrderRejected(f"intent mode {intent.mode} != venue mode {self.mode}")
        if intent.client_order_id in self._orders:
            # Exchange semantics: duplicate client_order_id is rejected, never re-executed.
            raise OrderRejected(f"duplicate client_order_id {intent.client_order_id}")
        fault, self.fault = self.fault, None
        if fault == "timeout_before_accept":
            raise AmbiguousSubmission(intent.client_order_id)
        status = self._execute(intent)
        if fault == "timeout_after_accept":
            raise AmbiguousSubmission(intent.client_order_id)
        return status

    def _execute(self, intent: OrderIntent) -> OrderStatus:
        now = self._clock.now()
        market = self._markets(intent.ticker)
        book = self._books(intent.ticker)
        base = OrderStatus(intent.client_order_id, f"paper-{intent.client_order_id}",
                           intent.ticker, intent.outcome, OrderState.REJECTED, intent.quantity,
                           ZERO, ZERO, intent.limit_price, now)
        if market is None or book is None:
            return self._store(base)
        if intent.max_cost > self.cash:
            return self._store(base)  # insufficient buying power
        if self.fill_probability < ONE and Decimal(str(self._rng.random())) >= \
                self.fill_probability:
            return self._store(replace(base, state=OrderState.CANCELED))

        remaining = intent.quantity
        legs: list[tuple[Decimal, Decimal]] = []
        for level in book.asks(intent.outcome):
            if remaining <= ZERO or level.price > intent.limit_price:
                break
            avail = floor_to_step(level.quantity * self.depth_fraction, market.quantity_step)
            take = min(remaining, avail)
            if take > ZERO:
                legs.append((level.price, take))
                remaining -= take
        filled = intent.quantity - remaining
        if intent.time_in_force is TimeInForce.FILL_OR_KILL and remaining > ZERO:
            return self._store(replace(base, state=OrderState.CANCELED))

        cost = ZERO
        for price, qty in legs:
            fee = self._fees.fee(qty, price, multiplier=market.fee_multiplier)
            self._fill_seq += 1
            self._fills.append(Fill(f"pf-{self._fill_seq}", intent.client_order_id,
                                    base.exchange_order_id or "", intent.ticker, intent.outcome,
                                    qty, price, fee, True, now))
            cost += qty * price + fee
            self.fees_paid += fee
        if cost > self.cash:  # fee overrun beyond max_cost: refuse atomically
            refused = self._fills[len(self._fills) - len(legs):]
            self.fees_paid -= sum((f.fee for f in refused), ZERO)
            del self._fills[len(self._fills) - len(legs):]
            return self._store(base)
        self.cash -= cost
        if filled > ZERO:
            key = (intent.ticker, intent.outcome)
            pos = self._positions.get(key)
            if pos is None:
                pos = _Pos(intent.ticker, market.event_ticker, market.category, intent.outcome,
                           ZERO, ZERO)
                self._positions[key] = pos
            pos.quantity += filled
            pos.cost_basis += cost

        if remaining == ZERO:
            state = OrderState.FILLED
        elif intent.time_in_force is TimeInForce.GOOD_TILL_CANCELED:
            state = OrderState.PARTIALLY_FILLED if filled > ZERO else OrderState.RESTING
            reserve = remaining * intent.limit_price + self._fees.fee(
                remaining, intent.limit_price, multiplier=market.fee_multiplier)
            self.cash -= reserve
            self._reserved[intent.client_order_id] = reserve
        else:
            # IOC remainder is cancelled immediately; a partial fill ends as CANCELED with
            # filled_quantity > 0 (mirrors exchange semantics).
            state = OrderState.CANCELED
        return self._store(replace(base, state=state, filled_quantity=filled,
                                   remaining_quantity=remaining if state.is_open else ZERO,
                                   quantity=intent.quantity))

    def _store(self, status: OrderStatus) -> OrderStatus:
        self._orders[status.client_order_id] = status
        return status

    # ------------------------------------------------------------- order mgmt
    def cancel(self, client_order_id: str) -> OrderStatus | None:
        st = self._orders.get(client_order_id)
        if st is None:
            return None
        if st.state.is_open:
            self.cash += self._reserved.pop(client_order_id, ZERO)
            st = replace(st, state=OrderState.CANCELED, remaining_quantity=ZERO,
                         ts=self._clock.now())
            self._orders[client_order_id] = st
        return st

    def get_order(self, client_order_id: str) -> OrderStatus | None:
        return self._orders.get(client_order_id)

    def list_open_orders(self) -> Sequence[OrderStatus]:
        return [o for o in self._orders.values() if o.state.is_open]

    def get_fills(self, since: datetime | None = None) -> Sequence[Fill]:
        return [f for f in self._fills if since is None or f.ts >= since]

    def get_account(self) -> AccountSnapshot:
        positions = tuple(Position(p.ticker, p.event_ticker, p.category, p.outcome, p.quantity,
                                   p.cost_basis)
                          for p in self._positions.values() if p.quantity > ZERO)
        return AccountSnapshot(self._clock.now(), self.cash, positions,
                               tuple(self.list_open_orders()), source=f"{self.mode.value}-venue")

    # --------------------------------------------------------------- settlement
    def settle(self, ticker: str, result: SettlementResult) -> Decimal:
        """Settle all positions in ``ticker``; returns cash paid out.

        VOID: cost basis *excluding fees already paid* is refunded (assumption; markets
        with nonstandard settlement are excluded from trading in the first place).
        """
        payout = ZERO
        for o in list(self._orders.values()):
            if o.ticker == ticker and o.state.is_open:
                self.cancel(o.client_order_id)
        for key, pos in list(self._positions.items()):
            if pos.ticker != ticker or pos.quantity == ZERO:
                continue
            if result is SettlementResult.VOID:
                fees = sum((f.fee for f in self._fills if f.ticker == ticker
                            and f.outcome is pos.outcome), ZERO)
                pay = max(ZERO, pos.cost_basis - fees)
            else:
                won = (result.value == pos.outcome.value)
                pay = pos.quantity if won else ZERO
            self.realized_pnl += pay - pos.cost_basis
            self.cash += pay
            payout += pay
            self.settlement_log.append({"ticker": ticker, "outcome": pos.outcome.value,
                                        "quantity": pos.quantity, "cost": pos.cost_basis,
                                        "payout": pay, "result": result.value,
                                        "ts": self._clock.now()})
            del self._positions[key]
        return payout

    # ------------------------------------------------------------- persistence
    def snapshot(self) -> dict[str, object]:
        """Serializable virtual-account state (paper mode survives restarts)."""
        return {
            "cash": str(self.cash), "realized_pnl": str(self.realized_pnl),
            "fees_paid": str(self.fees_paid), "fill_seq": self._fill_seq,
            "positions": [{"ticker": p.ticker, "event_ticker": p.event_ticker,
                           "category": p.category, "outcome": p.outcome.value,
                           "quantity": str(p.quantity), "cost_basis": str(p.cost_basis)}
                          for p in self._positions.values()],
            "orders": [{"cid": o.client_order_id, "xid": o.exchange_order_id,
                        "ticker": o.ticker, "outcome": o.outcome.value, "state": o.state.value,
                        "quantity": str(o.quantity), "filled": str(o.filled_quantity),
                        "remaining": str(o.remaining_quantity), "limit": str(o.limit_price),
                        "ts": o.ts.isoformat()} for o in self._orders.values()],
            "reserved": {k: str(v) for k, v in self._reserved.items()},
            "fills": [{"id": f.fill_id, "cid": f.client_order_id, "xid": f.exchange_order_id,
                       "ticker": f.ticker, "outcome": f.outcome.value, "qty": str(f.quantity),
                       "price": str(f.price), "fee": str(f.fee), "taker": f.is_taker,
                       "ts": f.ts.isoformat()} for f in self._fills],
        }

    def restore(self, data: dict[str, object]) -> None:
        def _list(key: str) -> list[dict[str, str]]:
            v = data.get(key, [])
            return v if isinstance(v, list) else []

        self.cash = Decimal(str(data["cash"]))
        self.realized_pnl = Decimal(str(data.get("realized_pnl", "0")))
        self.fees_paid = Decimal(str(data.get("fees_paid", "0")))
        self._fill_seq = int(str(data.get("fill_seq", 0)))
        self._positions = {}
        for p in _list("positions"):
            pos = _Pos(p["ticker"], p["event_ticker"], p["category"], Outcome(p["outcome"]),
                       Decimal(p["quantity"]), Decimal(p["cost_basis"]))
            self._positions[(pos.ticker, pos.outcome)] = pos
        self._orders = {}
        for o in _list("orders"):
            self._orders[o["cid"]] = OrderStatus(
                o["cid"], o["xid"], o["ticker"], Outcome(o["outcome"]), OrderState(o["state"]),
                Decimal(o["quantity"]), Decimal(o["filled"]), Decimal(o["remaining"]),
                Decimal(o["limit"]), datetime.fromisoformat(o["ts"]))
        reserved = data.get("reserved", {})
        self._reserved = ({k: Decimal(str(v)) for k, v in reserved.items()}
                          if isinstance(reserved, dict) else {})
        self._fills = [Fill(f["id"], f["cid"], f["xid"], f["ticker"], Outcome(f["outcome"]),
                            Decimal(f["qty"]), Decimal(f["price"]), Decimal(f["fee"]),
                            bool(f["taker"]), datetime.fromisoformat(f["ts"]))
                       for f in _list("fills")]
