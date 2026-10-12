from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from math import erf, sqrt

import pytest

from kalshi_edge.domain import (
    CalibrationStatus,
    ForecastObservation,
    Market,
    MarketStatus,
    OrderBook,
    Outcome,
    PriceLevel,
)
from kalshi_edge.models import IsotonicCalibrator, MarketImpliedEstimator, WeatherThresholdEstimator

D = Decimal
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def phi(x: float) -> float:
    return 0.5 * (1 + erf(x / sqrt(2)))


class FakeProvider:
    source = "fake"

    def __init__(self, obs: list[ForecastObservation]) -> None:
        self.obs = obs
        self.keys: list[str] = []

    def observations(self, key: str, as_of: datetime) -> Sequence[ForecastObservation]:
        self.keys.append(key)
        return [o for o in self.obs if o.key == key]


def obs(collected: datetime = NOW - timedelta(hours=1), mean: str = "70", sd: str = "3",
        key: str = "KNYC:max_temp_f:2026-10-13", revision: int = 0) -> ForecastObservation:
    return ForecastObservation("fake", key, collected, collected, NOW,
                               {"mean": D(mean), "sd": D(sd)}, revision)


def market(strike: dict[str, object], **kw: object) -> Market:
    base: dict[str, object] = dict(
        ticker="W-1", event_ticker="W", series_ticker="KXHIGHNY", category="weather", title="t",
        status=MarketStatus.OPEN, market_type="binary", open_time=NOW, close_time=NOW,
        price_ranges=(), strike=strike)
    base.update(kw)
    return Market(**base)  # type: ignore[arg-type]


def est(provider: FakeProvider, cal: IsotonicCalibrator | None = None
        ) -> WeatherThresholdEstimator:
    return WeatherThresholdEstimator(provider, cal, station_map={"KXHIGHNY": "KNYC"},
                                     max_forecast_age=timedelta(hours=6), model_version="t1")


def yes_q(strike: dict[str, object]) -> Decimal:
    e = est(FakeProvider([obs()])).estimate(
        market({**strike, "event_date": "2026-10-13"}), Outcome.YES, NOW)
    assert e is not None
    return e.q


def test_strike_types_with_continuity_correction() -> None:
    assert float(yes_q({"strike_type": "greater", "floor_strike": 70})) == pytest.approx(
        1 - phi(0.5 / 3), abs=2e-6)
    assert float(yes_q({"strike_type": "greater_or_equal", "floor_strike": 70})) == pytest.approx(
        1 - phi(-0.5 / 3), abs=2e-6)
    assert float(yes_q({"strike_type": "less", "cap_strike": 70})) == pytest.approx(
        phi(-0.5 / 3), abs=2e-6)
    assert float(yes_q({"strike_type": "less_or_equal", "cap_strike": 70})) == pytest.approx(
        phi(0.5 / 3), abs=2e-6)
    assert float(yes_q({"strike_type": "between", "floor_strike": 68, "cap_strike": 72})
                 ) == pytest.approx(phi(2.5 / 3) - phi(-2.5 / 3), abs=2e-6)
    ge = yes_q({"strike_type": "greater_or_equal", "floor_strike": 70})
    lt = yes_q({"strike_type": "less", "cap_strike": 70})
    assert ge + lt == D(1)


def test_unsupported_or_malformed_strike_returns_none() -> None:
    e = est(FakeProvider([obs()]))
    for strike in ({"strike_type": "custom"}, {"strike_type": "greater"},
                   {"strike_type": "between", "floor_strike": 70}, {}):
        assert e.estimate(market({**strike, "event_date": "2026-10-13"}), Outcome.YES,
                          NOW) is None


def test_no_outcome_is_complement_and_key_format() -> None:
    prov = FakeProvider([obs()])
    e = est(prov)
    m = market({"strike_type": "greater", "floor_strike": 75, "event_date": "2026-10-13"})
    y, n = e.estimate(m, Outcome.YES, NOW), e.estimate(m, Outcome.NO, NOW)
    assert y is not None and n is not None
    assert y.q + n.q == D(1)
    assert prov.keys[0] == "KNYC:max_temp_f:2026-10-13"
    assert y.independent and y.as_of == NOW - timedelta(hours=1)


def test_date_falls_back_to_expiration_time() -> None:
    prov = FakeProvider([obs()])
    m = market({"strike_type": "greater", "floor_strike": 75},
               expected_expiration_time=datetime(2026, 10, 13, 20, tzinfo=UTC))
    assert est(prov).estimate(m, Outcome.YES, NOW) is not None
    assert est(prov).forecast_key(m) == "KNYC:max_temp_f:2026-10-13"
    assert date(2026, 10, 13).isoformat() in prov.keys[0]


def test_stale_missing_future_and_unmapped_forecast() -> None:
    m = market({"strike_type": "greater", "floor_strike": 75, "event_date": "2026-10-13"})
    assert est(FakeProvider([obs(collected=NOW - timedelta(hours=7))])).estimate(
        m, Outcome.YES, NOW) is None
    assert est(FakeProvider([])).estimate(m, Outcome.YES, NOW) is None
    assert est(FakeProvider([obs(collected=NOW + timedelta(minutes=1))])).estimate(
        m, Outcome.YES, NOW) is None  # no look-ahead
    assert est(FakeProvider([obs(sd="0")])).estimate(m, Outcome.YES, NOW) is None
    other = market({"strike_type": "greater", "floor_strike": 75, "event_date": "2026-10-13"},
                   series_ticker="UNKNOWN")
    assert est(FakeProvider([obs()])).estimate(other, Outcome.YES, NOW) is None


def test_latest_observation_wins() -> None:
    m = market({"strike_type": "greater", "floor_strike": 75, "event_date": "2026-10-13"})
    old = obs(collected=NOW - timedelta(hours=3), mean="60")
    new = obs(collected=NOW - timedelta(hours=1), mean="80")
    e = est(FakeProvider([old, new])).estimate(m, Outcome.YES, NOW)
    assert e is not None and e.q > D("0.9")


def test_no_calibrator_is_insufficient_and_unusable() -> None:
    m = market({"strike_type": "greater", "floor_strike": 75, "event_date": "2026-10-13"})
    e = est(FakeProvider([obs()])).estimate(m, Outcome.YES, NOW)
    assert e is not None
    assert e.calibration_status is CalibrationStatus.INSUFFICIENT and e.q_safe == 0
    assert not e.usable_for_live


def test_with_calibrator_status_follows_calibrator() -> None:
    cal = IsotonicCalibrator().fit([0.2, 0.4, 0.6, 0.8], [0, 0, 1, 1], [1, 2, 3, 4])
    m = market({"strike_type": "greater", "floor_strike": 65, "event_date": "2026-10-13"})
    e = est(FakeProvider([obs()]), cal).estimate(m, Outcome.YES, NOW)
    assert e is not None and e.q_safe <= e.q and not e.usable_for_live
    cal.status = CalibrationStatus.ACCEPTED
    e2 = est(FakeProvider([obs()]), cal).estimate(m, Outcome.YES, NOW)
    assert e2 is not None and e2.usable_for_live and e2.n_calibration_events == 4


def test_market_implied_baseline_is_not_independent() -> None:
    book = OrderBook("W-1", (PriceLevel(D("0.10"), D(5)),), (PriceLevel(D("0.08"), D(5)),), NOW)
    m = market({})
    base = MarketImpliedEstimator(lambda t: book)
    e = base.estimate(m, Outcome.YES, NOW)
    assert e is not None and e.q == D("0.92") == e.q_safe  # YES ask = 1 - NO bid 0.08
    assert not e.independent and e.calibration_status is CalibrationStatus.NONE
    assert not e.usable_for_live
    mid = MarketImpliedEstimator(lambda t: book, use_mid=True).estimate(m, Outcome.YES, NOW)
    assert mid is not None and mid.q == D("0.51")
    assert MarketImpliedEstimator().estimate(m, Outcome.YES, NOW) is None
