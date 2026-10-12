"""HardRiskManager: eligibility, strategy gating, caps shrink-only, fail-closed behaviour."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from kalshi_edge.domain import (
    AccountSnapshot,
    CalibrationStatus,
    MarketStatus,
    Mode,
    OrderState,
    OrderStatus,
    Outcome,
    Position,
    SettlementResult,
    StrategyId,
)
from kalshi_edge.risk import (
    Exposure,
    ExposureItem,
    HardRiskManager,
    HealthStatus,
    RiskState,
    build_exposure,
)
from tests.support import (
    D,
    FEES,
    T0,
    TICKER,
    book,
    estimate,
    limits,
    market,
    proposal,
    risk_ctx,
)

def bk_at(ask: str, qty: str = "1000"):  # noqa: ANN201
    """YES ask at ``ask`` with a YES bid 2c below (finite spread, uncrossed)."""
    return book(yes_asks=[(ask, qty)], yes_bids=[(str(D(ask) - D("0.02")), "10")])


RM = HardRiskManager(limits(), FEES, slippage_buffer=D("0.005"))


def check(prop=None, **kw):  # noqa: ANN001, ANN003, ANN201
    return RM.check(prop or proposal(), risk_ctx(**kw))


def test_baseline_approves_and_caps_at_one_percent_of_equity() -> None:
    d = check(proposal(qty="500"))
    assert d.approved, d.reasons
    # cap = 1% of 1009 = 10.09 ; 11*0.87+fee(0.09)=9.66 fits, 12*0.87+0.10=10.54 doesn't
    assert d.quantity == 11
    assert d.max_loss <= D("10.09")
    assert d.quantity <= D("500")


def test_cap_applies_to_risk_dollars_not_contract_count() -> None:
    sure = estimate(q="0.9995", q_safe="0.999")
    cheap = check(proposal(qty="500", limit="0.81", expected="0.81", est=sure), bk=bk_at("0.81"))
    dear = check(proposal(qty="500", limit="0.97", expected="0.97", est=sure), bk=bk_at("0.97"))
    assert cheap.approved and dear.approved
    # more contracts when cheap, but dollars at risk never exceed the same cap
    assert cheap.quantity > dear.quantity
    assert cheap.max_loss <= D("10.09") and dear.max_loss <= D("10.09")
    # near $1: a 0.97 contract risks 0.97 (+fee), not 0.03
    assert dear.max_loss >= dear.quantity * D("0.97")


def test_quantity_never_increases() -> None:
    d = check(proposal(qty="3"))
    assert d.approved and d.quantity == 3


def test_fractional_quantity_floors_to_step() -> None:
    mkt = market(quantity_step=D("0.01"))
    d = check(proposal(qty="500.009", mkt=mkt))
    assert d.approved
    assert d.quantity % D("0.01") == 0 and d.quantity < D("500")


def test_each_cap_binds_independently() -> None:
    """per-entry, aggregate, cluster and cash caps each shrink quantity to their room."""
    # aggregate: 10% of 1009 = 100.9 ; existing exposure 98.9 -> room 2.0 (other cap 10.09)
    exp = Exposure([ExposureItem("OTHER", ("event:Z", "catalyst:x:y"), D("98.9"), "position")])
    d = check(exposure=exp)
    assert d.approved and d.max_loss <= D("2.0") and d.quantity == 2
    assert D(d.metrics["room_aggregate"]) == D("2.0")
    # cluster: 5% = 50.45 ; same event already has 49.45 -> room 1.0
    exp = Exposure([ExposureItem("OTHER", (f"event:{market().event_ticker}",), D("49.45"),
                                 "position")])
    d = check(exposure=exp)
    assert d.approved and d.max_loss <= D("1.0") and d.quantity == 1
    # cash: 1.00 fee reserve + pending -> only 3.0 of room
    d = check(account=AccountSnapshot(T0, D("4.0"), (), ()))
    assert d.approved and d.max_loss <= D("3.0") and d.quantity == 3
    d = check(pending_cash="1006.0")  # cash 1009 - 1006 - 1 reserve = 2
    assert d.approved and d.max_loss <= D("2.0")


def test_zero_room_is_rejected_with_binding_cap() -> None:
    exp = Exposure([ExposureItem("OTHER", ("event:Z",), D("100.9"), "position")])
    d = check(exposure=exp)
    assert not d.approved and d.quantity == 0
    assert any(r.startswith("size_zero_after_caps:aggregate") for r in d.reasons)
    d = check(account=AccountSnapshot(T0, D("1.0"), (), ()))  # only the reserve left
    assert not d.approved
    assert any("size_zero_after_caps:cash" in r for r in d.reasons)


def test_negative_room_never_yields_positive_quantity() -> None:
    exp = Exposure([ExposureItem("OTHER", ("event:Z",), D("500"), "position")])  # over budget
    d = check(exposure=exp)
    assert not d.approved and d.quantity == 0 and d.max_loss == 0


def test_tighter_of_strategy_capital_and_account_equity() -> None:
    d = check(proposal(qty="500"), capital="1009", equity="100")  # 1% of 100 = $1
    assert d.approved and d.max_loss <= D("1.0")
    st = RiskState.fresh(D("200"), T0)  # strategy equity marked down to 200
    d = check(proposal(qty="500"), state=st, capital="1009", equity="1009")
    assert d.approved and d.max_loss <= D("2.0")


def test_gains_above_designated_capital_are_not_relevered() -> None:
    st = RiskState.fresh(D("5000"), T0)
    d = check(proposal(qty="500"), state=st, capital="1009", equity="5000")
    assert d.max_loss <= D("10.09")


def test_all_pending_resting_and_positions_count_in_exposure() -> None:
    mk = {TICKER: market()}
    pos = Position(TICKER, market().event_ticker, "weather", Outcome.YES, D(10), D("8.7"))
    order = OrderStatus("c", "x", TICKER, Outcome.NO, OrderState.RESTING, D(10), D(0), D(10),
                        D("0.5"), T0)
    filled = replace(order, client_order_id="d", state=OrderState.FILLED, remaining_quantity=D(0))
    acct = AccountSnapshot(T0, D(900), (pos,), (order, filled))
    pending = [ExposureItem(TICKER, ("event:E",), D("3"), "pending")]
    exp = build_exposure(acct, mk.get, pending)
    kinds = sorted(i.kind for i in exp.items)
    assert kinds == ["order", "pending", "position"]  # terminal order is ignored
    # order worst case = remaining * (limit + 0.02 fee allowance)
    assert exp.total == D("8.7") + D(10) * D("0.52") + D("3")
    # exposure feeds the cap: the proposal must shrink vs an empty book of exposure
    big = check(proposal(qty="500"), exposure=exp, account=acct)
    assert big.approved is False or big.max_loss <= D("10.09")


def test_correlated_event_cluster_counts_both_legs() -> None:
    a = market(ticker="A-1", event_ticker="EV")
    b = market(ticker="A-2", event_ticker="EV")
    ps = [Position("A-1", "EV", "weather", Outcome.YES, D(1), D("40")),
          Position("A-2", "EV", "weather", Outcome.NO, D(1), D("9"))]  # hedge not netted
    exp = build_exposure(AccountSnapshot(T0, D(900), tuple(ps), ()), {"A-1": a, "A-2": b}.get)
    assert exp.cluster_total("event:EV") == D("49")
    # a new trade on that event can only use 50.45 - 49 = 1.45
    d = check(proposal(mkt=market(ticker=TICKER, event_ticker="EV")), exposure=exp)
    assert d.approved and d.max_loss <= D("1.45")


def test_catalyst_cluster_same_category_and_date_across_events() -> None:
    other = market(ticker="B-1", event_ticker="OTHER-EV")  # same category, same close date
    pos = Position("B-1", "OTHER-EV", "weather", Outcome.YES, D(1), D("48"))
    exp = build_exposure(AccountSnapshot(T0, D(900), (pos,), ()), {"B-1": other}.get)
    d = check(exposure=exp)  # TICKER shares catalyst:weather:<date> with B-1
    assert d.approved and d.max_loss <= D("2.45") and d.quantity == 2
    assert D(d.metrics["room_cluster"]) == D("2.45")
    # different date => not correlated
    later = market(ticker="B-1", event_ticker="OTHER-EV", close_time=T0 + timedelta(days=3))
    exp2 = build_exposure(AccountSnapshot(T0, D(900), (pos,), ()), {"B-1": later}.get)
    d2 = check(proposal(qty="500"), exposure=exp2)
    assert d2.quantity == 11


def test_unknown_market_in_exposure_is_isolated_but_counted_globally() -> None:
    pos = Position("GHOST", "E?", "?", Outcome.YES, D(1), D("95"))
    exp = build_exposure(AccountSnapshot(T0, D(900), (pos,), ()), lambda t: None)
    assert exp.total == D("95")
    d = check(exposure=exp)  # aggregate room 100.9-95 = 5.9
    assert d.max_loss <= D("5.9")


def test_worst_simultaneous_loss() -> None:
    exp = Exposure([ExposureItem("a", ("event:1",), D(5), "position"),
                    ExposureItem("b", ("event:2",), D(7), "position"),
                    ExposureItem("c", ("event:2",), D(1), "position")])
    assert exp.worst_simultaneous_loss(1) == 8
    assert exp.worst_simultaneous_loss(2) == 13


# ------------------------------------------------------- kill / stops / health gating
@pytest.mark.parametrize("flag", ["drawdown_kill", "weekly_halt", "daily_halt"])
def test_latched_stops_block(flag: str) -> None:
    st = RiskState.fresh(D("1009"), T0)
    setattr(st, flag, True)
    d = check(state=st)
    assert not d.approved and d.quantity == 0


def test_kill_switch_blocks_even_perfect_proposal_all_strategies() -> None:
    for sid in StrategyId:
        d = check(proposal(strategy=sid), kill=True, mode=Mode.BACKTEST)
        assert not d.approved and "kill_switch_engaged" in d.reasons


def test_submission_not_permitted_blocks() -> None:
    d = check(permitted=False)
    assert "mode_does_not_permit_submission" in d.reasons and not d.approved


@pytest.mark.parametrize(("health", "reason"), [
    (HealthStatus(False, True, 0), "not_reconciled"),
    (HealthStatus(True, False, 0), "feed_unhealthy"),
    (HealthStatus(True, True, 1), "unresolved_prior_orders"),
    (HealthStatus(True, True, 0, clock_skew_seconds=2.5), "clock_skew"),
    (HealthStatus(True, True, 0, clock_skew_seconds=-2.5), "clock_skew"),
])
def test_health_fail_closed(health: HealthStatus, reason: str) -> None:
    d = check(health=health)
    assert reason in d.reasons and not d.approved


def test_daily_order_count_limit() -> None:
    st = RiskState.fresh(D("1009"), T0)
    st.orders_today = 10
    assert "daily_order_count_limit" in check(state=st).reasons
    st.orders_today = 9
    assert check(state=st).approved


# ---------------------------------------------------------------- market / freshness
@pytest.mark.parametrize("status", [MarketStatus.PAUSED, MarketStatus.CLOSED,
                                    MarketStatus.SETTLED, MarketStatus.UNKNOWN,
                                    MarketStatus.UNOPENED])
def test_non_open_market_rejected(status: MarketStatus) -> None:
    d = check(proposal(mkt=market(status=status)))
    assert f"market_status_{status.value}" in d.reasons


def test_resolved_market_and_unverified_rules_and_nonbinary_rejected() -> None:
    d = check(proposal(mkt=market(result=SettlementResult.YES)))
    assert "market_already_resolved" in d.reasons
    assert "market_rules_not_verified" in check(proposal(mkt=market(rules_verified=False))).reasons
    assert "not_binary" in check(proposal(mkt=market(market_type="scalar"))).reasons


def test_category_allowlist_empty_means_nothing_eligible() -> None:
    rm = HardRiskManager(limits(category_allowlist=()), FEES)
    assert "category_not_allowlisted" in rm.check(proposal(), risk_ctx()).reasons
    rm2 = HardRiskManager(limits(category_allowlist=("sports",)), FEES)
    assert "category_not_allowlisted" in rm2.check(proposal(), risk_ctx()).reasons


def test_too_close_to_close_boundary() -> None:
    near = market(close_time=T0 + timedelta(hours=2) - timedelta(seconds=1))
    assert "too_close_to_close" in check(proposal(mkt=near)).reasons
    ok = market(close_time=T0 + timedelta(hours=2))
    assert "too_close_to_close" not in check(proposal(mkt=ok)).reasons


def test_stale_and_future_books() -> None:
    old = book(ts=T0 - timedelta(seconds=3, milliseconds=1))
    assert "stale_or_future_book" in check(bk=old).reasons
    edge = book(ts=T0 - timedelta(seconds=3))
    assert "stale_or_future_book" not in check(bk=edge).reasons
    fut = book(ts=T0 + timedelta(seconds=3))
    assert "stale_or_future_book" in check(bk=fut).reasons


def test_book_for_wrong_market_rejected() -> None:
    assert "book_market_mismatch" in check(bk=book(ticker="OTHER")).reasons


def test_empty_crossed_thin_and_wide_books() -> None:
    assert "no_ask_liquidity" in check(bk=book(yes_asks=(), yes_bids=())).reasons
    crossed = book(yes_asks=[("0.87", "1000")], yes_bids=[("0.90", "10")])
    assert "crossed_or_locked_book" in check(bk=crossed).reasons
    thin = book(yes_asks=[("0.87", "10")])
    assert "insufficient_depth" in check(bk=thin).reasons  # 8.7 < 25 notional
    wide = book(yes_asks=[("0.87", "1000")], yes_bids=[("0.50", "10")])
    assert "spread_unknown_or_too_wide" in check(bk=wide).reasons
    nobid = book(yes_asks=[("0.87", "1000")], yes_bids=())
    assert "spread_unknown_or_too_wide" in check(bk=nobid).reasons


def test_stale_forecast_rejected_at_boundary() -> None:
    at = T0 - timedelta(hours=6)
    assert "stale_forecast" not in check(proposal(est=estimate(as_of=at))).reasons
    assert "stale_forecast" in check(
        proposal(est=estimate(as_of=at - timedelta(seconds=1)))).reasons
    assert "stale_forecast" in check(
        proposal(est=estimate(as_of=T0 + timedelta(seconds=10)))).reasons


# ------------------------------------------------------------- price band and grid
def test_price_at_or_below_floor_rejected_for_every_strategy() -> None:
    bk = bk_at("0.80")
    for sid in StrategyId:
        for mode in (Mode.PAPER, Mode.DEMO, Mode.LIVE):
            d = check(proposal(strategy=sid, limit="0.80", expected="0.80"), bk=bk, mode=mode)
            assert "price_not_above_threshold" in d.reasons and not d.approved, (sid, mode)
    assert check(proposal(limit="0.81"), bk=bk_at("0.81")).approved


def test_price_above_max_rejected() -> None:
    bk = bk_at("0.98")
    assert "price_above_max" in check(proposal(limit="0.98", expected="0.98"), bk=bk).reasons


@pytest.mark.parametrize("limit", ["0.8750", "0.871", "1.00", "0", "-0.5"])
def test_off_grid_or_degenerate_limit_rejected(limit: str) -> None:
    d = check(proposal(limit=limit, expected="0.87"))
    assert not d.approved


def test_off_grid_flagged_specifically() -> None:
    d = check(proposal(limit="0.871"))
    assert "limit_price_off_grid_or_unknown_grid" in d.reasons


def test_missing_price_ranges_fails_closed() -> None:
    d = check(proposal(mkt=market(price_ranges=())))
    assert "limit_price_off_grid_or_unknown_grid" in d.reasons and not d.approved


def test_ablation_floor_waived_only_in_backtest() -> None:
    bk = bk_at("0.50")
    p = proposal(strategy=StrategyId.ABLATION, limit="0.50", expected="0.50",
                 est=estimate(q="0.9", q_safe="0.85"))
    assert "price_not_above_threshold" not in check(p, bk=bk, mode=Mode.BACKTEST).reasons
    assert "price_not_above_threshold" in check(p, bk=bk, mode=Mode.PAPER).reasons


# ------------------------------------------------- estimate / edge / strategy gating
def test_missing_estimate_means_no_trade_for_c_and_d() -> None:
    for sid in (StrategyId.POSITIVE_EV_80, StrategyId.ABLATION):
        d = check(proposal(strategy=sid, est=None))
        assert "missing_probability_estimate" in d.reasons and not d.approved


@pytest.mark.parametrize(("q", "qs", "label"), [
    ("0.99", "0.878", "equal"),   # q_safe == p + f exactly (0.87 + 0.008)
    ("0.99", "0.87", "below"),
])
def test_q_safe_at_or_below_breakeven_no_trade(q: str, qs: str, label: str) -> None:
    d = check(proposal(qty="10", est=estimate(q=q, q_safe=qs)))
    assert not d.approved, label
    assert "q_safe_not_above_breakeven" in d.reasons


def test_q_safe_above_breakeven_but_edge_below_minimum() -> None:
    # breakeven 0.878 ; edge = qs - 0.878 - 0.005 ; qs=0.899 -> 0.016 < 0.02
    d = check(proposal(qty="10", est=estimate(q="0.95", q_safe="0.899")))
    assert "edge_below_minimum" in d.reasons and not d.approved
    ok = check(proposal(qty="10", est=estimate(q="0.95", q_safe="0.903")))
    assert ok.approved, ok.reasons


def test_edge_exactly_at_min_edge_is_accepted() -> None:
    # qty 10 @0.87: fee = ceil(.07*10*.87*.13)=0.08 -> breakeven .878 ; edge = qs-.878-.005
    d = check(proposal(qty="10", est=estimate(q="0.95", q_safe="0.903")))
    assert d.approved and D(d.metrics["edge"]) == D("0.02")


def test_estimate_mismatch_and_not_usable_rejected() -> None:
    wrong_side = estimate(outcome=Outcome.NO)
    assert "estimate_mismatch" in check(proposal(est=wrong_side)).reasons
    wrong_tk = estimate(ticker="OTHER")
    assert "estimate_mismatch" in check(proposal(est=wrong_tk)).reasons
    assert "estimate_not_independent_or_not_calibrated" in check(
        proposal(est=estimate(independent=False))).reasons
    for st in (CalibrationStatus.NONE, CalibrationStatus.INSUFFICIENT,
               CalibrationStatus.REJECTED):
        assert not check(proposal(est=estimate(status=st))).approved


def test_edge_uses_fresh_book_not_proposal_numbers() -> None:
    """Book moved against us between discovery and submit: re-check must reject."""
    p = proposal(qty="10", limit="0.90", expected="0.87", est=estimate(q="0.95", q_safe="0.92"))
    worse = bk_at("0.89")
    d = check(p, bk=worse)
    assert not d.approved and "edge_below_minimum" in d.reasons


def test_thin_fresh_depth_for_quantity_rejected() -> None:
    p = proposal(qty="30")
    assert check(p, bk=bk_at("0.87", "30")).approved
    # 29 contracts shown: notional 25.2 >= 25 passes the depth screen; 30 cannot be filled
    d = check(p, bk=bk_at("0.87", "29"))
    assert d.approved is False or d.quantity <= 29


def test_vwap_above_limit_is_rejected() -> None:
    # limit .87 but walking the book to fill 40 contracts crosses into .90
    bk = book(yes_asks=[("0.87", "20"), ("0.90", "1000")], yes_bids=[("0.85", "10")])
    d = check(proposal(qty="40", limit="0.87", expected="0.87"), bk=bk)
    assert not d.approved or d.quantity <= 20


# ---------------------------------------------- A/B never live; C needs calibration
@pytest.mark.parametrize("sid", [StrategyId.NAIVE_80, StrategyId.SCALED_80, StrategyId.ABLATION])
@pytest.mark.parametrize("mode", [Mode.DEMO, Mode.LIVE])
def test_non_c_strategies_rejected_in_demo_and_live(sid: StrategyId, mode: Mode) -> None:
    d = check(proposal(strategy=sid, est=estimate()), mode=mode)
    assert "strategy_or_estimate_not_live_eligible" in d.reasons and not d.approved


@pytest.mark.parametrize("sid", [StrategyId.NAIVE_80, StrategyId.SCALED_80])
def test_a_and_b_proposals_are_never_live_eligible_even_with_perfect_estimate(
        sid: StrategyId) -> None:
    assert proposal(strategy=sid, est=estimate()).live_eligible is False
    assert proposal(strategy=StrategyId.POSITIVE_EV_80).live_eligible is True


@pytest.mark.parametrize(("independent", "status", "eligible"), [
    (True, CalibrationStatus.ACCEPTED, True),
    (False, CalibrationStatus.ACCEPTED, False),
    (True, CalibrationStatus.INSUFFICIENT, False),
    (True, CalibrationStatus.REJECTED, False),
    (True, CalibrationStatus.NONE, False),
])
def test_c_live_eligibility_needs_independent_and_accepted(
        independent: bool, status: CalibrationStatus, eligible: bool) -> None:
    p = proposal(est=estimate(independent=independent, status=status))
    assert p.live_eligible is eligible
    d = check(p, mode=Mode.DEMO)
    assert d.approved is eligible
    assert proposal(est=None).live_eligible is False


def test_c_in_live_is_additionally_limited_by_canary_total_risk() -> None:
    d = check(proposal(qty="500"), mode=Mode.LIVE)
    assert d.approved and d.max_loss <= D("5")
    exp = Exposure([ExposureItem("o", ("event:Z",), D("4.5"), "position")])
    d2 = check(proposal(qty="500"), mode=Mode.LIVE, exposure=exp)
    assert d2.max_loss <= D("0.5")


def test_live_without_arming_gate_is_rejected() -> None:
    d = check(mode=Mode.LIVE, permitted=False)
    assert not d.approved


# -------------------------------------------------------- malformed / malicious input
def test_negative_zero_quantities_rejected() -> None:
    for q in ("0", "-5"):
        d = check(proposal(qty=q))
        assert not d.approved and d.quantity == 0
        assert "non_positive_quantity" in d.reasons


def test_absurd_quantity_is_capped_not_trusted() -> None:
    d = check(proposal(qty="1000000000"))
    assert d.approved and d.quantity <= 11


def test_proposal_for_wrong_ticker_book_rejected() -> None:
    d = check(proposal(mkt=market(ticker="OTHER-1")), bk=book(ticker=TICKER))
    assert "book_market_mismatch" in d.reasons and not d.approved


def test_every_rejection_has_a_reason_and_zero_size() -> None:
    d = check(proposal(est=None), kill=True)
    assert not d.approved and d.reasons and d.quantity == 0 and d.max_loss == 0


@pytest.mark.xfail(strict=True, reason="BUG: risk check only tests proposal.expected_price / "
                   "limit_price against the >0.80 screen, not the fresh book's executable ask; "
                   "a proposal (limit .85) is approved when the fresh best ask is 0.70 "
                   "(risk/manager.py section 5)")
def test_executable_price_on_fresh_book_must_still_exceed_floor() -> None:
    cheap = bk_at("0.70")
    p = proposal(limit="0.85", expected="0.85", est=estimate(q="0.99", q_safe="0.98"))
    d = check(p, bk=cheap)
    assert not d.approved


def test_equity_base_helper() -> None:
    ctx = risk_ctx(capital="1009", equity="500")
    assert RM.equity_base(ctx) == 500
    ctx2 = risk_ctx(capital="1009", equity="2000")
    assert RM.equity_base(ctx2) == 1009
    ctx3 = risk_ctx(capital="1009", equity="-5")
    assert RM.equity_base(ctx3) == 0


def test_max_loss_for_includes_fees_and_uses_limit_for_unfilled_depth() -> None:
    p = proposal()
    bk = book(yes_asks=[("0.85", "5")], yes_bids=())
    ml = RM.max_loss_for(p, bk, D(10))
    fee = FEES.fee(D(5), D("0.85")) + FEES.fee(D(5), D("0.87"))
    assert ml == D(10) * D("0.87") + fee


def test_replace_helper_unused_import_guard() -> None:
    assert replace(proposal(), quantity=D(1)).quantity == 1
