"""TradingLoop edge paths: alerts, helper hooks, account-fetch failure, settlement hook."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_edge.domain import Mode, SettlementResult, StrategyId
from kalshi_edge.runtime.loop import default_submission_gate, run_steps
from tests.chaos.test_chaos_malicious_isolation import Scripted
from tests.support import TICKER, make_rig, proposal

D = Decimal


def test_submission_gate_defaults_live_to_closed() -> None:
    now = None
    for mode, expected in [(Mode.PAPER, True), (Mode.DEMO, True), (Mode.BACKTEST, True),
                           (Mode.LIVE, False)]:
        assert default_submission_gate(mode)(now) is expected  # type: ignore[arg-type]


def test_model_version_without_estimator(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    assert rig.loop.model_version == "fixed@test"
    rig.loop.estimator = None
    assert rig.loop.model_version == "none"
    assert rig.loop.step().submitted == 0  # no estimator => C cannot trade


def test_run_steps_and_heartbeat_event_every_step(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    reps = run_steps(rig.loop, 3)
    assert len(reps) == 3
    beats = [r for r in rig.store.recent_events(50) if r["kind"] == "step"]
    assert len(beats) == 3


def test_settle_ticker_hook_records_and_pays(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    assert rig.loop.step().submitted == 1
    qty = rig.venue.get_account().positions[0].quantity
    cash = rig.venue.cash
    pay = rig.loop.settle_ticker(TICKER, SettlementResult.YES)
    assert pay == qty and rig.venue.cash == cash + qty
    assert rig.store.settled_tickers() == {TICKER}


def test_settle_ticker_requires_a_settling_venue(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)

    class NoSettle:
        mode = Mode.PAPER

    rig.loop.engine.venue = NoSettle()  # type: ignore[assignment]
    with pytest.raises(RuntimeError):
        rig.loop.settle_ticker(TICKER, SettlementResult.NO)
    assert rig.loop.settle_resolved() == 0


def test_account_fetch_failure_at_final_check_blocks_the_order(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    real = rig.venue.get_account
    n = {"c": 0}

    def flaky():
        n["c"] += 1
        if n["c"] >= 3:  # settle scan, reconcile, then the pre-submit ledger snapshot fails
            raise ConnectionError("ledger unavailable")
        return real()

    rig.venue.get_account = flaky  # type: ignore[method-assign]
    rep = rig.loop.step()
    assert rep.submitted == 0 and rep.rejections.get("missing_fresh_book_or_account") == 1
    assert rig.venue.get_fills() == []


def test_repeated_venue_rejects_and_ambiguous_submits_raise_alerts(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, strategy=Scripted(
        lambda c: [proposal(qty="1") for _ in range(4)], StrategyId.POSITIVE_EV_80),
        risk=None)
    rig.venue._books = lambda t: None  # type: ignore[assignment]  # venue cannot see a book
    rep = rig.loop.step()
    assert rep.submitted == 4
    kinds = [k for k, _ in rig.alerts.sent]
    assert "repeated_rejects" in kinds
    assert kinds.count("order_submitted") == 4


def test_ambiguous_submit_alert(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.venue.fault = "timeout_before_accept"
    rig.loop.step()
    assert "ambiguous_submit" in [k for k, _ in rig.alerts.sent]
