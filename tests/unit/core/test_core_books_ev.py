"""EV identity, payoff asymmetry, YES/NO purchase, bid->ask conversion, books, VWAP."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from tests.support import FEES, T0, TICKER, book, market

from kalshi_edge.domain import (
    D,
    ExecutableQuote,
    Mode,
    OrderBook,
    OrderIntent,
    OrderState,
    Outcome,
    PriceLevel,
    SettlementResult,
    StrategyId,
    TimeInForce,
)
from kalshi_edge.execution import PaperVenue
from kalshi_edge.pricing import (
    ask_depth_notional,
    executable_quote,
    spread,
    walk_asks,
)
from kalshi_edge.runtime.clock import ReplayClock

ONE = Decimal(1)


def lv(p: str, q: str) -> PriceLevel:
    return PriceLevel(D(p), D(q))


def mkbook(yes: list[tuple[str, str]], no: list[tuple[str, str]]) -> OrderBook:
    return OrderBook(TICKER, tuple(lv(*x) for x in yes), tuple(lv(*x) for x in no), T0)


# ------------------------------------------------------------ bid -> ask conversion
def test_yes_ask_is_one_minus_best_no_bid_and_vice_versa() -> None:
    b = mkbook(yes=[("0.80", "5"), ("0.84", "7")], no=[("0.10", "3"), ("0.13", "9")])
    # YES asks come from NO bids: 1-0.13=0.87 (best), 1-0.10=0.90
    assert [(a.price, a.quantity) for a in b.asks(Outcome.YES)] == [
        (D("0.87"), D("9")), (D("0.90"), D("3"))]
    # NO asks come from YES bids: 1-0.84=0.16 (best), 1-0.80=0.20
    assert [(a.price, a.quantity) for a in b.asks(Outcome.NO)] == [
        (D("0.16"), D("7")), (D("0.20"), D("5"))]
    assert b.best_ask(Outcome.YES) == lv("0.87", "9")
    assert b.best_bid(Outcome.YES) == lv("0.84", "7")


def test_asks_are_sorted_cheapest_first_for_any_ordering() -> None:
    b = mkbook(yes=[], no=[("0.05", "1"), ("0.10", "1"), ("0.30", "1")])  # ascending, best bid last
    prices = [a.price for a in b.asks(Outcome.YES)]
    assert prices == sorted(prices)
    assert prices[0] == D("0.70")


def test_empty_book_has_no_asks_and_no_quote() -> None:
    b = mkbook([], [])
    assert b.asks(Outcome.YES) == () and b.asks(Outcome.NO) == ()
    assert b.best_ask(Outcome.YES) is None
    assert not b.is_crossed_or_locked
    assert executable_quote(b, Outcome.YES, D(1), FEES) is None
    assert spread(b, Outcome.YES) is None
    assert ask_depth_notional(b, Outcome.YES) == 0


def test_one_sided_book() -> None:
    b = mkbook(yes=[("0.84", "7")], no=[])
    assert b.best_ask(Outcome.YES) is None  # nothing to buy YES from
    assert b.best_ask(Outcome.NO) is not None
    assert spread(b, Outcome.YES) is None


@pytest.mark.parametrize(("y", "n", "crossed"), [
    ("0.60", "0.40", True),   # locked: yes bid + no bid == 1
    ("0.70", "0.40", True),   # crossed
    ("0.60", "0.39", False),  # 1 cent of spread
])
def test_crossed_locked_detection_and_quote_refused(y: str, n: str, crossed: bool) -> None:
    b = mkbook(yes=[(y, "10")], no=[(n, "10")])
    assert b.is_crossed_or_locked is crossed
    q = executable_quote(b, Outcome.YES, D(1), FEES)
    assert (q is None) is crossed


# ------------------------------------------------------------------ depth and VWAP
def test_vwap_over_varying_depth_exact() -> None:
    b = book(yes_asks=[("0.85", "10"), ("0.86", "20"), ("0.90", "100")], yes_bids=())
    q = executable_quote(b, Outcome.YES, D("40"), FEES)
    assert q is not None
    notional = D("0.85") * 10 + D("0.86") * 20 + D("0.90") * 10
    assert q.vwap == notional / 40
    assert q.fillable_quantity == 40 and q.requested_quantity == 40
    assert q.best_price == D("0.85") and q.worst_price == D("0.90")
    # fee is the sum of per-level rounded fees
    expect_fee = sum((FEES.fee(D(n), D(p)) for p, n in
                      [("0.85", "10"), ("0.86", "20"), ("0.90", "10")]), D(0))
    assert q.fee_total == expect_fee


def test_partial_depth_reports_fillable_less_than_requested() -> None:
    b = book(yes_asks=[("0.85", "3"), ("0.86", "2")], yes_bids=())
    q = executable_quote(b, Outcome.YES, D("10"), FEES)
    assert q is not None
    assert q.fillable_quantity == 5 and q.requested_quantity == 10
    assert q.vwap == (D("0.85") * 3 + D("0.86") * 2) / 5


def test_walk_never_exceeds_level_or_request() -> None:
    b = book(yes_asks=[("0.85", "3"), ("0.86", "2")], yes_bids=())
    fills = walk_asks(b, Outcome.YES, D("4"))
    assert [(f.price, f.quantity) for f in fills] == [(D("0.85"), D("3")), (D("0.86"), D("1"))]


def test_fractional_quantity_walk() -> None:
    b = book(yes_asks=[("0.85", "1.5")], yes_bids=())
    q = executable_quote(b, Outcome.YES, D("1.25"), FEES)
    assert q is not None and q.fillable_quantity == D("1.25") and q.vwap == D("0.85")


def test_quote_rejects_non_positive_quantity() -> None:
    with pytest.raises(ValueError):
        executable_quote(book(), Outcome.YES, D(0), FEES)


def test_depth_notional_respects_price_cap() -> None:
    b = book(yes_asks=[("0.85", "10"), ("0.95", "10")], yes_bids=())
    assert ask_depth_notional(b, Outcome.YES, D("0.90")) == D("8.5")
    assert ask_depth_notional(b, Outcome.YES) == D("18")


def test_yes_vs_no_purchase_uses_opposite_side() -> None:
    # Only NO bids exist -> can buy YES, cannot buy NO.
    b = mkbook(yes=[], no=[("0.13", "100")])
    assert executable_quote(b, Outcome.YES, D(1), FEES) is not None
    assert executable_quote(b, Outcome.NO, D(1), FEES) is None
    # symmetric: only YES bids -> can buy NO
    b2 = mkbook(yes=[("0.13", "100")], no=[])
    assert executable_quote(b2, Outcome.NO, D(1), FEES) is not None
    assert executable_quote(b2, Outcome.YES, D(1), FEES) is None


def test_cash_outlay_units() -> None:
    q = ExecutableQuote(TICKER, Outcome.YES, D(10), D(10), D("0.87"), D("0.87"), D("0.87"),
                        D("0.08"), T0)
    assert q.cash_outlay == D("8.78")
    assert q.fee_per_contract == D("0.008")


# ------------------------------------------------- EV identity / payoff asymmetry
def _buy_and_settle(result: SettlementResult, outcome: Outcome, qty: str, ask: str):
    clock = ReplayClock(T0)
    mk = market()
    bk = book(yes_asks=[(ask, "1000")] if outcome is Outcome.YES else [],
              no_asks=[(ask, "1000")] if outcome is Outcome.NO else [], yes_bids=())
    venue = PaperVenue(mode=Mode.PAPER, clock=clock, fees=FEES, book_source=lambda t: bk,
                       market_source=lambda t: mk, starting_cash=D("1000"))
    intent = OrderIntent("c1", "d", Mode.PAPER, StrategyId.POSITIVE_EV_80, TICKER, mk.event_ticker,
                         outcome, D(qty), D(ask), TimeInForce.IMMEDIATE_OR_CANCEL, D("500"), "h",
                         "v", T0)
    st = venue.submit(intent)
    assert st.state is OrderState.FILLED
    fills = venue.get_fills()
    cost = sum((f.quantity * f.price + f.fee for f in fills), D(0))
    fee = sum((f.fee for f in fills), D(0))
    assert venue.cash == D("1000") - cost
    pay = venue.settle(TICKER, result)
    return cost, fee, pay, venue


@pytest.mark.parametrize("outcome", [Outcome.YES, Outcome.NO])
@pytest.mark.parametrize(("q", "ask"), [("0.95", "0.87"), ("0.90", "0.90"), ("0.82", "0.85")])
def test_ev_identity_q_minus_p_minus_f_via_settlement(outcome: Outcome, q: str, ask: str) -> None:
    """EV over the two outcomes of actually settling a purchase equals qty*(q - p - f)."""
    qty = D("20")
    won = SettlementResult.YES if outcome is Outcome.YES else SettlementResult.NO
    lost = SettlementResult.NO if outcome is Outcome.YES else SettlementResult.YES
    cost_w, fee, pay_w, _ = _buy_and_settle(won, outcome, "20", ask)
    cost_l, _, pay_l, _ = _buy_and_settle(lost, outcome, "20", ask)
    pnl_win, pnl_lose = pay_w - cost_w, pay_l - cost_l
    # payoff asymmetry: win = qty*(1-p) - fee ; lose = -(qty*p + fee)
    assert pnl_win == qty * (ONE - D(ask)) - fee
    assert pnl_lose == -(qty * D(ask) + fee)
    ev = D(q) * pnl_win + (ONE - D(q)) * pnl_lose
    assert ev == qty * (D(q) - D(ask)) - fee  # q - p - f per contract (scaled)
    # break-even: q == p + f/qty  => zero EV
    qbe = D(ask) + fee / qty
    assert qbe * pnl_win + (ONE - qbe) * pnl_lose == 0


def test_max_loss_of_long_binary_is_cash_plus_fees() -> None:
    cost, fee, pay, venue = _buy_and_settle(SettlementResult.NO, Outcome.YES, "20", "0.87")
    assert pay == 0
    assert venue.cash == D("1000") - cost
    assert cost == D("20") * D("0.87") + fee


def test_win_payout_is_one_dollar_per_contract() -> None:
    _, _, pay, _ = _buy_and_settle(SettlementResult.YES, Outcome.YES, "20", "0.87")
    assert pay == 20


def test_buying_no_wins_when_result_no() -> None:
    cost, _fee, pay, venue = _buy_and_settle(SettlementResult.NO, Outcome.NO, "10", "0.88")
    assert pay == 10 and venue.cash == D("1000") - cost + pay


def test_book_timestamp_is_preserved_in_quote() -> None:
    b = book(ts=T0 + timedelta(seconds=7))
    q = executable_quote(b, Outcome.YES, D(1), FEES)
    assert q is not None and q.book_ts == T0 + timedelta(seconds=7)
