"""Out-of-sync detection gaps and latch persistence (some are expected-to-fail bug markers)."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from tests.chaos.test_chaos_malicious_isolation import live_cfg, live_loop
from tests.support import T0, TICKER, make_rig, market

from kalshi_edge.domain import (
    Mode,
    OrderIntent,
    OrderState,
    Outcome,
    StrategyId,
    TimeInForce,
)
from kalshi_edge.execution import reconcile
from kalshi_edge.runtime.arming import ArmingToken
from kalshi_edge.strategies import make_strategy

D = Decimal


def ioc(cid: str, qty: str = "5") -> OrderIntent:
    return OrderIntent(cid, "d-" + cid, Mode.PAPER, StrategyId.POSITIVE_EV_80, TICKER,
                       market().event_ticker, Outcome.YES, D(qty), D("0.87"),
                       TimeInForce.IMMEDIATE_OR_CANCEL, D("50"), "h", "v", T0)


def test_fill_for_unknown_order_must_fail_reconcile(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.venue.submit(ioc("ghost-filled"))  # filled at the exchange, unknown to our store
    rep = reconcile(rig.loop.engine)
    assert not rep.ok


def test_late_acceptance_of_retired_order_must_be_flagged(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.venue.fault = "timeout_before_accept"
    rig.loop.step()
    (first,) = rig.store.orders()
    rig.step_forward(seconds=61)
    rig.loop.startup()
    assert rig.store.get_order(first.client_order_id).state is OrderState.REJECTED  # type: ignore[union-attr]
    # the exchange finally processes the request that "timed out"
    rig.venue.submit(replace(ioc(first.client_order_id), quantity=first.quantity))
    rep = reconcile(rig.loop.engine)
    assert not rep.ok


def test_live_risk_is_bounded_by_the_amount_confirmed_at_arming(tmp_path: Path) -> None:
    log: list[OrderIntent] = []
    cfg = live_cfg()
    tok = ArmingToken(host="api.elections.kalshi.com", config_hash="h", issued_at=T0,
                      expires_at=T0 + timedelta(hours=1), max_total_risk=D("1"))
    loop, _ = live_loop(tmp_path, make_strategy(cfg.strategy, cfg.risk), log, token=tok)
    loop.step()
    assert log and all(i.max_cost <= D("1") for i in log)


def test_latched_flags_survive_kill_reset_and_restart(tmp_path: Path) -> None:
    """Documented behaviour: resetting the kill-switch FILE does not clear the persisted
    drawdown latch; there is intentionally (and currently) no code path that does."""
    rig = make_rig(tmp_path)
    rig.venue.cash = D("900")  # >10% below the 1009 peak
    rig.loop.step()
    assert rig.loop.state.drawdown_kill and rig.kill.engaged
    rig.kill.reset(operator_confirmed=True)
    rig.venue.cash = D("1009")
    rig.step_forward(days=1)
    rig.refresh_book()
    rig.store.close()
    rig2 = make_rig(tmp_path, venue=rig.venue, md=rig.md, clock=rig.clock, db_name=rig.db_path.name)
    rep = rig2.loop.step()
    assert rep.halted == "loss_stop" and rep.submitted == 0
