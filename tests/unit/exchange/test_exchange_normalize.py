from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from conftest import fx

from kalshi_edge.domain import MarketStatus, OrderState, Outcome
from kalshi_edge.exchange.normalize import (
    SeriesInfo,
    is_supported_binary,
    normalize_balance,
    normalize_fill,
    normalize_market,
    normalize_order,
    normalize_orderbook,
    normalize_position,
    normalize_series,
)
from kalshi_edge.exchange.schema import SCHEMA_VERSION, SchemaError
from kalshi_edge.pricing import is_on_grid, snap

D = Decimal
TS = datetime(2026, 10, 12, tzinfo=UTC)
SERIES = SeriesInfo("KXHIGHNY", "t", "Climate", "quadratic", D("1"))


def test_binary_tapered_market() -> None:
    m = normalize_market(fx("market_binary_tapered.json")["market"], series=SERIES)
    assert m.status is MarketStatus.OPEN and m.series_ticker == "KXHIGHNY"
    assert m.strike["strike_type"] == "greater" and m.strike["floor_strike"] == D("75")
    assert is_on_grid(D("0.912"), m.price_ranges) and not is_on_grid(D("0.812"), m.price_ranges)
    assert is_on_grid(D("0.0155"), m.price_ranges) is False
    assert is_on_grid(D("0.007"), m.price_ranges)
    assert snap(D("0.9124"), m.price_ranges, up=True) == D("0.913")
    assert m.schema_version == SCHEMA_VERSION and m.quantity_step == D("1")


def test_fractional_requires_opt_in() -> None:
    p = fx("market_fractional.json")["market"]
    assert normalize_market(p, series=SERIES).quantity_step == D("1")
    assert normalize_market(p, series=SERIES, allow_fractional=True).quantity_step == D("0.01")


@pytest.mark.parametrize("name,frag", [
    ("market_scalar.json", "binary"), ("market_mve.json", "multivariate"),
    ("market_closed.json", "not open"), ("market_no_price_ranges.json", "price_ranges")])
def test_unsupported(name: str, frag: str) -> None:
    ok, reason = is_supported_binary(fx(name)["market"])
    assert not ok and frag in reason
    with pytest.raises(SchemaError):
        normalize_market(fx(name)["market"], series=SERIES)


def test_supported_ok_and_unknown_money_field() -> None:
    assert is_supported_binary(fx("market_binary_tapered.json")["market"]) == (True, "ok")
    with pytest.raises(SchemaError, match="unknown money"):
        normalize_market(fx("market_unknown_money_field.json")["market"], series=SERIES)


def test_missing_fee_info_fails_closed() -> None:
    with pytest.raises(SchemaError, match="fee"):
        normalize_market(fx("market_binary_tapered.json")["market"])


def test_series_fee_type() -> None:
    assert normalize_series(fx("series.json")["series"]).fee_multiplier == D("1")
    with pytest.raises(SchemaError):
        normalize_series(fx("series_bad_fee.json")["series"])


def test_float_money_rejected() -> None:
    p = dict(fx("market_binary_tapered.json")["market"])
    p["price_ranges"] = [{"start": 0.01, "end": "0.99", "step": "0.01"}]
    with pytest.raises(SchemaError):
        normalize_market(p, series=SERIES)


def test_orderbook_parse_asks() -> None:
    b = normalize_orderbook(fx("orderbook.json"), ticker="T", ts=TS)
    assert b.best_bid(Outcome.YES).price == D("0.8200")  # type: ignore[union-attr]
    assert b.best_ask(Outcome.YES).price == D("0.8400")  # type: ignore[union-attr]
    assert b.best_ask(Outcome.NO).price == D("0.1800")  # type: ignore[union-attr]
    assert not b.is_crossed_or_locked


def test_orderbook_empty_crossed_subcent_bad() -> None:
    e = normalize_orderbook(fx("orderbook_empty.json"), ticker="T", ts=TS)
    assert e.yes_bids == () and e.best_ask(Outcome.YES) is None
    assert normalize_orderbook(fx("orderbook_crossed.json"), ticker="T", ts=TS).is_crossed_or_locked
    s = normalize_orderbook(fx("orderbook_subcent.json"), ticker="T", ts=TS)
    assert s.best_bid(Outcome.YES).price == D("0.9130")  # type: ignore[union-attr]
    assert s.best_bid(Outcome.YES).quantity == D("7.25")  # type: ignore[union-attr]
    with pytest.raises(SchemaError):
        normalize_orderbook(fx("orderbook_bad_keys.json"), ticker="T", ts=TS)
    with pytest.raises(SchemaError):
        normalize_orderbook({"orderbook_fp": {"yes_dollars": None, "no_dollars": []}},
                            ticker="T", ts=TS)
    dup = {"orderbook_fp": {"yes_dollars": [["0.5000", "1.00"], ["0.5000", "2.00"]],
                            "no_dollars": []}}
    with pytest.raises(SchemaError, match="duplicate"):
        normalize_orderbook(dup, ticker="T", ts=TS)


def test_orders_fills_positions_balance() -> None:
    o = normalize_order(fx("orders_page2.json")["orders"][0])
    assert o.outcome is Outcome.NO and o.state is OrderState.PARTIALLY_FILLED
    assert o.limit_price == D("0.8200") and o.filled_quantity == D("3.00")
    f = normalize_fill(fx("fills.json")["fills"][0])
    assert f.price == D("0.8200") and f.fee == D("0.0400") and f.is_taker
    pos = [normalize_position(p) for p in fx("positions.json")["market_positions"]]
    assert pos[1] is None and pos[0] is not None
    assert pos[0].outcome is Outcome.NO and pos[0].cost_basis == D("2.5000")
    assert normalize_balance(fx("balance.json")) == D("1000.50")
    with pytest.raises(SchemaError):
        normalize_balance({"balance": 100, "balance_dollars": "2.00"})
    bad = dict(fx("fills.json")["fills"][0])
    del bad["fee_cost"]
    with pytest.raises(SchemaError):
        normalize_fill(bad)
