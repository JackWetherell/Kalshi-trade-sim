"""Store idempotency, ExecutionEngine ambiguity handling, PaperVenue accounting, reconcile."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from tests.support import FEES, T0, TICKER, D, book, market

from kalshi_edge.domain import (
    Fill,
    Mode,
    OrderIntent,
    OrderState,
    Outcome,
    SettlementResult,
    StrategyId,
    TimeInForce,
)
from kalshi_edge.execution import (
    AmbiguousSubmission,
    DuplicateIntent,
    ExecutionEngine,
    OrderRejected,
    PaperVenue,
    Store,
    reconcile,
)
from kalshi_edge.runtime.clock import ReplayClock


def intent(cid: str = "c1", qty: str = "10", price: str = "0.87", *,
           outcome: Outcome = Outcome.YES, tif: TimeInForce = TimeInForce.IMMEDIATE_OR_CANCEL,
           max_cost: str = "100", mode: Mode = Mode.PAPER) -> OrderIntent:
    return OrderIntent(cid, "dec-" + cid, mode, StrategyId.POSITIVE_EV_80, TICKER,
                       market().event_ticker, outcome, D(qty), D(price), tif, D(max_cost), "h",
                       "v", T0)


class Env:
    def __init__(self, tmp_path: Path, cash: str = "1000", depth_fraction: str = "1",
                 bk=None) -> None:
        self.clock = ReplayClock(T0)
        self.bk = bk or book(yes_asks=[("0.87", "50"), ("0.88", "50")], yes_bids=[("0.85", "10")])
        self.mk = market()
        self.venue = PaperVenue(mode=Mode.PAPER, clock=self.clock, fees=FEES,
                                book_source=lambda t: self.bk, market_source=lambda t: self.mk,
                                starting_cash=D(cash), depth_fraction=D(depth_fraction))
        self.store = Store(tmp_path / "s.db")
        self.engine = ExecutionEngine(self.venue, self.store, self.clock)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


# ------------------------------------------------------------------------- store
def test_duplicate_intent_rejected_and_state_unchanged(env: Env) -> None:
    env.store.insert_intent(intent())
    with pytest.raises(DuplicateIntent):
        env.store.insert_intent(intent())
    assert len(env.store.orders()) == 1


def test_terminal_state_is_final_and_conflict_logged(env: Env) -> None:
    env.store.insert_intent(intent())
    env.store.update_order("c1", OrderState.FILLED, T0)
    env.store.update_order("c1", OrderState.RESTING, T0)  # late/out-of-order update ignored
    assert env.store.get_order("c1").state is OrderState.FILLED  # type: ignore[union-attr]
    kinds = [r["kind"] for r in env.store.recent_events()]
    assert "order_state_conflict" in kinds
    with pytest.raises(KeyError):
        env.store.update_order("nope", OrderState.FILLED, T0)


def test_fill_dedup_by_fill_id(env: Env) -> None:
    f = Fill("f1", "c1", "x", TICKER, Outcome.YES, D(5), D("0.87"), D("0.03"), True, T0)
    assert env.store.add_fill(f) is True
    assert env.store.add_fill(f) is False
    assert env.store.add_fill(replace(f, quantity=D(6))) is False  # same id, different body
    assert len(env.store.fills()) == 1 and env.store.fills()[0].quantity == 5


def test_store_persists_across_instances(tmp_path: Path) -> None:
    s1 = Store(tmp_path / "p.db")
    s1.insert_intent(intent())
    s1.put("k", {"a": 1})
    s1.close()
    s2 = Store(tmp_path / "p.db")
    assert s2.get_order("c1") is not None and s2.get("k") == {"a": 1}
    with pytest.raises(DuplicateIntent):
        s2.insert_intent(intent())


def test_decimal_precision_roundtrips_through_store(env: Env) -> None:
    env.store.insert_intent(intent(qty="1.37", price="0.8765"))
    o = env.store.get_order("c1")
    assert o is not None and o.quantity == D("1.37") and o.limit_price == D("0.8765")


# ---------------------------------------------------------------------- paper venue
def test_ioc_walks_book_and_charges_per_fill_fees(env: Env) -> None:
    st = env.venue.submit(intent(qty="60", price="0.88"))
    assert st.state is OrderState.FILLED and st.filled_quantity == 60
    fills = env.venue.get_fills()
    assert [(f.quantity, f.price) for f in fills] == [(D(50), D("0.87")), (D(10), D("0.88"))]
    cost = sum((f.quantity * f.price + f.fee for f in fills), D(0))
    assert env.venue.cash == D(1000) - cost
    assert fills[0].fee == FEES.fee(D(50), D("0.87"))


def test_limit_price_is_respected(env: Env) -> None:
    st = env.venue.submit(intent(qty="60", price="0.87"))
    assert st.state is OrderState.CANCELED and st.filled_quantity == 50  # IOC remainder dropped
    assert env.venue.cash < D(1000)


def test_fok_all_or_nothing(env: Env) -> None:
    st = env.venue.submit(intent(qty="60", price="0.87", tif=TimeInForce.FILL_OR_KILL))
    assert st.state is OrderState.CANCELED and st.filled_quantity == 0
    assert env.venue.cash == D(1000) and env.venue.get_fills() == []


def test_insufficient_buying_power_rejected_no_state_change(env: Env) -> None:
    st = env.venue.submit(intent(max_cost="2000"))
    assert st.state is OrderState.REJECTED
    assert env.venue.cash == D(1000) and not env.venue.get_fills()


def test_fee_overrun_beyond_max_cost_is_refused_atomically(tmp_path: Path) -> None:
    e = Env(tmp_path, cash="8.75")  # passes max_cost gate, but real cost is 8.70 + 0.08 fee
    st = e.venue.submit(intent(qty="10", max_cost="8.75"))
    assert st.state is OrderState.REJECTED
    assert e.venue.cash == D("8.75") and e.venue.get_fills() == []
    assert e.venue.get_account().positions == ()


def test_depth_fraction_haircut(tmp_path: Path) -> None:
    e = Env(tmp_path, depth_fraction="0.5")
    st = e.venue.submit(intent(qty="40", price="0.87"))
    assert st.filled_quantity == 25  # floor(50 * 0.5)


def test_duplicate_client_order_id_never_re_executes(env: Env) -> None:
    env.venue.submit(intent())
    cash = env.venue.cash
    with pytest.raises(OrderRejected):
        env.venue.submit(intent())
    assert env.venue.cash == cash and len(env.venue.get_fills()) == 1


def test_mode_mismatch_and_live_mode_refused(env: Env) -> None:
    with pytest.raises(OrderRejected):
        env.venue.submit(intent(mode=Mode.DEMO))
    with pytest.raises(ValueError):
        PaperVenue(mode=Mode.LIVE, clock=env.clock, fees=FEES, book_source=lambda t: None,
                   market_source=lambda t: None, starting_cash=D(1))
    with pytest.raises(ValueError):
        PaperVenue(mode=Mode.DEMO, clock=env.clock, fees=FEES, book_source=lambda t: None,
                   market_source=lambda t: None, starting_cash=D(1))


def test_no_book_or_market_rejects(env: Env) -> None:
    v = PaperVenue(mode=Mode.PAPER, clock=env.clock, fees=FEES, book_source=lambda t: None,
                   market_source=lambda t: env.mk, starting_cash=D(100))
    assert v.submit(intent()).state is OrderState.REJECTED


def test_buying_no_side_walks_no_asks(tmp_path: Path) -> None:
    bk = book(yes_asks=[], no_asks=[("0.88", "20")], yes_bids=[])
    e = Env(tmp_path, bk=bk)
    st = e.venue.submit(intent(outcome=Outcome.NO, qty="10", price="0.88"))
    assert st.state is OrderState.FILLED
    pos = e.venue.get_account().positions[0]
    assert pos.outcome is Outcome.NO and pos.quantity == 10
    pay = e.venue.settle(TICKER, SettlementResult.NO)
    assert pay == 10


def test_gtc_rests_reserves_cash_and_cancel_releases(env: Env) -> None:
    low = intent(cid="g1", qty="10", price="0.80", tif=TimeInForce.GOOD_TILL_CANCELED)
    st = env.venue.submit(low)
    assert st.state is OrderState.RESTING and st.remaining_quantity == 10
    reserve = D(10) * D("0.80") + FEES.fee(D(10), D("0.80"))
    assert env.venue.cash == D(1000) - reserve
    assert env.venue.get_account().open_orders[0].client_order_id == "g1"
    out = env.venue.cancel("g1")
    assert out is not None and out.state is OrderState.CANCELED
    assert env.venue.cash == D(1000)
    assert env.venue.cancel("g1").state is OrderState.CANCELED  # type: ignore[union-attr]
    assert env.venue.cancel("missing") is None


def test_gtc_partial_fill_then_rest(env: Env) -> None:
    st = env.venue.submit(intent(cid="g2", qty="80", price="0.87",
                                 tif=TimeInForce.GOOD_TILL_CANCELED))
    assert st.state is OrderState.PARTIALLY_FILLED
    assert st.filled_quantity == 50 and st.remaining_quantity == 30
    env.venue.cancel("g2")
    assert env.venue.get_account().positions[0].quantity == 50  # cancel never liquidates


def test_settlement_wins_losses_void_and_cash_identity(env: Env) -> None:
    env.venue.submit(intent())
    cost = D(1000) - env.venue.cash
    fee = sum((f.fee for f in env.venue.get_fills()), D(0))
    assert env.venue.settle(TICKER, SettlementResult.VOID) == cost - fee  # fees not refunded
    assert env.venue.get_account().positions == ()
    assert env.venue.settle(TICKER, SettlementResult.YES) == 0  # nothing left


def test_settle_cancels_resting_orders_on_that_ticker(env: Env) -> None:
    env.venue.submit(intent(cid="g3", price="0.80", tif=TimeInForce.GOOD_TILL_CANCELED))
    env.venue.settle(TICKER, SettlementResult.NO)
    assert env.venue.list_open_orders() == []
    assert env.venue.cash == D(1000)


def test_fill_probability_zero_cancels_deterministically(tmp_path: Path) -> None:
    e = Env(tmp_path)
    e.venue.fill_probability = D("0")
    assert e.venue.submit(intent()).state is OrderState.CANCELED


def test_fault_injection_is_one_shot(env: Env) -> None:
    env.venue.fault = "timeout_before_accept"
    with pytest.raises(AmbiguousSubmission):
        env.venue.submit(intent("a"))
    assert env.venue.get_order("a") is None and env.venue.cash == D(1000)
    assert env.venue.submit(intent("b")).state is OrderState.FILLED  # fault cleared
    env.venue.fault = "timeout_after_accept"
    with pytest.raises(AmbiguousSubmission):
        env.venue.submit(intent("c"))
    assert env.venue.get_order("c") is not None  # accepted despite the exception


# ------------------------------------------------------------------------- engine
def test_engine_happy_path_persists_everything(env: Env) -> None:
    res = env.engine.submit(intent())
    assert res.state is OrderState.FILLED and len(res.new_fills) == 1
    o = env.store.get_order("c1")
    assert o is not None and o.state is OrderState.FILLED and o.filled_quantity == 10
    assert len(env.store.fills()) == 1


def test_engine_duplicate_intent_does_not_touch_venue(env: Env) -> None:
    env.engine.submit(intent())
    cash = env.venue.cash
    with pytest.raises(DuplicateIntent):
        env.engine.submit(intent())
    assert env.venue.cash == cash and len(env.venue.get_fills()) == 1


def test_ambiguous_after_accept_resolves_by_query_without_resubmitting(env: Env) -> None:
    calls = {"n": 0}
    real = env.venue.submit

    def counting(i: OrderIntent):
        calls["n"] += 1
        return real(i)

    env.venue.submit = counting  # type: ignore[method-assign]
    env.venue.fault = "timeout_after_accept"
    res = env.engine.submit(intent())
    assert calls["n"] == 1  # never blindly retried
    assert res.state is OrderState.FILLED and len(res.new_fills) == 1
    assert env.store.get_order("c1").state is OrderState.FILLED  # type: ignore[union-attr]
    assert len(env.venue.get_fills()) == 1


def test_ambiguous_before_accept_stays_unknown_until_grace_then_retired(env: Env) -> None:
    env.venue.fault = "timeout_before_accept"
    res = env.engine.submit(intent())
    assert res.state is OrderState.UNKNOWN and res.note == "not_found_yet"
    assert [o.client_order_id for o in env.store.unresolved_orders()] == ["c1"]
    assert env.venue.cash == D(1000)
    env.clock.advance(timedelta(seconds=30))
    assert env.engine.resolve_unresolved()[0].state is OrderState.UNKNOWN
    env.clock.advance(timedelta(seconds=31))  # past 60s grace
    out = env.engine.resolve_unresolved()[0]
    assert out.state is OrderState.REJECTED and out.note == "not_found_after_grace"
    assert env.store.unresolved_orders() == []
    # the id is retired: re-using it can never produce an order
    with pytest.raises(DuplicateIntent):
        env.engine.submit(intent())


def test_query_failure_keeps_order_unknown(env: Env) -> None:
    env.venue.fault = "timeout_after_accept"

    def broken(cid: str):
        raise RuntimeError("network down")

    env.venue.get_order = broken  # type: ignore[method-assign]
    res = env.engine.submit(intent())
    assert res.state is OrderState.UNKNOWN and res.note == "query_failed"
    assert env.store.unresolved_orders()


def test_venue_rejection_marks_rejected(env: Env) -> None:
    env.venue.submit(intent())  # id now exists at the venue but NOT in the store
    res = env.engine.submit(intent())
    assert res.state is OrderState.REJECTED
    assert env.store.get_order("c1").state is OrderState.REJECTED  # type: ignore[union-attr]


def test_insufficient_buying_power_through_engine(tmp_path: Path) -> None:
    e = Env(tmp_path, cash="5")
    res = e.engine.submit(intent(max_cost="50"))
    assert res.state is OrderState.REJECTED
    assert e.venue.cash == D(5)


def test_sync_fills_dedups_delayed_duplicate_fills(env: Env) -> None:
    env.engine.submit(intent())
    first = env.venue.get_fills()
    env.venue._fills.append(first[0])  # exchange re-delivers the same fill
    env.venue._fills.insert(0, first[0])  # and out of order
    assert env.engine.sync_fills() == []
    assert len(env.store.fills()) == 1


def test_cancel_all_open_cancels_resting_never_liquidates(env: Env) -> None:
    env.engine.submit(intent("fill1"))
    env.engine.submit(intent("rest1", price="0.80", tif=TimeInForce.GOOD_TILL_CANCELED))
    assert [o.client_order_id for o in env.venue.list_open_orders()] == ["rest1"]
    pos_before = env.venue.get_account().positions
    done = env.engine.cancel_all_open("test")
    assert list(done) == ["rest1"]
    assert env.store.get_order("rest1").state is OrderState.CANCELED  # type: ignore[union-attr]
    assert env.venue.get_account().positions == pos_before  # positions intact
    assert env.venue.get_account().open_orders == ()


def test_cancel_race_cancel_after_fill_is_noop(env: Env) -> None:
    env.engine.submit(intent("x"))
    out = env.venue.cancel("x")
    assert out is not None and out.state is OrderState.FILLED  # already terminal
    assert env.venue.cash < D(1000)


# --------------------------------------------------------------------- reconcile
def test_reconcile_clean(env: Env) -> None:
    env.engine.submit(intent())
    rep = reconcile(env.engine)
    assert rep.ok and rep.issues == []


def test_reconcile_flags_untracked_venue_order(env: Env) -> None:
    env.venue.submit(intent("ghost", price="0.80", tif=TimeInForce.GOOD_TILL_CANCELED))
    rep = reconcile(env.engine)
    assert not rep.ok and any(i.startswith("untracked_venue_order:ghost") for i in rep.issues)


def test_reconcile_flags_position_mismatch_for_unknown_fill(env: Env) -> None:
    env.venue.submit(intent("ghost"))  # fills at venue; reconcile syncs fills but no order row
    rep = reconcile(env.engine)
    # a fill with no local order is itself a blocking discrepancy (needs human review)
    assert not rep.ok and any(i.startswith("unmatched_fill") for i in rep.issues)
    env.venue._positions[(TICKER, Outcome.YES)].quantity += D(3)
    rep2 = reconcile(env.engine)
    assert not rep2.ok and any(i.startswith("position_mismatch") for i in rep2.issues)


def test_reconcile_flags_local_open_missing_on_venue(env: Env) -> None:
    env.engine.submit(intent("rest", price="0.80", tif=TimeInForce.GOOD_TILL_CANCELED))
    env.venue._orders.pop("rest")  # venue forgot it
    rep = reconcile(env.engine)
    assert not rep.ok


def test_reconcile_fails_closed_on_venue_exception(env: Env) -> None:
    def boom() -> None:
        raise ConnectionError("down")

    env.venue.get_account = boom  # type: ignore[method-assign]
    rep = reconcile(env.engine)
    assert not rep.ok and rep.issues[0].startswith("venue_query_failed")


def test_reconcile_blocks_when_unknown_order_unresolved(env: Env) -> None:
    env.venue.fault = "timeout_before_accept"
    env.engine.submit(intent())
    rep = reconcile(env.engine)
    assert not rep.ok and "unresolved_orders_remain" in rep.issues


def test_reconcile_ignores_settled_tickers(env: Env) -> None:
    env.engine.submit(intent())
    pay = env.venue.settle(TICKER, SettlementResult.YES)
    env.store.record_settlement(TICKER, SettlementResult.YES, T0, pay)
    assert reconcile(env.engine).ok


def test_reconcile_detects_negative_cash(env: Env) -> None:
    env.venue.cash = D("-1")
    assert "negative_cash" in reconcile(env.engine).issues


def test_refused_fill_does_not_leak_into_fees_paid(tmp_path: Path) -> None:
    e = Env(tmp_path, cash="8.75")
    assert e.venue.submit(intent(qty="10", max_cost="8.75")).state is OrderState.REJECTED
    assert e.venue.fees_paid == 0
