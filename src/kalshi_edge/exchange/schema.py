"""Schema constants and strict primitive parsers for Kalshi payloads.

Anything that affects money fails closed with :class:`SchemaError`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

#: Version of the payload shapes this adapter was written against, and the date they were
#: (partially) verified via web-search snippets of docs.kalshi.com. The container could not reach
#: Kalshi hosts, so NO payload was verified against a live response. See docs/ADR_exchange.md.
SCHEMA_VERSION = (
    "kalshi-trade-api-v2/fixed-point-2026-03; docs-snippets-verified-2026-10-12; live-unverified"
)


class SchemaError(ValueError):
    """Payload is missing, has unexpected, or has malformed fields that affect money."""


def require(payload: Mapping[str, Any], key: str, ctx: str) -> Any:
    if not isinstance(payload, Mapping):
        raise SchemaError(f"{ctx}: expected object, got {type(payload).__name__}")
    if key not in payload or payload[key] is None:
        raise SchemaError(f"{ctx}: missing required field {key!r}")
    return payload[key]


def parse_decimal(value: Any, field: str) -> Decimal:
    """Parse a fixed-point *string* ("0.4200"). Floats/bools/ints are rejected."""
    if not isinstance(value, str) or isinstance(value, bool):
        raise SchemaError(f"{field}: expected fixed-point string, got {type(value).__name__}")
    try:
        out = Decimal(value.strip())
    except InvalidOperation:
        raise SchemaError(f"{field}: not a decimal: {value!r}") from None
    if not out.is_finite():
        raise SchemaError(f"{field}: non-finite decimal")
    return out


def parse_json_number(value: Any, field: str) -> Decimal:
    """For JSON *numbers* documented as such (e.g. series fee_multiplier). Uses repr, not binary."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise SchemaError(f"{field}: expected number, got {type(value).__name__}")
    try:
        out = Decimal(repr(value) if isinstance(value, float) else str(value))
    except InvalidOperation:
        raise SchemaError(f"{field}: not a number: {value!r}") from None
    if not out.is_finite():
        raise SchemaError(f"{field}: non-finite number")
    return out


def parse_price(value: Any, field: str) -> Decimal:
    p = parse_decimal(value, field)
    if not (Decimal(0) < p < Decimal(1)):
        raise SchemaError(f"{field}: price {p} outside (0, 1)")
    return p


def parse_quantity(value: Any, field: str, *, allow_zero: bool = False) -> Decimal:
    q = parse_decimal(value, field)
    if q < 0 or (q == 0 and not allow_zero):
        raise SchemaError(f"{field}: invalid quantity {q}")
    return q


def parse_ts(value: Any, field: str) -> datetime:
    """ISO-8601 string (with Z/offset) or epoch seconds/ms integer -> aware UTC datetime."""
    if isinstance(value, bool):
        raise SchemaError(f"{field}: invalid timestamp")
    if isinstance(value, int):
        secs = value / 1000 if value > 10**11 else value
        return datetime.fromtimestamp(secs, UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise SchemaError(f"{field}: bad timestamp {value!r}") from None
        if dt.tzinfo is None:
            raise SchemaError(f"{field}: naive timestamp {value!r}")
        return dt.astimezone(UTC)
    raise SchemaError(f"{field}: invalid timestamp type {type(value).__name__}")


def check_unknown_money_fields(payload: Mapping[str, Any], known: frozenset[str], ctx: str,
                               *, strict: bool = True) -> None:
    """Fail closed if the payload carries money-looking fields we do not understand."""
    if not strict:
        return
    unknown = sorted(k for k in payload
                     if k.endswith(("_dollars", "_fp", "_cents")) and k not in known)
    if unknown:
        raise SchemaError(f"{ctx}: unknown money fields {unknown}")
