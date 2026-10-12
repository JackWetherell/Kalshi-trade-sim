"""Order-book WebSocket maintenance: snapshot + delta application with ``seq`` gap detection.

The pure logic (:class:`BookBuilder`, :func:`apply_delta`) has no network dependency.
Message field names (``yes_dollars_fp`` / ``no_dollars_fp`` in snapshots; ``price_dollars``,
``delta_fp``, ``side`` in deltas) are UNVERIFIED against live traffic (docs unreachable from the
build container); anything else raises SchemaError and invalidates the book (fail closed).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from kalshi_edge.domain import ZERO, OrderBook, PriceLevel
from kalshi_edge.exchange.schema import (
    SchemaError,
    parse_decimal,
    parse_price,
    parse_quantity,
    require,
)


class SequenceGap(RuntimeError):
    def __init__(self, expected: int, got: int) -> None:
        super().__init__(f"seq gap: expected {expected}, got {got}")
        self.expected = expected
        self.got = got


def apply_delta(levels: Mapping[Decimal, Decimal], price: Decimal, delta: Decimal
                ) -> dict[Decimal, Decimal]:
    """Return a new side map with ``delta`` applied at ``price`` (<=0 quantity removes level).

    A delta that would drive a level negative is a schema/consistency violation.
    """
    out = dict(levels)
    new = out.get(price, ZERO) + delta
    if new < ZERO:
        raise SchemaError(f"delta drives level {price} negative ({new})")
    if new == ZERO:
        out.pop(price, None)
    else:
        out[price] = new
    return out


@dataclass
class BookBuilder:
    """Maintains one market's book from a single subscription's snapshot/delta stream."""

    ticker: str
    yes: dict[Decimal, Decimal] = field(default_factory=dict)
    no: dict[Decimal, Decimal] = field(default_factory=dict)
    last_seq: int | None = None
    valid: bool = False
    needs_resnapshot: bool = True
    invalid_reason: str = "no snapshot yet"
    last_ts: datetime | None = None

    def invalidate(self, reason: str) -> None:
        self.valid = False
        self.needs_resnapshot = True
        self.invalid_reason = reason
        self.yes, self.no, self.last_seq = {}, {}, None

    def apply_snapshot(self, msg: Mapping[str, Any], seq: int, ts: datetime) -> None:
        try:
            body = require(msg, "msg", "orderbook_snapshot")
            if require(body, "market_ticker", "snapshot") != self.ticker:
                raise SchemaError("snapshot for a different ticker")
            yes = self._side(body.get("yes_dollars_fp", []), "yes_dollars_fp")
            no = self._side(body.get("no_dollars_fp", []), "no_dollars_fp")
        except SchemaError as exc:
            self.invalidate(f"bad snapshot: {exc}")
            raise
        self.yes, self.no, self.last_seq = yes, no, seq
        self.valid, self.needs_resnapshot, self.invalid_reason = True, False, ""
        self.last_ts = ts

    @staticmethod
    def _side(raw: Any, field_name: str) -> dict[Decimal, Decimal]:
        if not isinstance(raw, list):
            raise SchemaError(f"{field_name}: expected list")
        out: dict[Decimal, Decimal] = {}
        for row in raw:
            if not isinstance(row, (list, tuple)) or len(row) != 2:
                raise SchemaError(f"{field_name}: bad level {row!r}")
            price = parse_price(row[0], field_name)
            if price in out:
                raise SchemaError(f"{field_name}: duplicate level {price}")
            out[price] = parse_quantity(row[1], field_name)
        return out

    def apply_delta_msg(self, msg: Mapping[str, Any], seq: int, ts: datetime) -> None:
        if not self.valid or self.last_seq is None:
            raise SchemaError("delta received while book invalid; waiting for snapshot")
        if seq != self.last_seq + 1:
            expected = self.last_seq + 1
            self.invalidate(f"seq gap: expected {expected}, got {seq}")
            raise SequenceGap(expected, seq)
        try:
            body = require(msg, "msg", "orderbook_delta")
            if require(body, "market_ticker", "delta") != self.ticker:
                raise SchemaError("delta for a different ticker")
            price = parse_price(require(body, "price_dollars", "delta"), "price_dollars")
            delta = parse_decimal(require(body, "delta_fp", "delta"), "delta_fp")
            side = require(body, "side", "delta")
            if side == "yes":
                self.yes = apply_delta(self.yes, price, delta)
            elif side == "no":
                self.no = apply_delta(self.no, price, delta)
            else:
                raise SchemaError(f"delta: bad side {side!r}")
        except SchemaError as exc:
            self.invalidate(f"bad delta: {exc}")
            raise
        self.last_seq = seq
        self.last_ts = ts

    def handle(self, message: Mapping[str, Any], now: datetime) -> bool:
        """Process one decoded WS message. Returns True if the book changed.

        Raises SequenceGap / SchemaError after invalidating the book (caller resubscribes).
        """
        mtype = message.get("type")
        if mtype not in ("orderbook_snapshot", "orderbook_delta"):
            return False
        seq = message.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            self.invalidate("message without integer seq")
            raise SchemaError("orderbook message without integer seq")
        if mtype == "orderbook_snapshot":
            self.apply_snapshot(message, seq, now)
        else:
            self.apply_delta_msg(message, seq, now)
        return True

    def to_orderbook(self) -> OrderBook | None:
        if not self.valid or self.last_ts is None:
            return None
        return OrderBook(
            ticker=self.ticker,
            yes_bids=tuple(PriceLevel(p, self.yes[p]) for p in sorted(self.yes)),
            no_bids=tuple(PriceLevel(p, self.no[p]) for p in sorted(self.no)),
            ts=self.last_ts, received_ts=self.last_ts, seq=self.last_seq,
            source="ws:orderbook_delta",
        )


class WsConnection(Protocol):
    async def send(self, data: str) -> None: ...
    def __aiter__(self) -> AsyncIterator[str | bytes]: ...


ConnectFactory = Callable[[], AbstractAsyncContextManager[WsConnection]]


async def run_orderbook_stream(
    connect: ConnectFactory,
    ticker: str,
    on_book: Callable[[OrderBook | None], Awaitable[None] | None],
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_resubscribes: int = 5,
) -> None:
    """Subscribe to ``orderbook_delta`` for ``ticker``; on gap/bad data, drop the book
    (``on_book(None)``) and reconnect for a fresh snapshot, up to ``max_resubscribes`` times.

    ``connect`` must produce an *authenticated* connection (see ``auth.RsaPssSigner.headers``
    with path ``/trade-api/ws/v2``). The caller owns host selection; this function never sends
    order messages.
    """
    attempts = 0
    while attempts <= max_resubscribes:
        builder = BookBuilder(ticker)
        async with connect() as conn:
            await conn.send(json.dumps({
                "id": 1, "cmd": "subscribe",
                "params": {"channels": ["orderbook_delta"], "market_ticker": ticker},
            }))
            async for raw in conn:
                try:
                    message = json.loads(raw)
                    if not isinstance(message, dict):
                        raise SchemaError("non-object ws message")
                    if builder.handle(message, clock()):
                        result = on_book(builder.to_orderbook())
                        if result is not None:
                            await result
                except (SequenceGap, SchemaError):
                    result = on_book(None)
                    if result is not None:
                        await result
                    break
            else:
                result = on_book(None)  # stream ended: book is stale
                if result is not None:
                    await result
                return
        attempts += 1
    raise RuntimeError(f"orderbook stream for {ticker}: too many resubscribes")
