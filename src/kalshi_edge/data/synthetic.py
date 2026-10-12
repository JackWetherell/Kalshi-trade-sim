"""Seeded, deterministic SYNTHETIC market worlds for backtest-engine validation.

    SYNTHETIC -- engine validation, NOT evidence of real-market edge.

Nothing here is calibrated to Kalshi data. The generator exists so the backtest engine,
accounting, risk gating, walk-forward calibration and reporting can be exercised end to end,
and so that known-by-construction worlds ("efficient", "favorite_underpriced", ...) show the
engine reports what it should. P&L from these worlds is never a prediction.

Information model (Gaussian martingale)
---------------------------------------
Each event has an outcome variable ``T`` (a city's daily high in F, or an economic print).
Public information arrives every ``info_interval_hours``: ``I_k = E[T | info_k]``, with
conditional variance ``v_k`` shrinking from ``sigma_open**2`` to ``sd_floor**2`` at close;
``I_{k+1} = I_k + N(0, v_k - v_{k+1})`` and ``T | I_k ~ N(I_k, v_k)`` exactly. The market's
fair belief for a strike is the model-free ``P(YES | I_k, v_k)`` (same normal-CDF with
continuity correction used by ``models.weather``), seen after ``market_reaction_minutes``.
A forecast provider publishes ``(I_k + bias + noise, sd)`` at ``collected_at = t_k + lag``
(``lag`` > reaction) so forecast information is never ahead of the market and never visible
before ``collected_at`` (no look-ahead).

Market makers quote hourly books: belief + logit noise + a scenario pricing-bias on the
favourite side, a random spread (cents) and random depth. Books contain YES and NO *bids*
only (asks implied by the opposite side). A quarter of the markets use the tapered sub-cent
price grid (0.001 steps below 0.10 and above 0.90).
"""

from __future__ import annotations

import bisect
import math
import random
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from kalshi_edge.data.replay import MarketRecord, ReplayDataset
from kalshi_edge.domain import (
    ForecastObservation,
    Market,
    MarketStatus,
    OrderBook,
    PriceLevel,
    PriceRange,
    SettlementResult,
)
from kalshi_edge.models.weather import prob_yes

SYNTHETIC_LABEL = "SYNTHETIC — engine validation, NOT evidence of real-market edge"

D = Decimal
_PRICE: list[Decimal] = [Decimal(i).scaleb(-3) for i in range(1001)]  # mils -> dollars


@dataclass(frozen=True)
class ScenarioSpec:
    name: str
    favorite_shift: float  # added to the favourite side's price (negative = underpriced)
    description: str


SCENARIOS: dict[str, ScenarioSpec] = {
    "efficient": ScenarioSpec(
        "efficient", 0.0, "market price == calibrated probability (+spread, noise)"),
    "favorite_underpriced": ScenarioSpec(
        "favorite_underpriced", -0.08,
        "favourites systematically underpriced by up to 8pp: an accurate model can find it"),
    "favorite_underpriced_mild": ScenarioSpec(
        "favorite_underpriced_mild", -0.03,
        "favourites underpriced by up to 3pp: below C's hurdle (q_safe haircut + slippage + "
        "min edge), so C should abstain"),
    "favorite_overpriced": ScenarioSpec(
        "favorite_overpriced", 0.04,
        "favourite-longshot bias against the naive buyer: favourites overpriced by up to 4pp"),
}


@dataclass(frozen=True)
class SyntheticConfig:
    seed: int = 20261012
    n_events: int = 600
    start: str = "2025-01-01"
    days: int = 360
    scenario: str = "favorite_underpriced"
    n_cities: int = 8
    econ_fraction: float = 0.15
    # information process
    horizon_hours: int = 46  # open -> close
    info_interval_hours: int = 3
    var_exponent: float = 1.3
    weather_sigma_open: float = 3.5  # F, uncertainty at open
    weather_sd_floor: float = 0.7
    econ_sigma_open: float = 0.25
    econ_sd_floor: float = 0.05
    market_reaction_minutes: int = 5
    # forecast provider
    forecast_noise_f: float = 0.3
    forecast_bias_f: float = 0.0
    forecast_sd_scale: float = 1.0
    forecast_lag_minutes: int = 20
    # market maker
    belief_noise_logit: float = 0.08
    book_minutes: int = 60
    tapered_fraction: float = 0.25
    depth_median_weather: float = 90.0
    depth_median_econ: float = 45.0
    void_rate: float = 0.01
    favorite_shift: float | None = None  # overrides the scenario preset when set

    def shift(self) -> float:
        if self.favorite_shift is not None:
            return self.favorite_shift
        return SCENARIOS[self.scenario].favorite_shift

    def validate(self) -> None:
        if self.scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {self.scenario!r}; have {sorted(SCENARIOS)}")
        if self.reaction_lt_lag() is False:
            raise ValueError("forecast_lag_minutes must exceed market_reaction_minutes")

    def reaction_lt_lag(self) -> bool:
        return self.market_reaction_minutes < self.forecast_lag_minutes


_CITIES: tuple[tuple[str, str, str, float, float], ...] = (
    # series, station, name, base F, seasonal amplitude F
    ("KXHIGHNY", "KNYC", "NYC", 62.0, 22.0),
    ("KXHIGHCHI", "KMDW", "Chicago", 58.0, 24.0),
    ("KXHIGHMIA", "KMIA", "Miami", 80.0, 8.0),
    ("KXHIGHAUS", "KAUS", "Austin", 78.0, 16.0),
    ("KXHIGHDEN", "KDEN", "Denver", 62.0, 20.0),
    ("KXHIGHLAX", "KLAX", "Los Angeles", 72.0, 7.0),
    ("KXHIGHPHIL", "KPHL", "Philadelphia", 63.0, 22.0),
    ("KXHIGHBOS", "KBOS", "Boston", 58.0, 22.0),
)
_ECON = (("KXCPI", "CPI m/m"), ("KXJOBS", "payrolls"), ("KXGDP", "GDP q/q"),
         ("KXPCE", "PCE y/y"))
_MON = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")

_STD_RANGES = (PriceRange(D("0.01"), D("0.99"), D("0.01")),)
_TAPERED_RANGES = (PriceRange(D("0.001"), D("0.100"), D("0.001")),
                   PriceRange(D("0.100"), D("0.900"), D("0.010")),
                   PriceRange(D("0.900"), D("0.999"), D("0.001")))
_SPREAD_CENTS = (1, 2, 3, 4, 6)
_SPREAD_CUM = (0.30, 0.60, 0.80, 0.92, 1.0)


# ------------------------------------------------------------------------------ grid helpers
def _snap_mils(mils: float, tapered: bool) -> int:
    """Nearest valid grid price (in mils = 1/1000 dollar)."""
    if not tapered:
        return min(990, max(10, round(mils / 10.0) * 10))
    if mils <= 100:
        return max(1, round(mils))
    if mils < 900:
        return round(mils / 10.0) * 10
    return min(999, round(mils))


def apply_bias(p_yes: float, shift: float) -> float:
    """Add ``shift`` to the *favourite* side's price (tapering to 0 at p = 0.5)."""
    if shift == 0.0:
        return p_yes
    fav_yes = p_yes >= 0.5
    pf = p_yes if fav_yes else 1.0 - p_yes
    taper = min(1.0, max(0.0, (pf - 0.5) / 0.3))
    pf2 = min(0.995, max(0.005, pf + shift * taper))
    return pf2 if fav_yes else 1.0 - pf2


# ------------------------------------------------------------------------------ forecasts
class SyntheticForecastProvider:
    """ExternalForecastProvider with a hard ``collected_at <= as_of`` filter."""

    source = "synthetic-nwp"

    def __init__(self) -> None:
        self._obs: dict[str, list[ForecastObservation]] = {}
        self._collected: dict[str, list[datetime]] = {}
        self.max_collected_returned: datetime | None = None

    def add(self, obs: ForecastObservation) -> None:
        self._obs.setdefault(obs.key, []).append(obs)

    def finalize(self) -> None:
        for key, lst in self._obs.items():
            lst.sort(key=lambda o: (o.collected_at, o.revision))
            self._collected[key] = [o.collected_at for o in lst]

    def observations(self, key: str, as_of: datetime) -> Sequence[ForecastObservation]:
        lst = self._obs.get(key)
        if not lst:
            return []
        n = bisect.bisect_right(self._collected[key], as_of)
        return lst[:n]

    @property
    def n_observations(self) -> int:
        return sum(len(v) for v in self._obs.values())


# ------------------------------------------------------------------------------ world
@dataclass(slots=True)
class _Gen:
    idx: int
    tapered: bool
    open_ts: datetime
    close_ts: datetime
    belief: list[float]  # P(YES | info_k) for k = 0..K-1
    depth_median: float


class SyntheticWorld(ReplayDataset):
    """Generated universe; see module docstring."""

    def __init__(self, cfg: SyntheticConfig) -> None:
        super().__init__(kind="synthetic", scenario=cfg.scenario)
        self.cfg = cfg
        self._gen: dict[str, _Gen] = {}
        self._shift = cfg.shift()
        self._build()

    # -------------------------------------------------------------------- construction
    def _build(self) -> None:
        cfg = self.cfg
        cfg.validate()
        start_d = date.fromisoformat(cfg.start)
        self.start = datetime(start_d.year, start_d.month, start_d.day, tzinfo=UTC)
        self._start: datetime = self.start
        self.end = self.start + timedelta(days=cfg.days)
        master = random.Random(cfg.seed)
        n_econ = round(cfg.n_events * cfg.econ_fraction)
        n_weather = cfg.n_events - n_econ
        cities = _CITIES[: cfg.n_cities]
        first_day, last_day = 2, cfg.days - 1  # event dates (open >= start, close < end)
        day_choices = list(range(first_day, last_day))
        combos = [(c, d) for d in day_choices for c in range(len(cities))]
        master.shuffle(combos)
        weather_slots = sorted(combos[: min(n_weather, len(combos))],
                               key=lambda cd: (cd[1], cd[0]))
        econ_slots: list[tuple[int, int]] = []
        seen: set[tuple[int, int]] = set()
        guard = 0
        while len(econ_slots) < n_econ and guard < 50 * max(1, n_econ):
            guard += 1
            slot = (master.randrange(len(_ECON)), master.choice(day_choices))
            if slot not in seen:
                seen.add(slot)
                econ_slots.append(slot)
        econ_slots.sort(key=lambda cd: (cd[1], cd[0]))

        provider = SyntheticForecastProvider()
        records: list[MarketRecord] = []
        for n, (ci, day) in enumerate(weather_slots):
            records.extend(self._weather_event(n, cities[ci], day, provider))
        for n, (ei, day) in enumerate(econ_slots):
            records.extend(self._econ_event(10_000_000 + n, _ECON[ei], day))
        provider.finalize()
        records.sort(key=lambda r: (r.market.open_time, r.market.ticker))
        self.records = records
        self.forecasts = provider
        self.station_map = {s: st for s, st, *_ in cities}
        n_events = len({r.event_key for r in records})
        self.provenance = {
            "label": SYNTHETIC_LABEL,
            "generator": "kalshi_edge.data.synthetic.SyntheticWorld",
            "config": dict(vars(cfg)),
            "scenario": cfg.scenario,
            "scenario_description": SCENARIOS[cfg.scenario].description,
            "favorite_shift": self._shift,
            "n_events": n_events,
            "n_markets": len(records),
            "n_forecast_observations": provider.n_observations,
            "voided_events": len({r.event_key for r in records
                                  if r.result is SettlementResult.VOID}),
        }

    def _info_path(self, rng: random.Random, mu: float, sigma_open: float, sd_floor: float
                   ) -> tuple[list[float], list[float], float]:
        cfg = self.cfg
        k_n = cfg.horizon_hours // cfg.info_interval_hours + 1
        v: list[float] = []
        for k in range(k_n):
            h = cfg.horizon_hours - k * cfg.info_interval_hours
            frac = h / cfg.horizon_hours
            v.append(sd_floor**2 + (sigma_open**2 - sd_floor**2) * frac**cfg.var_exponent)
        means = [mu]
        for k in range(k_n - 1):
            means.append(means[-1] + rng.gauss(0.0, math.sqrt(max(0.0, v[k] - v[k + 1]))))
        t_true = means[-1] + rng.gauss(0.0, math.sqrt(v[-1]))
        return means, v, t_true

    def _weather_event(self, n: int, city: tuple[str, str, str, float, float], day: int,
                       provider: SyntheticForecastProvider) -> list[MarketRecord]:
        cfg = self.cfg
        series, station, name, base, amp = city
        rng = random.Random(cfg.seed * 7_919 + n)
        d = (self._start + timedelta(days=day)).date()
        doy = d.timetuple().tm_yday
        mu = base + amp * math.sin(2.0 * math.pi * (doy - 110) / 365.0) + rng.gauss(0.0, 2.5)
        means, v, t_true = self._info_path(rng, mu, cfg.weather_sigma_open, cfg.weather_sd_floor)
        close = datetime(d.year, d.month, d.day, 23, tzinfo=UTC)
        open_ts = close - timedelta(hours=cfg.horizon_hours)
        result_ts = close + timedelta(hours=9 + rng.random() * 2.0)
        event_ticker = f"{series}-{d.year % 100:02d}{_MON[d.month - 1]}{d.day:02d}"
        voided = rng.random() < cfg.void_rate
        t_int = round(t_true)
        # forecasts
        key = f"{station}:max_temp_f:{d.isoformat()}"
        for k, (m_k, v_k) in enumerate(zip(means, v, strict=True)):
            issued = open_ts + timedelta(hours=k * cfg.info_interval_hours)
            fm = m_k + cfg.forecast_bias_f + rng.gauss(0.0, cfg.forecast_noise_f)
            fsd = max(0.3, math.sqrt(v_k) * cfg.forecast_sd_scale)
            provider.add(ForecastObservation(
                source=provider.source, key=key, issued_at=issued,
                collected_at=issued + timedelta(minutes=cfg.forecast_lag_minutes),
                valid_for=close, values={"mean": D(f"{fm:.2f}"), "sd": D(f"{fsd:.2f}")},
                revision=0, license="synthetic"))
        c = round(mu)
        pool: list[tuple[str, int, int]] = [
            ("greater", c - 6, 0), ("greater", c - 3, 0), ("greater", c, 0),
            ("greater", c + 3, 0), ("greater", c + 6, 0), ("less", c - 4, 0),
            ("less", c + 4, 0), ("between", c - 2, c - 1), ("between", c + 1, c + 2)]
        chosen = rng.sample(pool, 6)
        chosen.sort(key=lambda x: (x[0], x[1]))
        out: list[MarketRecord] = []
        for stype, lo, hi in chosen:
            if stype == "greater":
                suffix, strike = f"T{lo}", {"strike_type": "greater", "floor_strike": D(lo)}
                yes = t_int > lo
                title = f"{name} high above {lo}F?"
            elif stype == "less":
                suffix, strike = f"U{lo}", {"strike_type": "less", "cap_strike": D(lo)}
                yes = t_int < lo
                title = f"{name} high below {lo}F?"
            else:
                suffix = f"B{lo}.5"
                strike = {"strike_type": "between", "floor_strike": D(lo), "cap_strike": D(hi)}
                yes = lo <= t_int <= hi
                title = f"{name} high {lo}-{hi}F?"
            strike["event_date"] = d.isoformat()
            ticker = f"{event_ticker}-{suffix}"
            beliefs = [self._belief(strike, m_k, math.sqrt(v_k), True)
                       for m_k, v_k in zip(means, v, strict=True)]
            result = (SettlementResult.VOID if voided
                      else SettlementResult.YES if yes else SettlementResult.NO)
            out.append(self._record(ticker, event_ticker, series, "weather", title, open_ts,
                                    close, result_ts, result, strike, beliefs,
                                    cfg.depth_median_weather, rng, d))
        return out

    def _econ_event(self, n: int, ind: tuple[str, str], day: int) -> list[MarketRecord]:
        cfg = self.cfg
        series, label = ind
        rng = random.Random(cfg.seed * 7_919 + n)
        d = (self._start + timedelta(days=day)).date()
        mu = round(3.0 + rng.gauss(0.0, 0.4), 1)
        means, v, t_true = self._info_path(rng, mu, cfg.econ_sigma_open, cfg.econ_sd_floor)
        close = datetime(d.year, d.month, d.day, 12, tzinfo=UTC)
        open_ts = close - timedelta(hours=cfg.horizon_hours)
        result_ts = close + timedelta(hours=1)
        event_ticker = f"{series}-{d.year % 100:02d}{_MON[d.month - 1]}{d.day:02d}"
        voided = rng.random() < cfg.void_rate
        out: list[MarketRecord] = []
        for off in (-0.2, 0.0, 0.2):
            lo = round(mu + off, 1)
            strike: dict[str, Any] = {"strike_type": "greater", "floor_strike": D(str(lo)),
                                      "event_date": d.isoformat()}
            beliefs = [self._belief(strike, m_k, math.sqrt(v_k), False)
                       for m_k, v_k in zip(means, v, strict=True)]
            yes = t_true > lo
            result = (SettlementResult.VOID if voided
                      else SettlementResult.YES if yes else SettlementResult.NO)
            out.append(self._record(f"{event_ticker}-T{lo}", event_ticker, series, "economics",
                                    f"{label} above {lo}?", open_ts, close, result_ts, result,
                                    strike, beliefs, cfg.depth_median_econ, rng, d))
        return out

    @staticmethod
    def _belief(strike: dict[str, Any], mean: float, sd: float, integer_valued: bool) -> float:
        p = prob_yes(strike, mean, sd, integer_valued=integer_valued)
        assert p is not None
        return min(1.0 - 1e-6, max(1e-6, p))

    def _record(self, ticker: str, event_ticker: str, series: str, category: str, title: str,
                open_ts: datetime, close: datetime, result_ts: datetime,
                result: SettlementResult, strike: dict[str, Any], beliefs: list[float],
                depth_median: float, rng: random.Random, d: date) -> MarketRecord:
        tapered = rng.random() < self.cfg.tapered_fraction
        market = Market(
            ticker=ticker, event_ticker=event_ticker, series_ticker=series, category=category,
            title=title, status=MarketStatus.OPEN, market_type="binary", open_time=open_ts,
            close_time=close, price_ranges=_TAPERED_RANGES if tapered else _STD_RANGES,
            fee_multiplier=D(1), quantity_step=D(1), expected_expiration_time=result_ts,
            rules_primary=f"SYNTHETIC rules for {title}", rules_verified=True,
            can_close_early=False, strike=strike, result=None, settlement_ts=None,
            source="synthetic", schema_version="synthetic-v1")
        self._gen[ticker] = _Gen(len(self._gen), tapered, open_ts, close, beliefs, depth_median)
        return MarketRecord(market, result, result_ts, event_ticker, category,
                            (close - open_ts).total_seconds() / 3600.0)

    # -------------------------------------------------------------------------- books
    def _content_index(self, g: _Gen, t: datetime) -> int:
        step = self.cfg.book_minutes * 60.0
        j = int((t - g.open_ts).total_seconds() // step)
        jmax = int(((g.close_ts - g.open_ts).total_seconds() - 1.0) // step)
        return min(max(j, 0), jmax)

    def _belief_index(self, g: _Gen, content_ts: datetime) -> int:
        cfg = self.cfg
        secs = (content_ts - g.open_ts).total_seconds() - cfg.market_reaction_minutes * 60.0
        k = int(secs // (cfg.info_interval_hours * 3600.0))
        return min(max(k, 0), len(g.belief) - 1)

    def _top(self, g: _Gen, j: int) -> tuple[random.Random, int, int]:
        """Hour-``j`` top of book: (rng, yes_bid_mils, no_bid_mils). Deterministic."""
        cfg = self.cfg
        rng = random.Random((cfg.seed * 1_000_003 + g.idx) * 1_000_033 + j)
        content_ts = g.open_ts + timedelta(minutes=j * cfg.book_minutes)
        b = g.belief[self._belief_index(g, content_ts)]
        lg = math.log(b / (1.0 - b)) + rng.gauss(0.0, cfg.belief_noise_logit)
        p = apply_bias(1.0 / (1.0 + math.exp(-lg)), self._shift)
        m = min(0.996, max(0.004, p)) * 1000.0
        u = rng.random()
        spread = _SPREAD_CENTS[bisect.bisect_left(_SPREAD_CUM, u)]
        width = spread * 10  # mils: spreads are in whole cents on every grid
        lo, hi = (1, 999) if g.tapered else (10, 990)
        yb = max(lo, min(hi, _snap_mils(m - width / 2.0, g.tapered)))
        ya = min(hi, _snap_mils(yb + width, g.tapered))
        if ya <= yb:
            ya = min(hi, yb + 10)
            if ya <= yb:
                yb = ya - 10
        return rng, yb, 1000 - ya

    def book_at(self, ticker: str, t: datetime) -> OrderBook | None:
        g = self._gen.get(ticker)
        if g is None or t < g.open_ts:
            return None
        j = self._content_index(g, t)
        rng, yb, nb = self._top(g, j)
        lo = 1 if g.tapered else 10
        levels: list[list[PriceLevel]] = []
        for top in (yb, nb):
            n_lv = 1 + int(rng.random() * 4)
            q0 = max(5, int(rng.lognormvariate(math.log(g.depth_median), 0.7)))
            side: list[PriceLevel] = []
            price = top
            for i in range(n_lv):
                if price < lo:
                    break
                qty = q0 if i == 0 else max(3, int(q0 * rng.uniform(0.4, 1.6)))
                side.append(PriceLevel(_PRICE[price], D(qty)))
                price -= 10
            side.reverse()  # ascending, best bid last
            levels.append(side)
        return OrderBook(ticker, tuple(levels[0]), tuple(levels[1]), ts=t, received_ts=t,
                         source="synthetic")

    def max_best_ask(self, ticker: str, t: datetime) -> Decimal | None:
        g = self._gen.get(ticker)
        if g is None or t < g.open_ts:
            return None
        _, yb, nb = self._top(g, self._content_index(g, t))
        return _PRICE[max(1000 - nb, 1000 - yb)]

    def decision_times(self, interval: timedelta) -> list[datetime]:
        assert self.start is not None and self.end is not None
        out: list[datetime] = []
        t = self.start
        while t < self.end:
            out.append(t)
            t += interval
        return out

    # -------------------------------------------------------------------------- stats
    def true_probability(self, ticker: str, t: datetime) -> float:
        """The fair (unbiased, noise-free) P(YES) a perfectly informed-so-far agent holds at t."""
        g = self._gen[ticker]
        return g.belief[self._belief_index(g, g.open_ts + timedelta(
            minutes=self._content_index(g, t) * self.cfg.book_minutes))]


def make_world(cfg: SyntheticConfig, **overrides: Any) -> SyntheticWorld:
    return SyntheticWorld(replace(cfg, **overrides) if overrides else cfg)


@dataclass(frozen=True)
class WorldKey:
    """Hashable identity of a generated world (for caches)."""

    items: tuple[tuple[str, Any], ...] = field(default_factory=tuple)

    @staticmethod
    def of(cfg: SyntheticConfig) -> WorldKey:
        return WorldKey(tuple(sorted(vars(cfg).items())))
