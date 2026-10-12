"""Normalize Kalshi payloads into ``kalshi_edge.domain`` types (fail closed)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from kalshi_edge.domain import (
    ONE,
    ZERO,
    Fill,
    Market,
    MarketStatus,
    OrderBook,
    OrderState,
    OrderStatus,
    Outcome,
    Position,
    PriceLevel,
    PriceRange,
    SettlementResult,
)
from kalshi_edge.exchange.schema import (
    SCHEMA_VERSION,
    SchemaError,
    check_unknown_money_fields,
    parse_decimal,
    parse_json_number,
    parse_price,
    parse_quantity,
    parse_ts,
    require,
)

FRACTIONAL_STEP = Decimal("0.01")  # count_fp carries 2 decimals; used only if explicitly allowed

KNOWN_MARKET_MONEY_FIELDS: frozenset[str] = frozenset({
    "yes_bid_dollars", "yes_ask_dollars", "no_bid_dollars", "no_ask_dollars",
    "last_price_dollars", "previous_price_dollars", "previous_yes_bid_dollars",
    "previous_yes_ask_dollars", "volume_fp", "volume_24h_fp", "open_interest_fp",
    "liquidity_dollars", "notional_value_dollars", "settlement_value_dollars",
    "yes_bid_size_fp", "yes_ask_size_fp", "tick_size_dollars",
})

_STATUS_MAP: dict[str, MarketStatus] = {
    "initialized": MarketStatus.UNOPENED, "unopened": MarketStatus.UNOPENED,
    "active": MarketStatus.OPEN, "open": MarketStatus.OPEN,
    "inactive": MarketStatus.PAUSED, "paused": MarketStatus.PAUSED,
    "closed": MarketStatus.CLOSED,
    "determined": MarketStatus.SETTLED, "settled": MarketStatus.SETTLED,
    "finalized": MarketStatus.SETTLED,
}


def _market_status(raw: Any) -> MarketStatus:
    return _STATUS_MAP.get(str(raw).lower(), MarketStatus.UNKNOWN)


def is_supported_binary(market: Mapping[str, Any]) -> tuple[bool, str]:
    """(supported, reason). Reason is "ok" when supported, else why it is excluded."""
    if not isinstance(market, Mapping):
        return False, "payload is not an object"
    if market.get("market_type") != "binary":
        return False, f"market_type={market.get('market_type')!r} is not binary"
    for key in market:
        if key.startswith("mve_") and market[key] not in (None, "", [], {}):
            return False, f"multivariate/combo market ({key})"
    if market.get("strike_type") == "scalar":
        return False, "scalar strike_type"
    if _market_status(market.get("status")) is not MarketStatus.OPEN:
        return False, f"status={market.get('status')!r} is not open"
    ranges = market.get("price_ranges")
    if not isinstance(ranges, list) or not ranges:
        return False, "missing price_ranges"
    if market.get("result") in ("scalar",):
        return False, "nonstandard settlement"
    return True, "ok"


def parse_price_ranges(raw: Any) -> tuple[PriceRange, ...]:
    if not isinstance(raw, list) or not raw:
        raise SchemaError("market: price_ranges missing or empty")
    out: list[PriceRange] = []
    for i, band in enumerate(raw):
        ctx = f"price_ranges[{i}]"
        start = parse_decimal(require(band, "start", ctx), f"{ctx}.start")
        end = parse_decimal(require(band, "end", ctx), f"{ctx}.end")
        step = parse_decimal(require(band, "step", ctx), f"{ctx}.step")
        if step <= ZERO or start > end or start < ZERO or end > ONE:
            raise SchemaError(f"{ctx}: invalid band {start}..{end} step {step}")
        if (end - start) % step != ZERO:
            raise SchemaError(f"{ctx}: end not reachable from start by step")
        out.append(PriceRange(start, end, step))
    out.sort(key=lambda b: b.start)
    return tuple(out)


@dataclass(frozen=True, slots=True)
class SeriesInfo:
    ticker: str
    title: str
    category: str
    fee_type: str
    fee_multiplier: Decimal


SUPPORTED_FEE_TYPES = frozenset({"quadratic", "quadratic_with_maker_fees"})


def normalize_series(payload: Mapping[str, Any]) -> SeriesInfo:
    ctx = "series"
    ticker = str(require(payload, "ticker", ctx))
    fee_type = str(require(payload, "fee_type", ctx))
    if fee_type not in SUPPORTED_FEE_TYPES:
        raise SchemaError(f"series {ticker}: unsupported fee_type {fee_type!r}")
    mult = parse_json_number(require(payload, "fee_multiplier", ctx), "fee_multiplier")
    if mult < ZERO:
        raise SchemaError(f"series {ticker}: negative fee_multiplier")
    return SeriesInfo(ticker, str(payload.get("title", "")), str(payload.get("category", "")),
                      fee_type, mult)


def _strike(m: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in ("strike_type", "floor_strike", "cap_strike", "functional_strike", "custom_strike"):
        v = m.get(key)
        if v is None:
            continue
        if key in ("floor_strike", "cap_strike") and isinstance(v, (int, float, str)) \
                and not isinstance(v, bool):
            out[key] = parse_json_number(v, key)
        else:
            out[key] = v
    return out


def normalize_market(payload: Mapping[str, Any], *, series: SeriesInfo | None = None,
                     source: str = "", allow_fractional: bool = False,
                     strict_money_fields: bool = True) -> Market:
    """Raises SchemaError if unsupported or malformed; call is_supported_binary first to filter."""
    ok, reason = is_supported_binary(payload)
    if not ok:
        raise SchemaError(f"unsupported market: {reason}")
    ctx = "market"
    check_unknown_money_fields(payload, KNOWN_MARKET_MONEY_FIELDS, ctx, strict=strict_money_fields)
    ticker = str(require(payload, "ticker", ctx))
    event_ticker = str(require(payload, "event_ticker", ctx))
    series_ticker = str(payload.get("series_ticker") or (series.ticker if series else "")
                        or event_ticker.split("-", 1)[0])
    if series is not None:
        fee_mult = series.fee_multiplier
    elif "fee_multiplier" in payload:
        fee_mult = parse_json_number(payload["fee_multiplier"], "fee_multiplier")
    else:
        raise SchemaError(f"market {ticker}: unknown fee multiplier (no series info)")
    fractional = payload.get("fractional_trading_enabled")
    if fractional is not None and not isinstance(fractional, bool):
        raise SchemaError("fractional_trading_enabled must be boolean")
    result_raw = payload.get("result") or ""
    result: SettlementResult | None
    if result_raw in ("yes", "no"):
        result = SettlementResult(result_raw)
    elif result_raw == "":
        result = None
    else:
        result = SettlementResult.VOID
    expected_exp = payload.get("expected_expiration_time")
    settle_ts = payload.get("settlement_ts")
    return Market(
        ticker=ticker, event_ticker=event_ticker, series_ticker=series_ticker,
        category=(series.category if series else str(payload.get("category", ""))),
        title=str(payload.get("title", "")),
        status=_market_status(payload["status"]),
        market_type="binary",
        open_time=parse_ts(require(payload, "open_time", ctx), "open_time"),
        close_time=parse_ts(require(payload, "close_time", ctx), "close_time"),
        price_ranges=parse_price_ranges(payload.get("price_ranges")),
        fee_multiplier=fee_mult,
        quantity_step=FRACTIONAL_STEP if (allow_fractional and fractional) else ONE,
        expected_expiration_time=parse_ts(expected_exp, "expected_expiration_time")
        if expected_exp else None,
        rules_primary=str(payload.get("rules_primary", "")),
        rules_verified=False,
        can_close_early=bool(payload.get("can_close_early", False)),
        strike=_strike(payload),
        result=result,
        settlement_ts=parse_ts(settle_ts, "settlement_ts") if settle_ts else None,
        source=source,
        schema_version=SCHEMA_VERSION,
    )


# ---------------------------------------------------------------- order book

_BOOK_KEYS = frozenset({"yes_dollars", "no_dollars"})


def _parse_levels(raw: Any, field: str) -> tuple[PriceLevel, ...]:
    if not isinstance(raw, list):
        raise SchemaError(f"{field}: expected list")
    levels: dict[Decimal, Decimal] = {}
    for i, row in enumerate(raw):
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            raise SchemaError(f"{field}[{i}]: expected [price, quantity]")
        price = parse_price(row[0], f"{field}[{i}].price")
        qty = parse_quantity(row[1], f"{field}[{i}].quantity")
        if price in levels:
            raise SchemaError(f"{field}: duplicate price level {price}")
        levels[price] = qty
    return tuple(PriceLevel(p, levels[p]) for p in sorted(levels))


def normalize_orderbook(payload: Mapping[str, Any], *, ticker: str, ts: datetime,
                        received_ts: datetime | None = None, seq: int | None = None,
                        source: str = "") -> OrderBook:
    """Parse ``{"orderbook_fp": {"yes_dollars": [...], "no_dollars": [...]}}`` (bids only).

    Accepts only the documented keys; empty sides must be ``[]``. Crossed books are returned
    as-is (``OrderBook.is_crossed_or_locked``); consumers must treat them as unusable.
    """
    body = require(payload, "orderbook_fp", "orderbook")
    if not isinstance(body, Mapping):
        raise SchemaError("orderbook_fp: expected object")
    keys = set(body)
    if keys != _BOOK_KEYS:
        raise SchemaError(f"orderbook_fp: expected keys {sorted(_BOOK_KEYS)}, got {sorted(keys)}")
    if set(payload) - {"orderbook_fp"}:
        raise SchemaError(f"orderbook: unexpected top-level keys {sorted(set(payload))}")
    return OrderBook(
        ticker=ticker,
        yes_bids=_parse_levels(body["yes_dollars"], "yes_dollars"),
        no_bids=_parse_levels(body["no_dollars"], "no_dollars"),
        ts=ts, received_ts=received_ts, seq=seq, source=source,
    )


# ---------------------------------------------------------------- orders / fills / account

_ORDER_STATUS = {
    "resting": OrderState.RESTING,
    "pending": OrderState.SUBMITTING,
    "canceled": OrderState.CANCELED,
    "cancelled": OrderState.CANCELED,
    "executed": OrderState.FILLED,
    "expired": OrderState.EXPIRED,
    "rejected": OrderState.REJECTED,
}

KNOWN_ORDER_MONEY_FIELDS: frozenset[str] = frozenset({
    "yes_price_dollars", "no_price_dollars", "initial_count_fp", "fill_count_fp",
    "remaining_count_fp", "taker_fees_dollars", "maker_fees_dollars",
    "taker_fill_cost_dollars", "maker_fill_cost_dollars",
})


def _outcome(raw: Any, field: str) -> Outcome:
    if raw == "yes":
        return Outcome.YES
    if raw == "no":
        return Outcome.NO
    raise SchemaError(f"{field}: expected 'yes'|'no', got {raw!r}")


def normalize_order(payload: Mapping[str, Any]) -> OrderStatus:
    """Order object from GET /portfolio/orders (shape UNVERIFIED against live; see ADR)."""
    ctx = "order"
    check_unknown_money_fields(payload, KNOWN_ORDER_MONEY_FIELDS, ctx)
    outcome = _outcome(require(payload, "side", ctx), "side")
    action = require(payload, "action", ctx)
    if action != "buy":
        raise SchemaError(f"order: unsupported action {action!r} (system only opens long)")
    status_raw = str(require(payload, "status", ctx)).lower()
    if status_raw not in _ORDER_STATUS:
        raise SchemaError(f"order: unknown status {status_raw!r}")
    state = _ORDER_STATUS[status_raw]
    price_key = "yes_price_dollars" if outcome is Outcome.YES else "no_price_dollars"
    price = parse_price(require(payload, price_key, ctx), price_key)
    initial = parse_quantity(require(payload, "initial_count_fp", ctx), "initial_count_fp")
    filled = parse_quantity(require(payload, "fill_count_fp", ctx), "fill_count_fp",
                            allow_zero=True)
    remaining = parse_quantity(require(payload, "remaining_count_fp", ctx), "remaining_count_fp",
                               allow_zero=True)
    if filled > initial:
        raise SchemaError("order: fill count exceeds initial count")
    if state is OrderState.RESTING and filled > ZERO:
        state = OrderState.PARTIALLY_FILLED
    ts_raw = payload.get("last_update_time") or payload.get("created_time")
    if ts_raw is None:
        raise SchemaError("order: missing last_update_time/created_time")
    return OrderStatus(
        client_order_id=str(require(payload, "client_order_id", ctx)),
        exchange_order_id=str(require(payload, "order_id", ctx)),
        ticker=str(require(payload, "ticker", ctx)),
        outcome=outcome, state=state, quantity=initial, filled_quantity=filled,
        remaining_quantity=remaining, limit_price=price, ts=parse_ts(ts_raw, "order.ts"),
    )


KNOWN_FILL_MONEY_FIELDS: frozenset[str] = frozenset({
    "count_fp", "yes_price_dollars", "no_price_dollars", "fee_cost",
})


def normalize_fill(payload: Mapping[str, Any]) -> Fill:
    """Fill object from GET /portfolio/fills (shape partially verified; see ADR)."""
    ctx = "fill"
    check_unknown_money_fields(payload, KNOWN_FILL_MONEY_FIELDS - {"fee_cost"}, ctx)
    outcome = _outcome(require(payload, "side", ctx), "side")
    if require(payload, "action", ctx) != "buy":
        raise SchemaError("fill: sell fills unsupported (system only opens long)")
    price_key = "yes_price_dollars" if outcome is Outcome.YES else "no_price_dollars"
    taker = require(payload, "is_taker", ctx)
    if not isinstance(taker, bool):
        raise SchemaError("fill.is_taker must be boolean")
    ts_raw = payload.get("created_time") or payload.get("ts")
    if ts_raw is None:
        raise SchemaError("fill: missing created_time")
    return Fill(
        fill_id=str(require(payload, "fill_id", ctx)),
        client_order_id=str(payload.get("client_order_id") or ""),
        exchange_order_id=str(require(payload, "order_id", ctx)),
        ticker=str(require(payload, "ticker", ctx)),
        outcome=outcome,
        quantity=parse_quantity(require(payload, "count_fp", ctx), "count_fp"),
        price=parse_price(require(payload, price_key, ctx), price_key),
        fee=parse_decimal(require(payload, "fee_cost", ctx), "fee_cost"),
        is_taker=taker, ts=parse_ts(ts_raw, "fill.ts"),
    )


def normalize_position(payload: Mapping[str, Any]) -> Position | None:
    """Market position; signed ``position_fp`` (+YES / -NO). Flat positions -> None."""
    ctx = "position"
    qty = parse_decimal(require(payload, "position_fp", ctx), "position_fp")
    if qty == ZERO:
        return None
    exposure = parse_decimal(require(payload, "market_exposure_dollars", ctx),
                             "market_exposure_dollars")
    fees = parse_decimal(require(payload, "fees_paid_dollars", ctx), "fees_paid_dollars")
    if exposure < ZERO or fees < ZERO:
        raise SchemaError("position: negative exposure/fees")
    return Position(
        ticker=str(require(payload, "ticker", ctx)),
        event_ticker=str(payload.get("event_ticker", "")),
        category="", outcome=Outcome.YES if qty > ZERO else Outcome.NO,
        quantity=abs(qty), cost_basis=exposure + fees,
    )


def normalize_balance(payload: Mapping[str, Any]) -> Decimal:
    """Available cash in dollars from ``balance_dollars`` (string) or ``balance`` (int cents)."""
    dollars = payload.get("balance_dollars")
    cents = payload.get("balance")
    value: Decimal | None = None
    if dollars is not None:
        value = parse_decimal(dollars, "balance_dollars")
    if cents is not None:
        if isinstance(cents, bool) or not isinstance(cents, int):
            raise SchemaError("balance: expected integer cents")
        c = Decimal(cents) / 100
        if value is not None and value != c:
            raise SchemaError("balance and balance_dollars disagree")
        value = c
    if value is None:
        raise SchemaError("balance: neither balance_dollars nor balance present")
    if value < ZERO:
        raise SchemaError("balance: negative")
    return value
