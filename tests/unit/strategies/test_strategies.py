import dataclasses
from decimal import Decimal

from strat_helpers import estimate, make_book, make_ctx, make_market

from kalshi_edge.config import RiskLimits, StrategyConfig
from kalshi_edge.domain import (
    CalibrationStatus,
    MarketStatus,
    Outcome,
    StrategyId,
    TradeProposal,
)
from kalshi_edge.fees import FeeSchedule
from kalshi_edge.strategies import (
    NaiveThreshold,
    PositiveEV,
    PositiveEVAblation,
    ProbabilityScaled,
    find_candidates,
    make_strategy,
)

D = Decimal
RISK = RiskLimits()


def cfg(**kw: object) -> StrategyConfig:
    return StrategyConfig(**kw)  # type: ignore[arg-type]


def test_a_b_reject_at_or_below_80_accept_above() -> None:
    for strat in (NaiveThreshold(cfg(), RISK), ProbabilityScaled(cfg(), RISK)):
        assert strat.evaluate(make_ctx(make_book(yes_asks=[("0.80", "100")]))) == []
        props = strat.evaluate(make_ctx(make_book(yes_asks=[("0.81", "100")])))
        assert len(props) == 1
        p = props[0]
        assert p.outcome is Outcome.YES and p.estimate is None and p.edge_per_contract is None
        assert p.limit_price == D("0.81") and p.quantity == p.quantity.to_integral_value()


def test_a_respects_notional_and_max_price() -> None:
    strat = NaiveThreshold(cfg(naive_notional=D("10")), RISK)
    (p,) = strat.evaluate(make_ctx(make_book(yes_asks=[("0.90", "100")])))
    assert p.quantity * p.expected_price + p.quantity * p.fee_per_contract <= D("10")
    assert strat.evaluate(make_ctx(make_book(yes_asks=[("0.98", "100")]))) == []


def test_b_scales_with_price_and_never_exceeds_max_risk() -> None:
    strat = ProbabilityScaled(cfg(), RISK)
    (lo,) = strat.evaluate(make_ctx(make_book(yes_asks=[("0.82", "1000")])))
    (hi,) = strat.evaluate(make_ctx(make_book(yes_asks=[("0.96", "1000")])))
    cost = lambda x: x.quantity * (x.expected_price + x.fee_per_contract)  # noqa: E731
    assert cost(lo) <= D("2.0") + D("1.6") and cost(hi) <= D(10)
    assert cost(hi) > cost(lo)


def test_not_open_or_nonbinary_skipped() -> None:
    book = make_book(yes_asks=[("0.90", "100")])
    for kw in ({"status": MarketStatus.CLOSED}, {"market_type": "scalar"}):
        assert NaiveThreshold(cfg(), RISK).evaluate(make_ctx(book, market=make_market(**kw))) == []


def test_no_side_selected_from_yes_bids() -> None:
    # YES bid 0.10 => NO ask 0.90 (the NO outcome is the >0.80 candidate)
    book = make_book(no_asks=[("0.90", "100")])
    assert book.best_ask(Outcome.NO).price == D("0.90")  # type: ignore[union-attr]
    ctx = make_ctx(book, {Outcome.NO: estimate(Outcome.NO)})
    cands = find_candidates(ctx, min_price_exclusive=D("0.80"), max_price=D("0.97"),
                            probe_quantity=D(1))
    assert [c.outcome for c in cands] == [Outcome.NO]
    (p,) = PositiveEV(cfg(), RISK).evaluate(ctx)
    assert p.outcome is Outcome.NO and p.limit_price == D("0.90")


def test_c_trades_with_good_estimate_and_sizes_by_kelly() -> None:
    ctx = make_ctx(make_book(yes_asks=[("0.88", "1000")]), {Outcome.YES: estimate()})
    (p,) = PositiveEV(cfg(), RISK).evaluate(ctx)
    assert p.strategy_id is StrategyId.POSITIVE_EV_80
    assert p.edge_per_contract is not None and p.edge_per_contract >= RISK.min_edge
    assert p.ev_per_contract == D("0.97") - p.expected_price - p.fee_per_contract
    assert p.live_eligible
    c = p.expected_price + p.fee_per_contract + D("0.005")
    full = (D("0.95") - c) / (1 - c)
    assert p.quantity * (p.expected_price + p.fee_per_contract) <= D(1009) * D("0.1") * full
    assert "q_safe=0.95" in p.rationale


def test_c_rejections() -> None:
    book = make_book(yes_asks=[("0.88", "1000")])
    bad = [
        None,
        estimate(independent=False),
        estimate(status=CalibrationStatus.INSUFFICIENT),
        estimate(status=CalibrationStatus.REJECTED),
        estimate(status=CalibrationStatus.NONE),
        estimate(q="0.90", q_safe="0.89"),  # below breakeven
        estimate(q="0.93", q_safe="0.90"),  # edge below min_edge
        estimate(Outcome.NO),  # wrong side estimate under YES key
    ]
    for est in bad:
        assert PositiveEV(cfg(), RISK).evaluate(make_ctx(book, {Outcome.YES: est})) == []
    assert PositiveEV(cfg(), RISK).evaluate(make_ctx(book, {})) == []


def test_c_q_safe_at_breakeven_no_trade_even_with_zero_min_edge() -> None:
    risk = RiskLimits(min_edge=D(0))
    config = cfg(slippage_buffer=D(0), sizing="fixed")
    book = make_book(yes_asks=[("0.90", "1000")])
    fine_fees = FeeSchedule(rounding_increment=D("0.000000001"))  # fee/contract = 0.0063 exactly
    strat = PositiveEV(config, risk)

    def run(q_safe: str) -> list[TradeProposal]:
        ctx = make_ctx(book, {Outcome.YES: estimate(q="0.99", q_safe=q_safe)})
        return list(strat.evaluate(dataclasses.replace(ctx, fees=fine_fees)))

    assert run("0.9063") == []  # q_safe == p + f exactly: edge 0 >= min_edge 0 but not trade
    assert len(run("0.9064")) == 1
    assert run("0.9062") == []


def test_c_zero_sizing_and_fixed_sizing() -> None:
    ctx = make_ctx(make_book(yes_asks=[("0.88", "1000")]), {Outcome.YES: estimate()})
    assert PositiveEV(cfg(sizing="zero"), RISK).evaluate(ctx) == []
    (p,) = PositiveEV(cfg(sizing="fixed", fixed_risk=D("5")), RISK).evaluate(ctx)
    assert p.quantity * (p.expected_price + p.fee_per_contract) <= D(5)
    assert p.quantity >= 4


def test_c_floor_and_max_cap_respected() -> None:
    est = {Outcome.YES: estimate(q="0.99", q_safe="0.98")}
    book_lo = make_book(yes_asks=[("0.70", "1000")])
    assert PositiveEV(cfg(), RISK).evaluate(make_ctx(book_lo, est)) == []
    (d,) = PositiveEVAblation(cfg(), RISK).evaluate(make_ctx(book_lo, est))
    assert d.strategy_id is StrategyId.ABLATION and d.expected_price == D("0.70")
    deep = make_book(yes_asks=[("0.90", "5"), ("0.99", "1000")])
    (p,) = PositiveEV(cfg(sizing="fixed", fixed_risk=D(100)), RISK).evaluate(
        make_ctx(deep, est))
    assert p.quantity == 5 and p.limit_price == D("0.90")  # 0.99 level excluded by max_price


def test_c_recheck_with_vwap_drops_when_depth_erodes_edge() -> None:
    # best ask fine, but 2nd level makes VWAP too expensive for the edge
    est = {Outcome.YES: estimate(q="0.95", q_safe="0.94")}
    book = make_book(yes_asks=[("0.88", "1"), ("0.96", "1000")])
    props = PositiveEV(cfg(sizing="fixed", fixed_risk=D(50)), RISK).evaluate(make_ctx(book, est))
    for p in props:
        assert p.edge_per_contract is not None and p.edge_per_contract >= RISK.min_edge


def test_off_grid_and_no_ranges_dropped() -> None:
    book = make_book(yes_asks=[("0.905", "100")])
    assert NaiveThreshold(cfg(), RISK).evaluate(make_ctx(book)) == []
    book2 = make_book(yes_asks=[("0.90", "100")])
    m = make_market(price_ranges=())
    assert NaiveThreshold(cfg(), RISK).evaluate(make_ctx(book2, market=m)) == []


def test_factory() -> None:
    for sid, cls in ((StrategyId.NAIVE_80, NaiveThreshold), (StrategyId.SCALED_80,
                     ProbabilityScaled), (StrategyId.POSITIVE_EV_80, PositiveEV),
                     (StrategyId.ABLATION, PositiveEVAblation)):
        s = make_strategy(cfg(id=sid), RISK)
        assert isinstance(s, cls) and s.strategy_id is sid
