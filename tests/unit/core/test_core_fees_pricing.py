"""Fee formula, rounding, maker/taker, many-fills, and price-grid behaviour."""

from __future__ import annotations

from decimal import Decimal

import pytest

from kalshi_edge.domain import D, PriceLevel, PriceRange
from kalshi_edge.fees import FeeSchedule, round_up
from kalshi_edge.pricing import GridError, floor_to_step, is_on_grid, snap

F = FeeSchedule()


# ----------------------------------------------------------------------------- fees
def test_kalshi_documented_example_100_at_055() -> None:
    # 0.07 * 100 * 0.55 * 0.45 = 1.7325 -> rounded UP to the cent = 1.74
    assert F.fee(D(100), D("0.55")) == D("1.74")


def test_rounding_is_up_never_nearest() -> None:
    # raw = 0.07*1*0.5*0.5 = 0.0175 -> 0.02 (nearest would also be 0.02); use a tiny raw value
    assert F.fee(D(1), D("0.99")) == D("0.01")  # raw 0.000693 -> up to a cent
    assert round_up(D("0.0100000001"), D("0.01")) == D("0.02")
    assert round_up(D("0.01"), D("0.01")) == D("0.01")
    assert round_up(D("0"), D("0.01")) == D(0)


def test_fee_symmetry_around_half_and_zero_at_extremes() -> None:
    assert F.fee(D(10), D("0.20")) == F.fee(D(10), D("0.80"))
    assert F.fee(D(10), D(0)) == 0
    assert F.fee(D(10), D(1)) == 0
    assert F.fee(D(0), D("0.5")) == 0


def test_maker_cheaper_than_taker() -> None:
    t = F.fee(D(100), D("0.55"))
    m = F.fee(D(100), D("0.55"), maker=True)
    assert m == D("0.44")  # 0.0175*100*0.2475 = 0.433125 -> 0.44
    assert m < t


def test_multiplier_and_stress() -> None:
    assert F.fee(D(100), D("0.55"), multiplier=D("0.5")) == D("0.87")  # 0.86625 -> 0.87
    stressed = FeeSchedule(stress_multiplier=D("1.5"))
    assert stressed.fee(D(100), D("0.55")) == D("2.60")  # 2.59875 -> 2.60


def test_fee_validation() -> None:
    with pytest.raises(ValueError):
        F.fee(D(-1), D("0.5"))
    with pytest.raises(ValueError):
        F.fee(D(1), D("1.01"))
    with pytest.raises(ValueError):
        F.fee(D(1), D("-0.01"))
    with pytest.raises(ValueError):
        round_up(D(1), D(0))


def test_per_fill_rounding_at_least_aggregate_many_tiny_fills() -> None:
    one = F.fee(D(100), D("0.55"))
    tiny = F.fee_for_fills([PriceLevel(D("0.55"), D(1))] * 100)
    assert tiny == D("2.00")  # 100 * ceil(0.017325 -> 0.02)
    assert tiny >= one
    assert tiny > one  # strictly more expensive here


def test_fee_for_fills_equals_sum_of_per_fill_fee() -> None:
    fills = [PriceLevel(D("0.85"), D(3)), PriceLevel(D("0.86"), D(7)), PriceLevel(D("0.9"), D(1))]
    assert F.fee_for_fills(fills) == sum((F.fee(f.quantity, f.price) for f in fills), D(0))
    assert F.fee_for_fills([]) == 0


def test_fractional_count_fee() -> None:
    assert F.fee(D("0.5"), D("0.55")) == D("0.01")  # raw 0.0086625


def test_finer_rounding_increment_is_less_conservative_but_valid() -> None:
    fine = FeeSchedule(rounding_increment=D("0.0001"))
    assert fine.fee(D(100), D("0.55")) == D("1.7325")


# ---------------------------------------------------------------------------- grids
LINEAR = (PriceRange(D("0.01"), D("0.99"), D("0.01")),)
TAPERED = (
    PriceRange(D("0.001"), D("0.100"), D("0.001")),
    PriceRange(D("0.100"), D("0.900"), D("0.010")),
    PriceRange(D("0.900"), D("0.999"), D("0.001")),
)


@pytest.mark.parametrize(("price", "ok"), [
    ("0.55", True), ("0.555", False), ("0.01", True), ("0.99", True), ("1.00", False),
    ("0.00", False), ("0.005", False), ("-0.5", False),
])
def test_linear_grid_membership(price: str, ok: bool) -> None:
    assert is_on_grid(D(price), LINEAR) is ok


@pytest.mark.parametrize(("price", "ok"), [
    ("0.0955", False), ("0.095", True), ("0.100", True), ("0.105", False), ("0.15", True),
    ("0.155", False), ("0.900", True), ("0.9005", False), ("0.951", True), ("0.9991", False),
])
def test_tapered_grid_membership(price: str, ok: bool) -> None:
    assert is_on_grid(D(price), TAPERED) is ok


@pytest.mark.parametrize(("price", "down", "up"), [
    ("0.0955", "0.095", "0.096"), ("0.095", "0.095", "0.095"), ("0.1234", "0.12", "0.13"),
    ("0.8765", "0.87", "0.88"), ("0.9005", "0.900", "0.901"), ("0.55", "0.55", "0.55"),
])
def test_snap_down_and_up_tapered(price: str, down: str, up: str) -> None:
    assert snap(D(price), TAPERED, up=False) == D(down)
    assert snap(D(price), TAPERED, up=True) == D(up)
    assert is_on_grid(snap(D(price), TAPERED, up=True), TAPERED)
    assert is_on_grid(snap(D(price), TAPERED, up=False), TAPERED)


def test_snap_down_never_above_up_never_below_input() -> None:
    for raw in ("0.1234", "0.3333", "0.5051", "0.0123", "0.9876"):
        p = D(raw)
        assert snap(p, TAPERED, up=False) <= p <= snap(p, TAPERED, up=True)


def test_snap_fails_closed() -> None:
    with pytest.raises(GridError):
        snap(D("0.5"), (), up=True)  # missing price_ranges: refuse to guess a tick
    with pytest.raises(GridError):
        snap(D("1.5"), LINEAR, up=False)  # outside all bands
    with pytest.raises(GridError):
        snap(D("0.1045"), (PriceRange(D("0.01"), D("0.105"), D("0.01")),), up=True)  # > band end


def test_is_on_grid_false_when_no_ranges() -> None:
    assert is_on_grid(D("0.5"), ()) is False


def test_floor_to_step_integer_and_fractional() -> None:
    assert floor_to_step(D("7.9"), D(1)) == 7
    assert floor_to_step(D("1.239"), D("0.01")) == D("1.23")
    assert floor_to_step(D("0.009"), D("0.01")) == 0
    assert floor_to_step(D("2.5"), D("0.5")) == D("2.5")
    with pytest.raises(ValueError):
        floor_to_step(D(1), D(0))


def test_decimal_helper_refuses_floats() -> None:
    with pytest.raises(TypeError):
        D(0.1)  # type: ignore[arg-type]
    assert isinstance(D("0.1"), Decimal)
