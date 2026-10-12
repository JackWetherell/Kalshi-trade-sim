"""Loss stops (exact bounds, latching, rolling) and the persistent fail-closed kill switch."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kalshi_edge.risk import KillSwitch, RiskState
from tests.support import D, T0, limits

L = limits()


def fresh(eq: str = "1000") -> RiskState:
    return RiskState.fresh(D(eq), T0)


# ------------------------------------------------------------------------- stops
def test_daily_stop_trips_exactly_at_bound_not_before() -> None:
    s = fresh()
    s.mark(D("970.01"), T0)
    assert s.evaluate_stops(L) == [] and not s.daily_halt
    s.mark(D("970.00"), T0)  # exactly -3%
    assert s.evaluate_stops(L) == ["daily_stop"] and s.daily_halt


def test_weekly_stop_trips_exactly_at_bound() -> None:
    s = fresh()
    s.start_of_day_equity = D("940.00")  # isolate: daily floor (910) is below weekly bound
    s.mark(D("940.01"), T0)
    assert s.evaluate_stops(L) == []
    s.mark(D("940.00"), T0)
    assert s.evaluate_stops(L) == ["weekly_stop"] and s.weekly_halt


def test_drawdown_trips_exactly_at_ten_percent_of_peak() -> None:
    s = fresh()
    s.mark(D("1200"), T0)  # new peak
    s.start_of_day_equity = s.start_of_week_equity = D("1200")
    s.mark(D("1080.01"), T0)
    out = s.evaluate_stops(L)
    assert "max_drawdown" not in out and not s.drawdown_kill
    s.mark(D("1080.00"), T0)
    assert "max_drawdown" in s.evaluate_stops(L) and s.drawdown_kill


def test_stops_report_only_newly_tripped_and_latch() -> None:
    s = fresh()
    s.mark(D("850"), T0)  # trips all three
    assert set(s.evaluate_stops(L)) == {"daily_stop", "weekly_stop", "max_drawdown"}
    assert s.evaluate_stops(L) == []  # already latched, not reported again
    s.mark(D("1000"), T0)  # equity recovers: halts stay latched
    assert s.evaluate_stops(L) == []
    assert s.daily_halt and s.weekly_halt and s.drawdown_kill


def test_peak_only_ratchets_up() -> None:
    s = fresh()
    s.mark(D("1100"), T0)
    s.mark(D("1050"), T0)
    assert s.peak_equity == 1100 and s.current_equity == 1050


def test_daily_halt_clears_on_new_day_but_weekly_and_drawdown_persist() -> None:
    s = fresh()
    s.mark(D("850"), T0)
    s.evaluate_stops(L)
    nxt = T0 + timedelta(days=1)  # Tue 2026-01-06, same ISO week
    s.mark(D("850"), nxt)
    assert not s.daily_halt and s.start_of_day_equity == D("850")
    assert s.weekly_halt and s.drawdown_kill
    assert s.orders_today == 0
    s.evaluate_stops(L)  # still 15% below the *peak* -> drawdown remains latched, no re-trip
    assert s.drawdown_kill


def test_new_week_resets_start_of_week_but_not_latches() -> None:
    s = fresh()
    s.mark(D("900"), T0)
    s.weekly_halt = True
    s.mark(D("900"), T0 + timedelta(days=7))
    assert s.start_of_week_equity == D("900")
    assert s.weekly_halt  # human review required, never auto-cleared


def test_orders_today_resets_on_utc_day_roll() -> None:
    s = fresh()
    s.orders_today = 7
    s.mark(D("1000"), datetime(2026, 1, 5, 23, 59, tzinfo=UTC))
    assert s.orders_today == 7
    s.mark(D("1000"), datetime(2026, 1, 6, 0, 0, tzinfo=UTC))
    assert s.orders_today == 0


def test_json_roundtrip_preserves_latches() -> None:
    s = fresh()
    s.mark(D("850"), T0)
    s.evaluate_stops(L)
    s.orders_today = 3
    back = RiskState.from_json(json.loads(json.dumps(s.to_json())))
    assert back == s


# ---------------------------------------------------------------------- kill switch
def test_not_engaged_when_absent_or_dir_missing(tmp_path: Path) -> None:
    assert KillSwitch(tmp_path / "x" / "KILL").engaged is False
    assert KillSwitch(tmp_path / "KILL").engaged is False


def test_engage_persists_across_instances_and_keeps_first_reason(tmp_path: Path) -> None:
    p = tmp_path / "state" / "KILL"
    KillSwitch(p).engage("first", now=T0)
    ks2 = KillSwitch(p)  # simulates a process restart
    st = ks2.state()
    assert st.engaged and st.reason == "first" and st.since == T0.isoformat()
    ks2.engage("second")
    assert KillSwitch(p).state().reason == "first"


def test_reset_requires_operator_confirmation(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path / "KILL")
    ks.engage("x")
    with pytest.raises(PermissionError):
        ks.reset(operator_confirmed=False)
    assert ks.engaged
    ks.reset(operator_confirmed=True)
    assert not KillSwitch(tmp_path / "KILL").engaged


def test_fail_closed_when_unreadable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "KILL"
    p.mkdir()  # exists but cannot be read as a file
    st = KillSwitch(p).state()
    assert st.engaged and "unreadable" in st.reason
    p.rmdir()
    p.write_text("{}")

    def boom(self: Path, *a: object, **k: object) -> str:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "read_text", boom)
    assert KillSwitch(p).engaged


def test_fail_closed_when_state_dir_not_accessible(tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    d = tmp_path / "locked"
    d.mkdir()
    monkeypatch.setattr(os, "access", lambda *a, **k: False)
    st = KillSwitch(d / "KILL").state()
    assert st.engaged and "fail closed" in st.reason


def test_empty_and_corrupt_files_still_engaged(tmp_path: Path) -> None:
    p = tmp_path / "KILL"
    p.write_text("")
    assert KillSwitch(p).engaged
    p.write_text("not json at all")
    st = KillSwitch(p).state()
    assert st.engaged and st.reason == "not json at all"


@pytest.mark.xfail(strict=True, reason="BUG: KillSwitch.state() raises AttributeError when the "
                   "file holds valid JSON that is not an object (e.g. '[]', 'null', '5') instead "
                   "of failing closed with engaged=True (risk/killswitch.py:45)")
@pytest.mark.parametrize("content", ["[]", "null", "5", '"x"'])
def test_non_object_json_is_still_engaged(tmp_path: Path, content: str) -> None:
    p = tmp_path / "KILL"
    p.write_text(content)
    assert KillSwitch(p).state().engaged is True


def test_engage_is_atomic_no_tmp_left(tmp_path: Path) -> None:
    ks = KillSwitch(tmp_path / "KILL")
    ks.engage("r")
    assert sorted(x.name for x in tmp_path.iterdir()) == ["KILL"]
    assert json.loads((tmp_path / "KILL").read_text())["reason"] == "r"
