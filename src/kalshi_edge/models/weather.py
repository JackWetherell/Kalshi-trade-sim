"""Weather temperature-threshold estimator (forecast mean/sd -> normal CDF).

Forecast key format (documented contract with the forecast provider)::

    f"{station}:max_temp_f:{YYYY-MM-DD}"      station = station_map[market.series_ticker]
    date = market.strike["event_date"] (str/date/datetime) else expected_expiration_time date

The latest observation with ``collected_at <= as_of`` is used (ties broken by revision); if it is
older than ``max_forecast_age`` the estimate is None. ``values`` must hold ``mean`` and ``sd``.

Strike semantics (Kalshi ``strike_type``). Daily highs resolve on whole degrees, so with
``integer_valued=True`` a continuity correction of 0.5 is applied to the normal CDF ``Phi``
(``F(x) = Phi((x - mean)/sd)``):

* ``greater``          (T >  floor):  1 - F(floor + 0.5)      [T > k  <=>  T >= k+1]
* ``greater_or_equal`` (T >= floor):  1 - F(floor - 0.5)
* ``less``             (T <  cap):    F(cap - 0.5)
* ``less_or_equal``    (T <= cap):    F(cap + 0.5)
* ``between``          (floor <= T <= cap, inclusive): F(cap + 0.5) - F(floor - 0.5)

Floats are used only inside this probability model; outputs are Decimals quantized to 1e-6.
Without a calibrator the estimate is marked INSUFFICIENT with ``q_safe = 0`` so C/D never trade.
The calibrator must have been fit on samples oriented to the purchased outcome (the raw
probability of the outcome being bought is what gets transformed).
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from kalshi_edge.domain import (
    ZERO,
    CalibrationStatus,
    ForecastObservation,
    Market,
    Outcome,
    ProbabilityEstimate,
)
from kalshi_edge.interfaces import ExternalForecastProvider
from kalshi_edge.models.calibration import Calibrator, to_decimal

STRIKE_TYPES = frozenset({"greater", "greater_or_equal", "less", "less_or_equal", "between"})


def _cdf(x: float, mean: float, sd: float) -> float:
    return 0.5 * (1.0 + math.erf((x - mean) / (sd * math.sqrt(2.0))))


def _dec(v: Any) -> Decimal | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def prob_yes(strike: Mapping[str, Any], mean: float, sd: float, *,
             integer_valued: bool = True) -> float | None:
    """Raw P(YES) for a strike dict, or None if the strike is unsupported/malformed."""
    st = strike.get("strike_type")
    if st not in STRIKE_TYPES or sd <= 0:
        return None
    floor, cap = _dec(strike.get("floor_strike")), _dec(strike.get("cap_strike"))
    h = 0.5 if integer_valued else 0.0
    if st in ("greater", "greater_or_equal"):
        if floor is None:
            return None
        edge = float(floor) + (h if st == "greater" else -h)
        return 1.0 - _cdf(edge, mean, sd)
    if st in ("less", "less_or_equal"):
        if cap is None:
            return None
        edge = float(cap) + (h if st == "less_or_equal" else -h)
        return _cdf(edge, mean, sd)
    if floor is None or cap is None or cap < floor:
        return None
    return _cdf(float(cap) + h, mean, sd) - _cdf(float(floor) - h, mean, sd)


def _event_date(market: Market) -> date | None:
    raw = market.strike.get("event_date")
    if isinstance(raw, datetime):
        return raw.date()
    if isinstance(raw, date):
        return raw
    if isinstance(raw, str):
        try:
            return date.fromisoformat(raw[:10])
        except ValueError:
            return None
    if market.expected_expiration_time is not None:
        return market.expected_expiration_time.date()
    return None


class WeatherThresholdEstimator:
    model_id = "weather-threshold-normal"

    def __init__(self, provider: ExternalForecastProvider, calibrator: Calibrator | None, *,
                 station_map: Mapping[str, str], max_forecast_age: timedelta,
                 model_version: str, integer_valued: bool = True) -> None:
        self.provider = provider
        self.calibrator = calibrator
        self.station_map = dict(station_map)
        self.max_forecast_age = max_forecast_age
        self.model_version = model_version
        self.integer_valued = integer_valued

    def forecast_key(self, market: Market) -> str | None:
        station = self.station_map.get(market.series_ticker)
        d = _event_date(market)
        if station is None or d is None:
            return None
        return f"{station}:max_temp_f:{d.isoformat()}"

    def _latest(self, key: str, as_of: datetime) -> ForecastObservation | None:
        obs = [o for o in self.provider.observations(key, as_of) if o.collected_at <= as_of]
        if not obs:
            return None
        return max(obs, key=lambda o: (o.collected_at, o.revision))

    def estimate(self, market: Market, outcome: Outcome, as_of: datetime
                 ) -> ProbabilityEstimate | None:
        key = self.forecast_key(market)
        if key is None:
            return None
        obs = self._latest(key, as_of)
        if obs is None or as_of - obs.collected_at > self.max_forecast_age:
            return None
        mean, sd = obs.values.get("mean"), obs.values.get("sd")
        if mean is None or sd is None or sd <= ZERO:
            return None
        p_yes = prob_yes(market.strike, float(mean), float(sd),
                         integer_valued=self.integer_valued)
        if p_yes is None:
            return None
        raw = to_decimal(p_yes if outcome is Outcome.YES else 1.0 - p_yes)
        details: dict[str, Any] = {
            "forecast_key": key, "forecast_source": obs.source,
            "forecast_collected_at": obs.collected_at.isoformat(),
            "forecast_revision": obs.revision, "mean": str(mean), "sd": str(sd),
            "raw_p": str(raw), "strike_type": market.strike.get("strike_type"),
        }
        if self.calibrator is None:
            q, q_safe = raw, ZERO
            status, n_events = CalibrationStatus.INSUFFICIENT, 0
        else:
            q, q_safe = self.calibrator.transform(raw)
            q_safe = min(q, q_safe)
            status, n_events = self.calibrator.status, self.calibrator.n_events
        return ProbabilityEstimate(
            ticker=market.ticker, outcome=outcome, q=q, q_safe=q_safe, model_id=self.model_id,
            model_version=self.model_version, independent=True, calibration_status=status,
            as_of=obs.collected_at, category=market.category, n_calibration_events=n_events,
            details=details)
