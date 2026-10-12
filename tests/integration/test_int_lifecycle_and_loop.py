"""End-to-end TradingLoop + PaperVenue scenarios: lifecycle, accounting, stops, kill."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from kalshi_edge.data.memory import InMemoryMarketData
from kalshi_edge.domain import (
    Market,
    MarketStatus,
    Mode,
    OrderIntent,
    OrderState,
    Outcome,
    SettlementResult,
    StrategyId,
    TimeInForce,
)
from kalshi_edge.risk import KillSwitch
from tests.support import (
    FEES,
    T0,
    FixedEstimator,
    Rig,
    book,
    limits,
    make_rig,
    market,
    replace_market,
)

D = Decimal


def tk(i: int) -> str:
    return f"KXHIGH{i}-26JAN05-T40"


def multi_market_rig(tmp_path: Path, n: int = 3, **kw: object) -> Rig:
    mkts = [market(ticker=tk(i), event_ticker=f"KXHIGH{i}-26JAN05", series_ticker=f"KXHIGH{i}")
            for i in range(n)]
    bks = [book(ticker=tk(i), ts=T0) for i in range(n)]
    return make_rig(tmp_path, markets=mkts, books=bks, **kw)  # type: ignore[arg-type]


def refresh_all(rig: Rig, n: int) -> None:
    for i in range(n):
        rig.md.set_book(book(ticker=tk(i), ts=rig.clock.now()))


def gtc_intent(cid: str, price: str = "0.80", qty: str = "10", ticker: str = tk(0)
               ) -> OrderIntent:
    return OrderIntent(cid, "d-" + cid, Mode.PAPER, StrategyId.POSITIVE_EV_80, ticker,
                       ticker.split("-T")[0], Outcome.YES, D(qty), D(price),
                       TimeInForce.GOOD_TILL_CANCELED, D("50"), "h", "v", T0)


# ------------------------------------------------------------------------ lifecycle
def test_full_lifecycle_wins_and_losses_and_accounting_identity(tmp_path: Path) -> None:
    rig = multi_market_rig(tmp_path, 3)
    start_cash = rig.venue.cash
    rep = rig.loop.step()
    assert rep.submitted >= 3 and rep.reconciled, rep.as_dict()
    assert rep.rejections.get("not_reconciled") is None
    # every individual entry respected the 1% cap (cost basis incl. fees)
    for pos in rig.venue.get_account().positions:
        assert pos.cost_basis <= D("10.09")
    fills = rig.store.fills()
    cost = sum((f.quantity * f.price for f in fills), D(0))
    fees = sum((f.fee for f in fills), D(0))
    assert fees == sum((FEES.fee(f.quantity, f.price) for f in fills), D(0))
    assert rig.venue.cash == start_cash - cost - fees

    # resolve: market 0 -> YES (we win), markets 1,2 -> NO (we lose)
    rig.step_forward(hours=11)
    results = {0: SettlementResult.YES, 1: SettlementResult.NO, 2: SettlementResult.NO}
    for i, res in results.items():
        rig.md.upsert_market(market(ticker=tk(i), event_ticker=f"KXHIGH{i}-26JAN05",
                                    status=MarketStatus.SETTLED, result=res))
        rig.md.set_book(book(ticker=tk(i), ts=rig.clock.now()))
    rig.loop.step()
    assert rig.venue.get_account().positions == ()
    assert rig.store.settled_tickers() == {tk(0), tk(1), tk(2)}

    payouts = sum((f.quantity for f in fills if f.ticker == tk(0)), D(0))  # YES fills on winner
    assert payouts > 0
    assert rig.venue.cash == start_cash - cost - fees + payouts  # the accounting identity
    assert sum((D(r["payout"]) for r in rig.store.settlements()), D(0)) == payouts
    assert rig.venue.realized_pnl == payouts - cost - fees
    # a winner AND two losers happened
    won = [e for e in rig.venue.settlement_log if e["payout"] > 0]
    lost = [e for e in rig.venue.settlement_log if e["payout"] == 0]
    assert len(won) == 1 and len(lost) == 2


def test_no_trade_when_q_safe_equals_breakeven(tmp_path: Path) -> None:
    # YES ask .87, fee/c .008..0.01 -> p+f ~ .878-.88 ; NO ask .15 is below the .80 screen
    rig = make_rig(tmp_path, estimator=FixedEstimator(D("0.87")))
    assert rig.loop.step().submitted == 0 and rig.venue.cash == D("1009")


def test_missing_estimate_means_no_trade(tmp_path: Path) -> None:
    class NoEst:
        model_id, model_version = "none", "0"

        def estimate(self, m: Market, o: Outcome, t: object) -> None:
            return None

    rig = make_rig(tmp_path)
    rig.loop.estimator = NoEst()  # type: ignore[assignment]
    assert rig.loop.step().submitted == 0


def test_estimator_exception_fails_closed(tmp_path: Path) -> None:
    class Boom:
        model_id, model_version = "b", "0"

        def estimate(self, m: Market, o: Outcome, t: object) -> None:
            raise RuntimeError("model crashed")

    rig = make_rig(tmp_path)
    rig.loop.estimator = Boom()  # type: ignore[assignment]
    assert rig.loop.step().submitted == 0


def test_p_at_or_below_80_no_trade_for_a_b_c(tmp_path: Path) -> None:
    for sid in (StrategyId.NAIVE_80, StrategyId.SCALED_80, StrategyId.POSITIVE_EV_80):
        sub = tmp_path / sid.value
        sub.mkdir()
        rig = make_rig(sub, strategy_id=sid, q_yes="0.99",
                       books=[book(yes_asks=[("0.80", "1000")], yes_bids=[("0.78", "10")])])
        assert rig.loop.step().submitted == 0, sid


def test_category_not_allowlisted_is_never_scanned_for_trades(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, markets=[market(category="crypto")])
    rep = rig.loop.step()
    assert rep.markets_scanned == 1 and rep.markets_eligible == 0 and rep.submitted == 0


# ----------------------------------------------------------- freshness / market state
def test_stale_book_is_rejected_by_final_check(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.step_forward(seconds=10)  # book ts is still T0
    rep = rig.loop.step()
    assert rep.submitted == 0 and rep.rejections.get("stale_or_future_book", 0) >= 1


def test_stale_forecast_is_rejected(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, estimator=FixedEstimator(D("0.95"), as_of_lag=timedelta(hours=7)))
    rep = rig.loop.step()
    assert rep.submitted == 0 and rep.rejections.get("stale_forecast", 0) >= 1


def test_forecast_just_inside_staleness_window_still_trades(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, estimator=FixedEstimator(D("0.95"), as_of_lag=timedelta(hours=5)))
    assert rig.loop.step().submitted == 1


class ListsEverything(InMemoryMarketData):
    def list_markets(self) -> Sequence[Market]:
        return list(self.markets.values())


@pytest.mark.parametrize("status", [MarketStatus.PAUSED, MarketStatus.CLOSED,
                                    MarketStatus.SETTLED, MarketStatus.UNKNOWN])
def test_halted_or_settled_markets_never_trade(tmp_path: Path, status: MarketStatus) -> None:
    md = ListsEverything()
    md.upsert_market(market(status=status))
    md.set_book(book())
    rig = make_rig(tmp_path, md=md)
    assert rig.loop.step().submitted == 0 and rig.venue.get_fills() == []


def test_market_data_failure_halts_without_trading(tmp_path: Path) -> None:
    class Down(InMemoryMarketData):
        def list_markets(self) -> Sequence[Market]:
            raise ConnectionError("feed down")

    rig = make_rig(tmp_path, md=Down())
    rep = rig.loop.step()
    assert rep.halted == "market_discovery_failed" and rep.submitted == 0
    assert any(k == "stale_feed" for k, _ in rig.alerts.sent)


def test_feed_unhealthy_blocks_trading(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.loop.feed_health = lambda: False
    rep = rig.loop.step()
    assert rep.submitted == 0 and rep.rejections.get("feed_unhealthy", 0) >= 1


def test_clock_skew_blocks_trading(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.loop.clock_skew = lambda: 5.0
    assert rig.loop.step().rejections.get("clock_skew", 0) >= 1


def test_ws_sequence_gap_drops_book_and_halts_decisions(tmp_path: Path) -> None:
    from kalshi_edge.exchange.ws import BookBuilder, SequenceGap
    rig = make_rig(tmp_path, books=[])
    b = BookBuilder(market().ticker)
    snap = {"type": "orderbook_snapshot", "seq": 1,
            "msg": {"market_ticker": b.ticker, "yes_dollars_fp": [["0.8500", "10.00"]],
                    "no_dollars_fp": [["0.1300", "1000.00"]]}}

    def feed() -> None:
        ob = b.to_orderbook()
        if ob is None:
            rig.md.books.pop(b.ticker, None)
        else:
            rig.md.set_book(replace(ob, ts=rig.clock.now()))

    b.handle(snap, rig.clock.now())
    feed()
    assert rig.loop.step().submitted == 1  # good book -> trades
    rig.step_forward(seconds=1)
    gap = {"type": "orderbook_delta", "seq": 5,
           "msg": {"market_ticker": b.ticker, "price_dollars": "0.1300", "delta_fp": "1.00",
                   "side": "no"}}
    with pytest.raises(SequenceGap):
        b.handle(gap, rig.clock.now())
    feed()
    before = len(rig.venue.get_fills())
    rep = rig.loop.step()
    assert rep.submitted == 0 and len(rig.venue.get_fills()) == before
    # still halted for further deltas until a fresh snapshot arrives
    assert b.to_orderbook() is None


# ------------------------------------------------------------------- loss stops
def test_daily_stop_trips_exactly_at_three_percent_in_loop_and_cancels_resting(
        tmp_path: Path) -> None:
    rig = make_rig(tmp_path, q_yes="0.5")  # no trading signal: isolate the stop logic
    rig.loop.engine.submit(gtc_intent("rest1", ticker=market().ticker))
    assert [o.client_order_id for o in rig.venue.list_open_orders()] == ["rest1"]
    # equity mark = cash + resting reserve.  Strategy capital 1009 -> floor 978.73
    reserve = D(10) * D("0.80")  # AccountSnapshot.open_order_reserved ignores the fee
    rig.venue.cash = D("978.74") - reserve
    rep = rig.loop.step()
    assert rep.equity == D("978.74") and rep.halted != "loss_stop"
    rig.venue.cash = D("978.73") - reserve
    rep = rig.loop.step()
    assert rep.halted == "loss_stop" and rig.loop.state.daily_halt
    assert rig.venue.list_open_orders() == []  # resting entries cancelled
    assert any(k == "daily_stop" for k, _ in rig.alerts.sent)
    # latched across restart: a new loop on the same DB loads the persisted state
    rig2 = make_rig(tmp_path, venue=rig.venue, md=rig.md, clock=rig.clock, db_name="s.sqlite3")
    assert rig2.loop.state.daily_halt
    assert rig2.loop.step().halted == "loss_stop"


def test_drawdown_stop_latches_kill_switch_file(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.venue.cash = D("908.10")  # exactly -10% of the 1009 peak
    rep = rig.loop.step()
    assert rig.loop.state.drawdown_kill
    assert rep.halted and rep.halted.startswith("kill_switch")
    assert KillSwitch(tmp_path / "KILL_SWITCH").engaged  # persisted, separate instance
    rig.venue.cash = D("2000")  # recovery does not unlatch
    assert make_rig(tmp_path, venue=rig.venue, md=rig.md, clock=rig.clock,
                    db_name="s.sqlite3").loop.step().submitted == 0


def test_weekly_stop_requires_human_review(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    rig.loop.state.start_of_day_equity = D("500")  # isolate the weekly stop
    rig.venue.cash = D("1009") * D("0.94")
    rep = rig.loop.step()
    assert rig.loop.state.weekly_halt and rep.halted == "loss_stop"
    rig.venue.cash = D("1009")
    rig.step_forward(days=2)
    refresh_all(rig, 1)
    assert rig.loop.step().halted == "loss_stop"  # does not self-clear


def test_max_orders_per_day_enforced_and_rolls_over(tmp_path: Path) -> None:
    rig = make_rig(tmp_path, risk=limits(max_orders_per_day=1), books=[book()])
    assert rig.loop.step().submitted == 1
    rig.step_forward(seconds=1)
    rig.refresh_book()
    rep = rig.loop.step()
    assert rep.submitted == 0 and rep.rejections.get("daily_order_count_limit", 0) >= 1
    rig.step_forward(hours=13)  # next UTC day (T0 = 12:00)
    rig.refresh_book()
    replace_market(rig, close_time=rig.clock.now() + timedelta(hours=10))
    assert rig.loop.step().submitted == 1


# -------------------------------------------------------------------- kill switch
def test_kill_switch_cancels_resting_but_does_not_liquidate(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    assert rig.loop.step().submitted == 1
    pos_before = rig.venue.get_account().positions
    assert pos_before
    rig.loop.engine.submit(gtc_intent("g1", ticker=market().ticker))
    rig.loop.engine.submit(gtc_intent("g2", price="0.81", ticker=market().ticker))
    assert len(rig.venue.list_open_orders()) == 2
    cash_before = rig.venue.cash
    rig.kill.engage("operator stop", now=T0)
    rep = rig.loop.step()
    assert rep.halted and "operator stop" in rep.halted and rep.submitted == 0
    assert rig.venue.list_open_orders() == []
    assert rig.venue.get_account().positions == pos_before  # NOT liquidated
    assert rig.venue.cash > cash_before  # reserved cash released by the cancels
    for oid in ("g1", "g2"):
        assert rig.store.get_order(oid).state is OrderState.CANCELED  # type: ignore[union-attr]


def test_kill_switch_survives_restart_and_beats_every_strategy(tmp_path: Path) -> None:
    for sid in (StrategyId.NAIVE_80, StrategyId.SCALED_80, StrategyId.POSITIVE_EV_80,
                StrategyId.ABLATION):
        sub = tmp_path / sid.value
        sub.mkdir()
        rig = make_rig(sub, strategy_id=sid)
        KillSwitch(sub / "KILL_SWITCH").engage("x")  # engaged by "another process"
        rep = rig.loop.step()
        assert rep.halted and rep.submitted == 0 and rig.venue.get_fills() == [], sid


def test_unreadable_kill_switch_fails_closed_in_loop(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    (tmp_path / "KILL_SWITCH").mkdir()  # cannot be read as a file => engaged
    rep = rig.loop.step()
    assert rep.halted and rep.submitted == 0


def test_cluster_cap_limits_total_across_steps(tmp_path: Path) -> None:
    rig = make_rig(tmp_path)
    for _ in range(6):
        rig.step_forward(seconds=1)
        rig.refresh_book()
        rig.loop.step()
    cost = sum((p.cost_basis for p in rig.venue.get_account().positions), D(0))
    assert cost <= D("1009") * D("0.05")  # event cluster 5%
    assert cost <= D("1009") * D("0.10")
