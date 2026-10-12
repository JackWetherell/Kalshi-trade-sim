"""Execution venues: demo (mock funds) and live (deny-by-default).

Order mapping (Create Order V2, ``POST /portfolio/events/orders``): ``side`` is relative to the
YES leg. Buying YES at ``p`` is ``bid`` at ``p``; buying NO at ``p`` is ``ask`` at ``1 - p``.
Submit never retries; a timeout raises :class:`AmbiguousSubmission` and the caller must call
``get_order(client_order_id)`` before any further attempt.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from kalshi_edge.config import ExchangeConfig
from kalshi_edge.domain import (
    ONE,
    ZERO,
    AccountSnapshot,
    Fill,
    Mode,
    OrderIntent,
    OrderState,
    OrderStatus,
    Outcome,
    TimeInForce,
)
from kalshi_edge.exchange.auth import RsaPssSigner
from kalshi_edge.exchange.hosts import (
    DEMO_REST_BASE,
    PROD_REST_BASE,
    LiveAuthorization,
    ProductionOrderGuard,
    host_of,
    is_demo_host,
)
from kalshi_edge.exchange.normalize import (
    normalize_balance,
    normalize_fill,
    normalize_order,
    normalize_position,
)
from kalshi_edge.exchange.rest import (
    HttpStatusError,
    KalshiError,
    KalshiRestClient,
)
from kalshi_edge.exchange.schema import (
    SchemaError,
    parse_quantity,
    parse_ts,
    require,
)
from kalshi_edge.execution.errors import (
    AmbiguousSubmission,
    LiveTradingDisabled,
    OrderRejected,
)
from kalshi_edge.interfaces import Clock

CREATE_ORDER_PATH = "/portfolio/events/orders"
ORDERS_LIST_PATH = "/portfolio/orders"  # UNVERIFIED: legacy path reported deprecated; see ADR
FILLS_PATH = "/portfolio/fills"
BALANCE_PATH = "/portfolio/balance"
POSITIONS_PATH = "/portfolio/positions"
#: Operator-provisioned PRODUCTION credential env vars (never set by this repo or agents).
PROD_KEY_ID_ENV = "KALSHI_PROD_KEY_ID"
PROD_KEY_PATH_ENV = "KALSHI_PROD_PRIVATE_KEY_PATH"

#: Value UNVERIFIED (docs snippets did not list the enum); a wrong value is rejected by the
#: exchange, never silently accepted.
SELF_TRADE_PREVENTION = "taker_at_cross"

PRICE_DP = Decimal("0.0001")
COUNT_DP = Decimal("0.01")


class CredentialsMissing(KalshiError):
    pass


class VenueConfigError(KalshiError):
    pass


def _fixed(value: Decimal, quantum: Decimal, field: str) -> str:
    q = value.quantize(quantum)
    if q != value:
        raise ValueError(f"{field}={value} not representable at {quantum} resolution")
    return format(q, "f")


def build_create_order_body(intent: OrderIntent) -> dict[str, Any]:
    """Map a buy intent to the V2 body. NO buys become YES-relative asks at 1 - price."""
    if intent.quantity <= ZERO:
        raise ValueError("quantity must be positive")
    if not (ZERO < intent.limit_price < ONE):
        raise ValueError("limit_price must be inside (0, 1)")
    if intent.outcome is Outcome.YES:
        side, yes_price = "bid", intent.limit_price
    else:
        side, yes_price = "ask", ONE - intent.limit_price
    return {
        "ticker": intent.ticker,
        "client_order_id": intent.client_order_id,
        "side": side,
        "count": _fixed(intent.quantity, COUNT_DP, "quantity"),
        "price": _fixed(yes_price, PRICE_DP, "price"),
        "time_in_force": TimeInForce(intent.time_in_force).value,
        "self_trade_prevention_type": SELF_TRADE_PREVENTION,
        "post_only": False,
        "reduce_only": False,
        "cancel_order_on_pause": True,
    }


def status_from_create_response(intent: OrderIntent, data: Mapping[str, Any],
                                now: datetime) -> OrderStatus:
    ctx = "create_order"
    order_id = str(require(data, "order_id", ctx))
    echoed = data.get("client_order_id")
    if echoed is not None and echoed != intent.client_order_id:
        raise SchemaError("create_order: client_order_id mismatch")
    filled = parse_quantity(require(data, "fill_count", ctx), "fill_count", allow_zero=True)
    remaining = parse_quantity(require(data, "remaining_count", ctx), "remaining_count",
                               allow_zero=True)
    if filled > intent.quantity:
        raise SchemaError("create_order: filled more than requested")
    ts_raw = data.get("ts_ms")
    ts = parse_ts(ts_raw, "ts_ms") if isinstance(ts_raw, int) else now
    gtc = intent.time_in_force is TimeInForce.GOOD_TILL_CANCELED
    if filled == intent.quantity:
        state = OrderState.FILLED
    elif remaining > ZERO and gtc:
        state = OrderState.PARTIALLY_FILLED if filled > ZERO else OrderState.RESTING
    else:  # IOC/FOK remainder (or nothing left to rest) is cancelled
        state = OrderState.CANCELED
    open_ = state.is_open
    return OrderStatus(
        client_order_id=intent.client_order_id, exchange_order_id=order_id,
        ticker=intent.ticker, outcome=intent.outcome, state=state, quantity=intent.quantity,
        filled_quantity=filled, remaining_quantity=remaining if open_ else ZERO,
        limit_price=intent.limit_price, ts=ts)


class _KalshiExecutionVenue:
    mode: Mode
    _base_url: str

    def __init__(self, client: KalshiRestClient, clock: Clock) -> None:
        self._client = client
        self._clk = clock

    def _clock(self) -> datetime:
        return self._clk.now()

    # -- gate for mutating calls; overridden by live ------------------------------------------
    def _authorize_mutation(self) -> None:  # pragma: no cover - overridden
        raise NotImplementedError

    def submit(self, intent: OrderIntent) -> OrderStatus:
        self._authorize_mutation()
        if intent.mode is not self.mode:
            raise VenueConfigError(f"intent mode {intent.mode} != venue mode {self.mode}")
        body = build_create_order_body(intent)
        try:
            data = self._client.request_json("POST", CREATE_ORDER_PATH, json_body=body)
        except HttpStatusError as exc:
            if 400 <= exc.status < 500 and exc.status != 429:
                raise OrderRejected(str(exc)) from exc
            raise
        return status_from_create_response(intent, data, self._clock())

    def cancel(self, client_order_id: str) -> OrderStatus | None:
        self._authorize_mutation()
        current = self.get_order(client_order_id)
        if current is None:
            return None
        if current.state.is_terminal or current.exchange_order_id is None:
            return current
        self._client.request_json("DELETE",
                                  f"{CREATE_ORDER_PATH}/{current.exchange_order_id}")
        after = self.get_order(client_order_id)
        if after is not None:
            return after
        return replace(current, state=OrderState.CANCELED, remaining_quantity=ZERO,
                       ts=self._clock())

    def get_order(self, client_order_id: str) -> OrderStatus | None:
        for payload in self._client.paginate(ORDERS_LIST_PATH, "orders"):
            if payload.get("client_order_id") == client_order_id:
                return normalize_order(payload)
        return None

    def list_open_orders(self) -> Sequence[OrderStatus]:
        return [normalize_order(p) for p in
                self._client.paginate(ORDERS_LIST_PATH, "orders", {"status": "resting"})]

    def get_fills(self, since: datetime | None = None) -> Sequence[Fill]:
        params: dict[str, Any] = {}
        if since is not None:
            params["min_ts"] = int(since.timestamp())
        return [normalize_fill(p) for p in
                self._client.paginate(FILLS_PATH, "fills", params)]

    def get_account(self) -> AccountSnapshot:
        now = self._clock()
        balance = normalize_balance(self._client.get(BALANCE_PATH))
        positions = []
        for p in self._client.paginate(POSITIONS_PATH, "market_positions"):
            pos = normalize_position(p)
            if pos is not None:
                positions.append(pos)
        return AccountSnapshot(ts=now, cash=balance, positions=tuple(positions),
                               open_orders=tuple(self.list_open_orders()),
                               source=f"{self._base_url}")


def _load_signer(key_id_env: str, key_path_env: str, env: Mapping[str, str]) -> RsaPssSigner:
    key_id, key_path = env.get(key_id_env), env.get(key_path_env)
    if not key_id or not key_path:
        raise CredentialsMissing(
            f"missing credentials: set env vars {key_id_env} and {key_path_env}")
    return RsaPssSigner.from_pem_path(key_id, key_path)


class DemoExecutionVenue(_KalshiExecutionVenue):
    mode = Mode.DEMO

    def __init__(self, config: ExchangeConfig, clock: Clock,
                 transport: httpx.BaseTransport | None = None, *,
                 base_url: str = DEMO_REST_BASE,
                 env: Mapping[str, str] | None = None, signer: RsaPssSigner | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if config.environment != "demo":
            raise VenueConfigError("DemoExecutionVenue requires exchange.environment == 'demo'")
        if not is_demo_host(host_of(base_url)):
            raise VenueConfigError(f"refusing non-demo host {host_of(base_url)!r}")
        signer = signer or _load_signer(config.demo_key_id_env, config.demo_private_key_path_env,
                                        env if env is not None else os.environ)
        client = KalshiRestClient(
            base_url, signer=signer, guard=ProductionOrderGuard(None), transport=transport,
            timeout=config.request_timeout_seconds,
            max_requests_per_second=config.max_requests_per_second, sleep=sleep)
        super().__init__(client, clock)
        self._base_url = base_url

    def _authorize_mutation(self) -> None:
        return None


class LiveExecutionVenue(_KalshiExecutionVenue):
    """Production code path. Deny-by-default: constructed with a LiveAuthorization AND
    re-checked on every mutating call (and again, per request, by the transport guard)."""

    mode = Mode.LIVE

    def __init__(self, config: ExchangeConfig, clock: Clock,
                 authorization: LiveAuthorization | None,
                 transport: httpx.BaseTransport | None = None, *,
                 signer: RsaPssSigner | None = None, base_url: str = PROD_REST_BASE,
                 env: Mapping[str, str] | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        if config.environment != "prod":
            raise LiveTradingDisabled("exchange.environment is not 'prod'")
        if authorization is None:
            raise LiveTradingDisabled("no live authorization supplied")
        if not authorization.is_valid(clock.now()):
            raise LiveTradingDisabled("live authorization invalid or expired")
        if authorization.host.lower() != host_of(base_url):
            raise LiveTradingDisabled("live authorization is for a different host")
        self._authorization = authorization
        signer = signer or _load_signer(PROD_KEY_ID_ENV, PROD_KEY_PATH_ENV,
                                        env if env is not None else os.environ)
        client = KalshiRestClient(
            base_url, signer=signer, guard=ProductionOrderGuard(authorization, clock=clock.now),
            transport=transport, timeout=config.request_timeout_seconds,
            max_requests_per_second=config.max_requests_per_second, sleep=sleep)
        super().__init__(client, clock)
        self._base_url = base_url

    def _authorize_mutation(self) -> None:
        auth = self._authorization
        if auth is None or not auth.is_valid(self._clock()):
            raise LiveTradingDisabled("live authorization invalid or expired")


__all__ = [
    "AmbiguousSubmission",
    "CredentialsMissing",
    "DemoExecutionVenue",
    "HttpStatusError",
    "LiveExecutionVenue",
    "LiveTradingDisabled",
    "OrderRejected",
    "VenueConfigError",
    "build_create_order_body",
    "status_from_create_response",
]
