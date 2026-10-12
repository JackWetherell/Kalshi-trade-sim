"""Replay-only subclasses that remove O(total orders/fills)-per-step costs of the shared
``Store`` and ``PaperVenue`` WITHOUT changing their observable behaviour.

* ``ReplayStore.add_fill`` short-circuits duplicates with an in-memory id set (the SQL
  ``INSERT OR IGNORE`` would return False for them anyway) and ``fills()`` serves a cache that
  is appended to on every successful insert.
* ``ReplayVenue`` tracks open order ids so ``list_open_orders`` does not scan every order ever
  placed.

Both are exercised against the unmodified classes in tests/unit/backtest.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from kalshi_edge.domain import Fill, OrderState, OrderStatus
from kalshi_edge.execution import PaperVenue, Store


class ReplayStore(Store):
    def __init__(self, path: Path | str) -> None:
        super().__init__(path)
        self._fill_ids: set[str] = set()
        self._fills_cache: list[Fill] = []

    def add_fill(self, fill: Fill) -> bool:
        if fill.fill_id in self._fill_ids:
            return False
        inserted = super().add_fill(fill)
        if inserted:
            self._fill_ids.add(fill.fill_id)
            self._fills_cache.append(fill)
        return inserted

    def fills(self, ticker: str | None = None) -> list[Fill]:
        if ticker is None:
            return list(self._fills_cache)
        return [f for f in self._fills_cache if f.ticker == ticker]


class ReplayVenue(PaperVenue):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._open_ids: set[str] = set()

    def _store(self, status: OrderStatus) -> OrderStatus:
        out = super()._store(status)
        if status.state.is_open:
            self._open_ids.add(status.client_order_id)
        else:
            self._open_ids.discard(status.client_order_id)
        return out

    def cancel(self, client_order_id: str) -> OrderStatus | None:
        out = super().cancel(client_order_id)
        self._open_ids.discard(client_order_id)
        return out

    def list_open_orders(self) -> Sequence[OrderStatus]:
        return [self._orders[i] for i in sorted(self._open_ids)
                if self._orders[i].state is not OrderState.CANCELED]
