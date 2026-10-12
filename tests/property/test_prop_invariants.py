"""Hypothesis property tests for money math, risk caps and paper accounting."""

from __future__ import annotations

from decimal import Decimal

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from kalshi_edge.domain import (
    AccountSnapshot,
    Mode,
    OrderBook,
    OrderIntent,
    OrderState,
    Outcome,
    PriceLevel,
    PriceRange,
    SettlementResult,
    StrategyId,
    TimeInForce,
)
from kalshi_edge.execution import PaperVenue
from kalshi_edge.fees import FeeSchedule
from kalshi_edge.pricing import (
    GridError,
    executable_quote,
    floor_to_step,
    is_on_grid,
    snap,
    walk_asks,
)
from kalshi_edge.risk import Exposure, ExposureItem, HardRiskManager
from kalshi_edge.runtime.clock import ReplayClock
from tests.support import FEES, T0, TICKER, estimate, limits, market, proposal, risk_ctx

D = Decimal
CENTS = st.integers(1, 99).map(lambda n: D(n) / 100)
QTY = st.integers(1, 500).map(D)
FAST = settings(max_examples=120, deadline=None,
                suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture])


# ----------------------------------------------------------------------------- fees
@FAST
@given(p=CENTS, fills=st.lists(st.integers(1, 200), min_size=1, max_size=40),
       maker=st.booleans())
def test_per_fill_rounding_never_cheaper_than_aggregate(p: Decimal, fills: list[int],
                                                        maker: bool) -> None:
    total = D(sum(fills))
    split = FEES.fee_for_fills([PriceLevel(p, D(n)) for n in fills], maker=maker)
    assert split >= FEES.fee(total, p, maker=maker)
    assert split <= FEES.fee(total, p, maker=maker) + D("0.01") * len(fills)


@FAST
@given(p=CENTS, n=QTY, m=st.integers(1, 100).map(lambda x: D(x) / 100))
def test_fee_nonnegative_cent_rounded_and_at_least_raw(p: Decimal, n: Decimal, m: Decimal) -> None:
    fee = FEES.fee(n, p, multiplier=m)
    raw = D("0.07") * m * n * p * (1 - p)
    assert fee >= raw >= 0
    assert fee - raw < D("0.01")
    assert fee == fee.quantize(D("0.01"))


@FAST
@given(p=CENTS, n=QTY)
def test_maker_never_costs_more_than_taker(p: Decimal, n: Decimal) -> None:
    assert FEES.fee(n, p, maker=True) <= FEES.fee(n, p)


@FAST
@given(p=CENTS, n=QTY, k=st.integers(1, 50))
def test_fee_monotone_in_count(p: Decimal, n: Decimal, k: int) -> None:
    assert FEES.fee(n + k, p) >= FEES.fee(n, p)


# ---------------------------------------------------------------------------- grids
BANDS = (PriceRange(D("0.001"), D("0.100"), D("0.001")),
         PriceRange(D("0.100"), D("0.900"), D("0.010")),
         PriceRange(D("0.900"), D("0.999"), D("0.001")))


@FAST
@given(raw=st.integers(1, 9990).map(lambda n: D(n) / 10000))
def test_snap_lands_on_grid_and_brackets_the_input(raw: Decimal) -> None:
    try:
        down, up = snap(raw, BANDS, up=False), snap(raw, BANDS, up=True)
    except GridError:
        return  # fails closed is acceptable; never an off-grid answer
    assert is_on_grid(down, BANDS) and is_on_grid(up, BANDS)
    assert down <= raw <= up
    if is_on_grid(raw, BANDS):
        assert down == up == raw


@FAST
@given(q=st.decimals(min_value=0, max_value=10000, places=4),
       step=st.sampled_from([D(1), D("0.1"), D("0.01"), D("0.5")]))
def test_floor_to_step_is_multiple_not_above(q: Decimal, step: Decimal) -> None:
    f = floor_to_step(q, step)
    assert f <= q and q - f < step and f % step == 0


# ----------------------------------------------------------------------------- books
levels = st.lists(st.tuples(st.integers(1, 99), st.integers(1, 300)), min_size=0, max_size=8,
                  unique_by=lambda t: t[0])


def mk_book(yes: list[tuple[int, int]], no: list[tuple[int, int]]) -> OrderBook:
    return OrderBook(TICKER, tuple(PriceLevel(D(p) / 100, D(q)) for p, q in sorted(yes)),
                     tuple(PriceLevel(D(p) / 100, D(q)) for p, q in sorted(no)), T0)


@FAST
@given(yes=levels, no=levels)
def test_asks_are_mirror_of_opposite_bids(yes: list[tuple[int, int]],
                                          no: list[tuple[int, int]]) -> None:
    b = mk_book(yes, no)
    ay, an = b.asks(Outcome.YES), b.asks(Outcome.NO)
    assert sorted((1 - lv.price, lv.quantity) for lv in ay) == sorted(
        (lv.price, lv.quantity) for lv in b.no_bids)
    assert sorted((1 - lv.price, lv.quantity) for lv in an) == sorted(
        (lv.price, lv.quantity) for lv in b.yes_bids)
    assert [x.price for x in ay] == sorted(x.price for x in ay)
    for side in (ay, an):
        assert all(0 < lv.price < 1 for lv in side)


@FAST
@given(yes=levels, no=levels, qty=QTY)
def test_quote_bounds(yes: list[tuple[int, int]], no: list[tuple[int, int]], qty: Decimal) -> None:
    b = mk_book(yes, no)
    for outcome in Outcome:
        q = executable_quote(b, outcome, qty, FEES)
        if q is None:
            assert b.is_crossed_or_locked or not b.asks(outcome)
            continue
        depth = sum((lv.quantity for lv in b.asks(outcome)), D(0))
        assert q.fillable_quantity == min(qty, depth)
        assert q.best_price <= q.vwap <= q.worst_price
        fills = walk_asks(b, outcome, qty)
        assert q.vwap * q.fillable_quantity == sum((f.price * f.quantity for f in fills), D(0))
        assert q.fee_total == FEES.fee_for_fills(fills)


# ----------------------------------------------------------- risk manager invariants
@FAST
@given(
    qty=st.integers(1, 100000).map(D),
    ask=st.integers(81, 97),
    depth=st.integers(30, 5000),
    capital=st.integers(50, 20000).map(D),
    exposure=st.integers(0, 5000).map(D),
    cash_frac=st.integers(1, 100),
    q_safe=st.integers(80, 100),
)
def test_risk_decision_never_exceeds_any_cap_and_never_grows(
        qty: Decimal, ask: int, depth: int, capital: Decimal, exposure: Decimal, cash_frac: int,
        q_safe: int) -> None:
    lim = limits()
    rm = HardRiskManager(lim, FeeSchedule(), slippage_buffer=D("0.005"))
    bk = OrderBook(TICKER, (PriceLevel(D(ask - 2) / 100, D(10)),),
                   (PriceLevel(D(100 - ask) / 100, D(depth)),), T0)
    cash = capital * cash_frac / 100
    ctx = risk_ctx(bk=bk, capital=str(capital), account=AccountSnapshot(T0, cash, (), ()),
                   exposure=Exposure([ExposureItem("X", ("event:X",), exposure, "position")]))
    qs = D(q_safe) / 100
    p = proposal(qty=str(qty), limit=str(D(ask) / 100), est=estimate(q=str(qs), q_safe=str(qs)))
    d = rm.check(p, ctx)
    assert d.quantity <= qty
    if not d.approved:
        assert d.quantity == 0 and d.max_loss == 0 and d.reasons
        return
    assert 0 < d.quantity <= qty
    assert d.max_loss == rm.max_loss_for(p, bk, d.quantity)
    assert d.max_loss <= capital * lim.per_entry_max_loss_frac
    assert d.max_loss <= capital * lim.aggregate_open_loss_frac - exposure
    assert d.max_loss <= capital * lim.cluster_loss_frac - exposure * 0
    assert d.max_loss <= cash - lim.fee_reserve
    # edge on the final quantity: q_safe strictly above all-in breakeven
    assert qs > D(d.metrics["vwap"]) + D(d.metrics["fee_per_contract"])
    # near-$1 prices still bound dollars, not contracts
    assert d.quantity * D(ask) / 100 <= d.max_loss


@FAST
@given(kill=st.booleans(), halt=st.sampled_from(["daily_halt", "weekly_halt", "drawdown_kill",
                                                 None]),
       sid=st.sampled_from(list(StrategyId)), mode=st.sampled_from(list(Mode)))
def test_kill_and_latches_dominate_every_strategy_and_mode(
        kill: bool, halt: str | None, sid: StrategyId, mode: Mode) -> None:
    from kalshi_edge.risk import RiskState
    rm = HardRiskManager(limits(), FEES)
    state = RiskState.fresh(D(1009), T0)
    if halt:
        setattr(state, halt, True)
    d = rm.check(proposal(strategy=sid), risk_ctx(kill=kill, state=state, mode=mode))
    if kill or halt:
        assert not d.approved and d.quantity == 0


@FAST
@given(eq=st.integers(1, 10**6).map(lambda n: D(n) / 100),
       frac=st.sampled_from([D("0.03"), D("0.02"), D("0.01")]))
def test_daily_stop_equality_trips_and_epsilon_above_does_not(eq: Decimal, frac: Decimal) -> None:
    from kalshi_edge.risk import RiskState
    lim = limits(daily_stop_frac=frac) if frac <= D("0.03") else limits()
    s = RiskState.fresh(eq, T0)
    floor = eq * (1 - lim.daily_stop_frac)
    s.mark(floor + D("0.0000001"), T0)
    assert "daily_stop" not in s.evaluate_stops(lim)
    s.mark(floor, T0)
    assert "daily_stop" in s.evaluate_stops(lim)


# -------------------------------------------------------------- paper accounting identity
orders = st.lists(
    st.tuples(st.integers(1, 120), st.integers(80, 99),
              st.sampled_from([Outcome.YES, Outcome.NO]),
              st.sampled_from([TimeInForce.IMMEDIATE_OR_CANCEL, TimeInForce.FILL_OR_KILL])),
    min_size=1, max_size=12)


@FAST
@given(seq=orders, result=st.sampled_from([SettlementResult.YES, SettlementResult.NO]),
       depth_fraction=st.sampled_from([D(1), D("0.5")]))
def test_cash_identity_start_minus_cost_minus_fees_plus_payouts(
        seq: list[tuple[int, int, Outcome, TimeInForce]], result: SettlementResult,
        depth_fraction: Decimal) -> None:
    bk = OrderBook(TICKER, (PriceLevel(D("0.70"), D(100)), PriceLevel(D("0.75"), D(100))),
                   (PriceLevel(D("0.15"), D(100)), PriceLevel(D("0.20"), D(100))), T0)
    venue = PaperVenue(mode=Mode.PAPER, clock=ReplayClock(T0), fees=FEES,
                       book_source=lambda t: bk, market_source=lambda t: market(),
                       starting_cash=D("100000"), depth_fraction=depth_fraction)
    start = venue.cash
    for i, (qty, price, outcome, tif) in enumerate(seq):
        venue.submit(OrderIntent(f"c{i}", f"d{i}", Mode.PAPER, StrategyId.POSITIVE_EV_80, TICKER,
                                 "E", outcome, D(qty), D(price) / 100, tif, D("100000"), "h", "v",
                                 T0))
    fills = venue.get_fills()
    cost = sum((f.quantity * f.price for f in fills), D(0))
    fees = sum((f.fee for f in fills), D(0))
    assert venue.cash == start - cost - fees  # nothing resting: no reserves
    won_qty = sum((f.quantity for f in fills if f.outcome.value == result.value), D(0))
    paid = venue.settle(TICKER, result)
    assert paid == won_qty
    assert venue.cash == start - cost - fees + won_qty
    assert venue.realized_pnl == won_qty - cost - fees
    assert venue.get_account().positions == ()
    for o in venue.list_open_orders():  # IOC/FOK never rest
        raise AssertionError(o)
    assert all(venue.get_order(f"c{i}").state is not OrderState.INTENT  # type: ignore[union-attr]
               for i in range(len(seq)))
