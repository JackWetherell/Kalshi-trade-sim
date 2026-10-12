"""Order lifecycle with persisted idempotency.

Sequence for every order:
    persist INTENT  ->  mark SUBMITTING  ->  venue.submit
        ok        -> record venue state + fills
        rejected  -> REJECTED
        ambiguous -> UNKNOWN, then query by client_order_id (never blind resubmit)

Any order left in INTENT/SUBMITTING/UNKNOWN blocks new trading until resolved.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from kalshi_edge.domain import Fill, OrderIntent, OrderState, OrderStatus
from kalshi_edge.execution.errors import AmbiguousSubmission, OrderRejected
from kalshi_edge.execution.store import Store
from kalshi_edge.interfaces import Clock, ExecutionVenue

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SubmitResult:
    client_order_id: str
    state: OrderState
    status: OrderStatus | None
    new_fills: tuple[Fill, ...]
    note: str = ""


class ExecutionEngine:
    def __init__(self, venue: ExecutionVenue, store: Store, clock: Clock,
                 *, unknown_grace_seconds: float = 60.0) -> None:
        self.venue = venue
        self.store = store
        self.clock = clock
        self.unknown_grace_seconds = unknown_grace_seconds

    # ------------------------------------------------------------------ submit
    def submit(self, intent: OrderIntent) -> SubmitResult:
        cid = intent.client_order_id
        self.store.insert_intent(intent)  # raises DuplicateIntent: never resubmit
        self.store.update_order(cid, OrderState.SUBMITTING, self.clock.now())
        try:
            status = self.venue.submit(intent)
        except AmbiguousSubmission:
            self.store.update_order(cid, OrderState.UNKNOWN, self.clock.now(),
                                    note="ambiguous_submit")
            self.store.log_event(self.clock.now(), "ambiguous_submit", {"client_order_id": cid})
            return self._resolve_one(cid)
        except OrderRejected as exc:
            self.store.update_order(cid, OrderState.REJECTED, self.clock.now(),
                                    note=str(exc)[:200])
            return SubmitResult(cid, OrderState.REJECTED, None, (), str(exc))
        self._apply_status(status)
        fills = self.sync_fills()
        return SubmitResult(cid, status.state, status, tuple(f for f in fills
                                                              if f.client_order_id == cid))

    def _apply_status(self, status: OrderStatus) -> None:
        self.store.update_order(status.client_order_id, status.state, self.clock.now(),
                                exchange_order_id=status.exchange_order_id,
                                filled_quantity=status.filled_quantity)

    def _resolve_one(self, cid: str) -> SubmitResult:
        try:
            status = self.venue.get_order(cid)
        except Exception as exc:  # query failed: stay UNKNOWN (blocks trading)
            log.warning("order query failed for %s: %s", cid, exc)
            return SubmitResult(cid, OrderState.UNKNOWN, None, (), "query_failed")
        if status is None:
            order = self.store.get_order(cid)
            if order is not None:
                age = (self.clock.now() - order.created_at).total_seconds()
                if age >= self.unknown_grace_seconds:
                    # Exchange confirms no such order after the grace window. The same
                    # client_order_id is retired; a new decision is required to trade again.
                    self.store.update_order(cid, OrderState.REJECTED, self.clock.now(),
                                            note="not_found_after_grace")
                    return SubmitResult(cid, OrderState.REJECTED, None, (),
                                        "not_found_after_grace")
            return SubmitResult(cid, OrderState.UNKNOWN, None, (), "not_found_yet")
        self._apply_status(status)
        fills = self.sync_fills()
        return SubmitResult(cid, status.state, status,
                            tuple(f for f in fills if f.client_order_id == cid))

    def resolve_unresolved(self) -> list[SubmitResult]:
        """Resolve INTENT/SUBMITTING/UNKNOWN orders by querying the venue."""
        out = []
        for order in self.store.unresolved_orders():
            if order.state is OrderState.INTENT:
                # Persisted but never sent (crash before submit). Query anyway: if the venue
                # has it, adopt it; otherwise retire it after the grace window.
                pass
            out.append(self._resolve_one(order.client_order_id))
        return out

    # ------------------------------------------------------------- order sync
    def sync_fills(self, since: datetime | None = None) -> list[Fill]:
        new: list[Fill] = []
        for fill in self.venue.get_fills(since):
            if self.store.add_fill(fill):
                new.append(fill)
        return new

    def refresh_open_orders(self) -> None:
        for order in self.store.open_orders():
            st = self.venue.get_order(order.client_order_id)
            if st is not None:
                self._apply_status(st)

    def cancel_all_open(self, reason: str) -> Sequence[str]:
        """Cancel resting *entry* orders (all our orders are entries). Never liquidates."""
        cancelled: list[str] = []
        for st in self.venue.list_open_orders():
            res = self.venue.cancel(st.client_order_id)
            if res is not None:
                if self.store.get_order(st.client_order_id) is not None:
                    self._apply_status(res)
                cancelled.append(st.client_order_id)
        self.store.log_event(self.clock.now(), "cancel_all", {"reason": reason,
                                                              "cancelled": cancelled})
        return cancelled
