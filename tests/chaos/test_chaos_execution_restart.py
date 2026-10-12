"""Failure injection through TradingLoop: ambiguous submits, restarts, reconcile gating."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_edge.domain import (
    OrderIntent,
    OrderState,
    Outcome,
    StrategyId,
    TimeInForce,
)
from kalshi_edge.execution import DuplicateIntent
from kalshi_edge.risk import KillSwitch
from tests.support import T0, Rig, make_rig, market

D = Decimal
TK = market().ticker


def restart(rig: Rig, tmp_path: Path) -> Rig:
    """Simulate a process crash + restart: same exchange (venue/md/clock), new Store/Loop."""
    rig.store.close()
    return make_rig(tmp_path, venue=rig.venue, md=rig.md, clock=rig.clock,
                    db_name=rig.db_path.name)


class FlakyQuery:
    """Makes venue.get_order raise for the first ``n`` calls (order lookup outage)."""

    def __init__(self, rig: Rig, n: int | None = 1) -> None:
        self.rig, self.n = rig, n
        self.real = rig.venue.get_order
        rig.venue.get_order = self  # type: ignore[method-assign]

    def __call__(self, cid: str):  # noqa: ANN204
        if self.n is None or self.n > 0:
            if self.n is not None:
                self.n -= 1
            raise ConnectionError("order query unavailable")
        return self.real(cid)

    def restore(self) -> None:
        self.rig.venue.get_order = self.real  # type: ignore[method-assign]


def test_timeout_after_accept_then_restart_does_not_duplicate(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    flaky = FlakyQuery(rig, n=None)  # query outage: cannot resolve
    rig.venue.fault = "timeout_after_accept"
    rep = rig.loop.step()
    assert rep.submitted == 1
    assert [o.state for o in rig.store.orders()] == [OrderState.UNKNOWN]
    assert len(rig.venue._orders) == 1  # the venue DID accept it
    cash_after_first = rig.venue.cash

    # while the outcome is unknown, no further trading in the same process
    rig.step_forward(seconds=1)
    rig.refresh_book()
    rep2 = rig.loop.step()
    assert rep2.submitted == 0 and not rep2.reconciled
    assert rep2.rejections.get("not_reconciled", 0) + rep2.rejections.get(
        "unresolved_prior_orders", 0) >= 1
    assert len(rig.venue._orders) == 1 and rig.venue.cash == cash_after_first

    # crash + restart with the outage over: startup reconcile adopts the fill, no resubmit
    flaky.restore()
    rig2 = restart(rig, tmp_path)
    assert rig2.store.get_order(rig.store.orders()[0].client_order_id if False else
                                list(rig.venue._orders)[0]).state is OrderState.UNKNOWN
    rep3 = rig2.loop.startup()
    assert rep3.ok, rep3.issues
    (order,) = rig2.store.orders()
    assert order.state is OrderState.FILLED
    assert len(rig.venue._orders) == 1 and len(rig2.store.fills()) == 1
    assert rig.venue.cash == cash_after_first


def test_timeout_before_accept_retires_id_after_grace_and_trades_with_new_id(
        tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.venue.fault = "timeout_before_accept"
    assert rig.loop.step().submitted == 1
    (first,) = rig.store.orders()
    assert first.state is OrderState.UNKNOWN and len(rig.venue._orders) == 0
    # immediate retry attempt in the same process is blocked by the unresolved order
    rig.step_forward(seconds=2)
    rig.refresh_book()
    rep = rig.loop.step()
    assert rep.submitted == 0 and len(rig.venue._orders) == 0

    rig2 = restart(rig, tmp_path)
    rig2.step_forward(seconds=61)  # beyond the 60s grace window
    rig2.refresh_book()
    assert rig2.loop.startup().ok
    assert rig2.store.get_order(first.client_order_id).state is OrderState.REJECTED  # type: ignore[union-attr]
    assert len(rig.venue._orders) == 0  # nothing ever reached the exchange
    assert rig2.loop.step().submitted == 1
    new = [o for o in rig2.store.orders() if o.client_order_id != first.client_order_id]
    assert len(new) == 1 and len(rig.venue._orders) == 1
    with pytest.raises(DuplicateIntent):  # retired id can never be reused
        rig2.loop.engine.submit(OrderIntent(
            first.client_order_id, "d", rig2.loop.mode, StrategyId.POSITIVE_EV_80, TK,
            "E", Outcome.YES, D(1), D("0.87"), TimeInForce.IMMEDIATE_OR_CANCEL, D(5), "h", "v",
            T0))


def test_query_before_retry_no_second_post_for_same_decision(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    submits: list[str] = []
    real = rig.venue.submit

    def spy(i: OrderIntent):  # noqa: ANN202
        submits.append(i.client_order_id)
        return real(i)

    rig.venue.submit = spy  # type: ignore[method-assign]
    rig.venue.fault = "timeout_after_accept"
    rig.loop.step()
    assert len(submits) == 1 and len(set(submits)) == 1
    st = rig.store.orders()[0]
    assert st.state is OrderState.FILLED  # resolved by query, not by resubmitting


def test_untracked_venue_order_blocks_all_trading_until_cleared(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    # an order exists at the exchange that this process knows nothing about
    rig.venue.submit(OrderIntent("rogue-1", "d", rig.loop.mode, StrategyId.POSITIVE_EV_80, TK,
                                 "E", Outcome.YES, D(5), D("0.80"),
                                 TimeInForce.GOOD_TILL_CANCELED, D(10), "h", "v", T0))
    rep = rig.loop.step()
    assert not rep.reconciled and rep.submitted == 0
    assert rep.rejections.get("not_reconciled", 0) >= 1
    assert not rig.loop.startup().ok
    rig.venue.cancel("rogue-1")
    rig.step_forward(seconds=1)
    rig.refresh_book()
    rep2 = rig.loop.step()
    assert rep2.reconciled and rep2.submitted == 1


def test_position_mismatch_blocks_trading(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    assert rig.loop.step().submitted == 1
    rig.venue._positions[(TK, Outcome.YES)].quantity += D(1)  # venue disagrees with our fills
    rig.step_forward(seconds=1)
    rig.refresh_book()
    rep = rig.loop.step()
    assert not rep.reconciled and rep.submitted == 0


def test_venue_outage_during_reconcile_blocks_trading(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)

    def down() -> None:
        raise ConnectionError("exchange down")

    rig.venue.get_account = down  # type: ignore[method-assign]
    rep = rig.loop.step()
    assert not rep.reconciled and rep.halted == "no_account_snapshot" and rep.submitted == 0


def test_restart_resume_reconciles_and_loads_risk_state(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    assert rig.loop.step().submitted == 1
    orders_today = rig.loop.state.orders_today
    pos = rig.venue.get_account().positions
    rig2 = restart(rig, tmp_path)
    assert rig2.loop.state.orders_today == orders_today  # persisted
    assert rig2.loop.startup().ok
    assert rig2.venue.get_account().positions == pos


def test_duplicate_and_delayed_fills_do_not_change_positions(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.loop.step()
    n = len(rig.store.fills())
    rig.venue._fills.extend(list(rig.venue._fills))  # exchange replays the fills
    rig.venue._fills.reverse()  # and out of order
    rig.step_forward(seconds=1)
    rig.refresh_book()
    rep = rig.loop.step()
    assert rep.reconciled
    assert len(rig.store.fills()) >= n
    seen = [f.fill_id for f in rig.store.fills()]
    assert len(seen) == len(set(seen))
    implied = sum((f.quantity for f in rig.store.fills()), D(0))
    assert implied == sum((p.quantity for p in rig.venue.get_account().positions), D(0))


def test_partial_fill_is_booked_exactly_and_reconciles(tmp_path: Path) -> None:
    # risk approves ~11 contracts on a 30-deep book, but the venue only gives 30% of depth
    from tests.support import book
    rig = make_rig(tmp_path, depth_fraction="0.3",
                   books=[book(yes_asks=[("0.87", "30")], yes_bids=[("0.85", "10")])])
    rep = rig.loop.step()
    assert rep.submitted == 1 and rep.filled_contracts == 9  # floor(30 * 0.3)
    (o,) = rig.store.orders()
    assert o.state is OrderState.CANCELED and o.filled_quantity == 9  # IOC remainder dropped
    assert rig.venue.get_account().positions[0].quantity == 9
    rig.step_forward(seconds=1)
    assert rig.loop.startup().ok


def test_kill_with_failing_cancel_still_halts_and_does_not_crash(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.kill.engage("stop")

    def boom(cid: str):  # noqa: ANN202
        raise ConnectionError("cancel failed")

    rig.venue.cancel = boom  # type: ignore[method-assign]
    rig.venue.submit(OrderIntent("g", "d", rig.loop.mode, StrategyId.POSITIVE_EV_80, TK, "E",
                                 Outcome.YES, D(5), D("0.80"), TimeInForce.GOOD_TILL_CANCELED,
                                 D(10), "h", "v", T0))
    rep = rig.loop.step()
    assert rep.halted and rep.submitted == 0


def test_kill_during_unresolved_order_does_not_resubmit(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.venue.fault = "timeout_before_accept"
    rig.loop.step()
    rig.kill.engage("stop")
    rig.step_forward(seconds=100)
    rep = rig.loop.step()
    assert rep.halted and len(rig.venue._orders) == 0


def test_clock_regression_is_impossible_for_replay_clock(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    with pytest.raises(ValueError):
        rig.clock.set(T0 - timedelta(seconds=1))


def test_kill_switch_file_never_cleared_by_loop(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.kill.engage("x")
    for _ in range(3):
        rig.loop.step()
    assert KillSwitch(tmp_path / "KILL_SWITCH").engaged
