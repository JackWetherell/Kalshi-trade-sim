from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st
from strat_helpers import make_book

from kalshi_edge.domain import Outcome
from kalshi_edge.fees import FeeSchedule
from kalshi_edge.strategies.sizing import (
    cash_outlay,
    contracts_for_budget,
    kelly_budget,
    kelly_fraction_binary,
    raw_scaled_curve,
    scaled_risk_budget,
)

D = Decimal
FEES = FeeSchedule()


def test_contracts_respect_budget_and_are_maximal() -> None:
    book = make_book(yes_asks=[("0.85", "100"), ("0.90", "100")])
    n = contracts_for_budget(D("20"), book, Outcome.YES, FEES, D(1), D(1), D(500))
    assert cash_outlay(book, Outcome.YES, n, FEES, D(1)) <= D("20")
    assert cash_outlay(book, Outcome.YES, n + 1, FEES, D(1)) > D("20")


def test_walks_depth_and_limited_by_depth() -> None:
    book = make_book(yes_asks=[("0.85", "3"), ("0.90", "2")])
    assert contracts_for_budget(D("1000"), book, Outcome.YES, FEES, D(1), D(1), D(500)) == D(5)


def test_near_one_does_not_oversize() -> None:
    book = make_book(yes_asks=[("0.99", "1000")])
    n = contracts_for_budget(D("10"), book, Outcome.YES, FEES, D(1), D(1), D(500))
    assert n == D(10)  # 10 * 0.99 = 9.90 + fee 0.01 = 9.91; 11 would cost > 10
    assert cash_outlay(book, Outcome.YES, n, FEES, D(1)) <= D("10")


def test_step_rounding_down_and_max_contracts() -> None:
    book = make_book(yes_asks=[("0.50", "1000")])
    n = contracts_for_budget(D("10"), book, Outcome.YES, FEES, D(1), D("0.5"), D("500"))
    assert n % D("0.5") == 0 and cash_outlay(book, Outcome.YES, n, FEES, D(1)) <= D(10)
    assert contracts_for_budget(D("1000"), book, Outcome.YES, FEES, D(1), D(1), D(7)) == D(7)


def test_zero_budget_or_empty_book() -> None:
    book = make_book(yes_asks=[("0.85", "10")])
    assert contracts_for_budget(D(0), book, Outcome.YES, FEES, D(1), D(1), D(5)) == 0
    assert contracts_for_budget(D(10), book, Outcome.NO, FEES, D(1), D(1), D(5)) == 0


@given(budget=st.decimals(min_value="0.5", max_value="500", places=2),
       price=st.integers(min_value=81, max_value=99),
       qty=st.integers(min_value=1, max_value=300),
       step=st.sampled_from([D(1), D("0.5"), D("0.01")]))
def test_property_never_exceeds_budget(budget: Decimal, price: int, qty: int,
                                       step: Decimal) -> None:
    book = make_book(yes_asks=[(f"{price / 100:.2f}", str(qty)),
                               (f"{min(price + 1, 99) / 100:.2f}", "50")])
    n = contracts_for_budget(budget, book, Outcome.YES, FEES, D(1), step, D(500))
    assert n % step == 0
    assert cash_outlay(book, Outcome.YES, n, FEES, D(1)) <= budget


def test_kelly_formula() -> None:
    assert kelly_fraction_binary(D("0.95"), D("0.90")) == D("0.5")
    assert kelly_fraction_binary(D("0.90"), D("0.90")) == 0
    assert kelly_fraction_binary(D("0.80"), D("0.90")) == 0
    assert kelly_fraction_binary(D("1"), D("1")) == 0
    assert kelly_budget(D("0.95"), D("0.90"), D("1000"), D("0.1")) == D("50")
    assert kelly_budget(D("0.85"), D("0.90"), D("1000"), D("0.1")) == 0


def test_scaled_curve_endpoints_and_monotone() -> None:
    lo, hi = D(2), D(10)
    assert scaled_risk_budget(D("0.80"), lo, hi) == lo
    assert scaled_risk_budget(D("0.50"), lo, hi) == lo
    assert scaled_risk_budget(D("0.90"), lo, hi) == D(6)
    assert scaled_risk_budget(D("1.00"), lo, hi) == hi
    pts = [D(i) / 100 for i in range(70, 101)]
    curve = raw_scaled_curve(pts, lo, hi)
    budgets = [b for _, b in curve]
    assert budgets == sorted(budgets) and len(curve) == len(pts)
