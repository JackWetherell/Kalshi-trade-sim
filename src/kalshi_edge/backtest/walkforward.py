"""Walk-forward calibration of the raw weather model, leakage-safe by construction.

At each calendar refit time ``R`` (weekly by default):

1. only events whose result became known strictly before ``R`` (``result_ts + settlement
   delay < R``) are visible;
2. ``models.group_time_split`` separates them, by *event* and by label-known time, into
   TRAIN (resolved before ``R - fold_days``) and a recent VALIDATION fold (resolved at least
   ``embargo_days`` after the train cut-off, so every training label was known before any
   validation prediction was made);
3. an ``IsotonicCalibrator`` is fit on TRAIN only (raw weather probability of the purchased
   outcome vs outcome, grouped by event);
4. ``models.build_report`` + ``models.acceptance`` (predeclared default criteria) are
   evaluated on the VALIDATION fold against the market-implied baseline (best ask of the
   same outcome at the same time). Only if they return ACCEPTED is the calibrator's status set
   to ACCEPTED; otherwise it is left INSUFFICIENT/REJECTED and strategies C/D cannot trade.

The calibrator that is deployed is exactly the one that was validated (train-only fit).
No data after ``R`` is used. The final ``holdout_fraction`` of the calendar is only a
*reporting* split: refits inside it still use strictly past data, and no hyper-parameter is
tuned on it.
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import math
import pickle
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np

from kalshi_edge.backtest.options import BacktestOptions
from kalshi_edge.data.replay import ReplayDataset
from kalshi_edge.domain import CalibrationStatus, Outcome, SettlementResult
from kalshi_edge.models import (
    AcceptanceCriteria,
    IsotonicCalibrator,
    WeatherThresholdEstimator,
    acceptance,
    build_report,
    group_time_split,
)

CACHE_VERSION = "wf-cache-3"
_MEM_CACHE: dict[str, WalkForwardResult] = {}


@dataclass(slots=True)
class CalibRows:
    label_ts: np.ndarray  # epoch seconds when the label became known
    sample_ts: np.ndarray  # epoch seconds of the prediction
    event_id: np.ndarray
    raw: np.ndarray  # raw model P(purchased outcome)
    y: np.ndarray  # 1 if purchased outcome occurred
    ask: np.ndarray  # market-implied q = best ask of the purchased outcome (nan if none)
    horizon_h: np.ndarray  # hours from sample to market close
    event_label_ts: np.ndarray  # per event id
    event_keys: list[str]

    @property
    def n(self) -> int:
        return int(self.raw.shape[0])


@dataclass(slots=True)
class WalkForwardResult:
    key: str
    refit_ts: list[datetime]
    calibrators: list[IsotonicCalibrator | None]
    diagnostics: list[dict[str, Any]]
    rows: CalibRows
    q_cal: np.ndarray  # calibrated point q using the calibrator in force at sample time
    accepted_at_sample: np.ndarray  # bool: ACCEPTED calibrator in force at sample time
    params: dict[str, Any] = field(default_factory=dict)


def _epoch(t: datetime) -> float:
    return t.timestamp()


def fast_q(cal: IsotonicCalibrator, raw: np.ndarray) -> np.ndarray:
    """Vectorised point-q of an isotonic calibrator (same block lookup as ``transform``)."""
    blocks = cal.blocks
    los = np.array([b.lo for b in blocks])
    vals = np.array([b.value for b in blocks])
    idx = np.searchsorted(los, raw, side="right") - 1
    out: np.ndarray = vals[np.maximum(idx, 0)]
    return out


def cache_key(ds_params: dict[str, Any], opts: BacktestOptions, *, quote_delay_s: float,
              extra_ticks: int, settle_delay_h: float, interval_min: int,
              max_forecast_age_s: float, min_seconds_to_close: float) -> str:
    payload = {
        "v": CACHE_VERSION, "ds": ds_params,
        "wf": [opts.refit_days, opts.fold_days, opts.embargo_days, opts.min_train_events,
               opts.calib_stride_hours, opts.model_version],
        "q": quote_delay_s, "x": extra_ticks, "s": settle_delay_h, "i": interval_min,
        "f": max_forecast_age_s, "c": min_seconds_to_close,
        "crit": [AcceptanceCriteria().min_events_high_total,
                 AcceptanceCriteria().min_events_per_occupied_bin, AcceptanceCriteria().max_ece],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:20]


def build_rows(ds: ReplayDataset, *, estimator: WeatherThresholdEstimator,
               quote_delay: timedelta, extra_ticks: int, settle_delay: timedelta,
               interval: timedelta, stride_hours: int, min_seconds_to_close: float
               ) -> CalibRows:
    """Resolved-event prediction samples on the decision grid (both purchased sides)."""
    from kalshi_edge.backtest.feed import widen_book

    assert ds.start is not None
    times = ds.decision_times(interval)
    start_epoch = _epoch(ds.start)
    sampled = [t for t in times
               if round((_epoch(t) - start_epoch) / 3600.0) % stride_hours == 0]
    ev_index: dict[str, int] = {}
    label_ts: list[float] = []
    sample_ts: list[float] = []
    event_id: list[int] = []
    raw: list[float] = []
    ys: list[int] = []
    ask: list[float] = []
    hor: list[float] = []
    ev_label: dict[int, float] = {}
    import bisect

    for rec in ds.records:
        if rec.result not in (SettlementResult.YES, SettlementResult.NO) or rec.result_ts is None:
            continue
        m = rec.market
        if estimator.forecast_key(m) is None:
            continue
        label = _epoch(rec.result_ts + settle_delay)
        eid = ev_index.setdefault(rec.event_key, len(ev_index))
        ev_label[eid] = label
        lo = bisect.bisect_left(sampled, m.open_time)
        hi = bisect.bisect_right(sampled, m.close_time - timedelta(seconds=min_seconds_to_close))
        y_yes = 1 if rec.result is SettlementResult.YES else 0
        for t in sampled[lo:hi]:
            est = estimator.estimate(m, Outcome.YES, t)
            if est is None:
                continue
            book = ds.book_at(m.ticker, t - quote_delay)
            if book is None:
                continue
            book = widen_book(book, m.price_ranges, extra_ticks)
            q_yes = est.q
            for side, q_side, y_side in ((Outcome.YES, q_yes, y_yes),
                                         (Outcome.NO, Decimal(1) - q_yes, 1 - y_yes)):
                lvl = book.best_ask(side)
                label_ts.append(label)
                sample_ts.append(_epoch(t))
                event_id.append(eid)
                raw.append(float(q_side))
                ys.append(y_side)
                ask.append(float(lvl.price) if lvl is not None else math.nan)
                hor.append((m.close_time - t).total_seconds() / 3600.0)
    n_ev = len(ev_index)
    ev_label_arr = np.full(n_ev, np.inf)
    for eid, lab in ev_label.items():
        ev_label_arr[eid] = lab
    return CalibRows(
        np.array(label_ts), np.array(sample_ts), np.array(event_id, dtype=np.int64),
        np.array(raw), np.array(ys, dtype=np.int8), np.array(ask), np.array(hor),
        ev_label_arr, list(ev_index))


def run_walk_forward(rows: CalibRows, ds: ReplayDataset, opts: BacktestOptions, *, key: str,
                     settle_delay: timedelta) -> WalkForwardResult:
    assert ds.start is not None and ds.end is not None
    refit_ts: list[datetime] = []
    cals: list[IsotonicCalibrator | None] = []
    diags: list[dict[str, Any]] = []
    t = ds.start + timedelta(days=14)
    criteria = AcceptanceCriteria()
    n_ev = len(rows.event_keys)
    events = list(range(n_ev))
    while t < ds.end:
        r_epoch = _epoch(t)
        resolved = [e for e in events if rows.event_label_ts[e] < r_epoch]
        val_start = r_epoch - opts.fold_days * 86400.0
        split = group_time_split(
            [(e, float(rows.event_label_ts[e])) for e in resolved],
            time_key=lambda rec: rec[1], group_key=lambda rec: rec[0],
            train_end=val_start, test_start=val_start + opts.embargo_days * 86400.0)
        train_ids = {rec[0] for rec in split.train}
        val_ids = {rec[0] for rec in split.test}
        diag: dict[str, Any] = {
            "refit_ts": t.isoformat(), "n_resolved_events": len(resolved),
            "n_train_events": len(train_ids), "n_val_events": len(val_ids)}
        if train_ids:
            diag["train_max_label_epoch"] = max(float(rows.event_label_ts[e]) for e in train_ids)
        if val_ids:
            vl = [float(rows.event_label_ts[e]) for e in val_ids]
            diag["val_min_label_epoch"], diag["val_max_label_epoch"] = min(vl), max(vl)
        diag["refit_epoch"] = r_epoch
        cal: IsotonicCalibrator | None = None
        status = CalibrationStatus.INSUFFICIENT
        if len(train_ids) >= opts.min_train_events and val_ids:
            tmask = np.zeros(n_ev, dtype=bool)
            tmask[list(train_ids)] = True
            vmask = np.zeros(n_ev, dtype=bool)
            vmask[list(val_ids)] = True
            tr = tmask[rows.event_id]
            cal = IsotonicCalibrator().fit(rows.raw[tr].tolist(), rows.y[tr].astype(int).tolist(),
                                           rows.event_id[tr].tolist())
            va = vmask[rows.event_id] & ~np.isnan(rows.ask)
            if int(va.sum()) > 0:
                q_va = fast_q(cal, rows.raw[va])
                rep = build_report(q_va.tolist(), rows.y[va].astype(int).tolist(),
                                   rows.event_id[va].tolist(),
                                   baseline_preds=rows.ask[va].tolist())
                status = acceptance(rep, criteria)
                diag.update({
                    "val_brier_model": rep.brier, "val_brier_market": rep.baseline_brier,
                    "val_ece": rep.ece, "val_ece_high": rep.ece_high,
                    "val_n_events_high": rep.n_events_high,
                    "val_high_bin_events": [b.n_events for b in rep.high_bins]})
            cal.status = status
            cal.n_events = len(train_ids)
        diag["status"] = status.value
        refit_ts.append(t)
        cals.append(cal)
        diags.append(diag)
        t += timedelta(days=opts.refit_days)

    q_cal = np.full(rows.n, np.nan)
    accepted = np.zeros(rows.n, dtype=bool)
    if refit_ts and rows.n:
        r_arr = np.array([_epoch(x) for x in refit_ts])
        idx = np.searchsorted(r_arr, rows.sample_ts, side="right") - 1
        for j, cal in enumerate(cals):
            if cal is None:
                continue
            sel = idx == j
            if sel.any():
                q_cal[sel] = fast_q(cal, rows.raw[sel])
                accepted[sel] = cal.status is CalibrationStatus.ACCEPTED
    return WalkForwardResult(key, refit_ts, cals, diags, rows, q_cal, accepted,
                             {"refit_days": opts.refit_days, "fold_days": opts.fold_days,
                              "embargo_days": opts.embargo_days,
                              "min_train_events": opts.min_train_events,
                              "calib_stride_hours": opts.calib_stride_hours,
                              "settle_delay_h": settle_delay.total_seconds() / 3600.0})


def get_walk_forward(ds: ReplayDataset, opts: BacktestOptions, *, key: str,
                     estimator: WeatherThresholdEstimator, quote_delay: timedelta,
                     extra_ticks: int, settle_delay: timedelta, interval: timedelta,
                     min_seconds_to_close: float) -> WalkForwardResult:
    if key in _MEM_CACHE:
        return _MEM_CACHE[key]
    path: Path | None = None
    if opts.cache_dir:
        path = Path(opts.cache_dir) / f"wf_{key}.pkl"
        if path.exists():
            try:
                with open(path, "rb") as fh:
                    res = pickle.load(fh)  # our own cache file, never external data
                _MEM_CACHE[key] = res
                return res  # type: ignore[no-any-return]
            except Exception:  # corrupt/partial cache => recompute
                pass
    rows = build_rows(ds, estimator=estimator, quote_delay=quote_delay, extra_ticks=extra_ticks,
                      settle_delay=settle_delay, interval=interval,
                      stride_hours=opts.calib_stride_hours,
                      min_seconds_to_close=min_seconds_to_close)
    res = run_walk_forward(rows, ds, opts, key=key, settle_delay=settle_delay)
    _MEM_CACHE[key] = res
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(f".tmp{id(res)}")
        with open(tmp, "wb") as fh:
            pickle.dump(res, fh)
        tmp.replace(path)
    return res


# --------------------------------------------------------------------------- evaluation
def _brier(p: np.ndarray, y: np.ndarray) -> float | None:
    return float(np.mean((p - y) ** 2)) if p.size else None


def _bins_to_dicts(bins: Sequence[Any]) -> list[dict[str, Any]]:
    return [{"lo": b.lo, "hi": b.hi, "n": b.n, "n_events": b.n_events,
             "mean_pred": b.mean_pred, "freq": b.freq} for b in bins]


def evaluate_estimator(wf: WalkForwardResult, holdout_start: datetime) -> dict[str, Any]:
    """Out-of-sample estimator quality, split in-sample-period vs untouched holdout.

    Predictions are the calibrated point-q in force at the sample time (walk-forward), scored
    against realised outcomes and against the market-implied ask on the same rows.
    """
    rows = wf.rows
    out: dict[str, Any] = {}
    h_epoch = _epoch(holdout_start)
    valid = ~np.isnan(wf.q_cal) & ~np.isnan(rows.ask)
    for name, mask in (("in_sample", valid & (rows.sample_ts < h_epoch)),
                       ("holdout", valid & (rows.sample_ts >= h_epoch)),
                       ("all", valid)):
        sub: dict[str, Any] = {"n_rows": int(mask.sum())}
        if mask.sum() == 0:
            out[name] = sub
            continue
        q, y, a, raw = wf.q_cal[mask], rows.y[mask].astype(float), rows.ask[mask], rows.raw[mask]
        ev = rows.event_id[mask]
        sub.update({
            "n_events": len(set(ev.tolist())),
            "brier_model_calibrated": _brier(q, y), "brier_model_raw": _brier(raw, y),
            "brier_market_implied_ask": _brier(a, y),
            "share_rows_accepted": float(wf.accepted_at_sample[mask].mean()),
        })
        hi = q >= 0.80
        if hi.any():
            sub["high_ge_0.80"] = {
                "n_rows": int(hi.sum()), "n_events": len(set(ev[hi].tolist())),
                "brier_model_calibrated": _brier(q[hi], y[hi]),
                "brier_market_implied_ask": _brier(a[hi], y[hi]),
                "mean_pred": float(q[hi].mean()), "freq": float(y[hi].mean()),
                "mean_market_ask": float(a[hi].mean())}
        rep = build_report(q.tolist(), y.astype(int).tolist(), ev.tolist(),
                           baseline_preds=a.tolist())
        sub["ece"] = rep.ece
        sub["ece_high"] = rep.ece_high
        sub["reliability_high"] = _bins_to_dicts(rep.high_bins)
        sub["reliability_full"] = _bins_to_dicts(rep.full_bins)
        # reliability restricted to rows where an ACCEPTED calibrator was in force
        acc = wf.accepted_at_sample[mask]
        if acc.any():
            rep2 = build_report(q[acc].tolist(), y[acc].astype(int).tolist(),
                                ev[acc].tolist(), baseline_preds=a[acc].tolist())
            sub["accepted_only"] = {
                "n_rows": int(acc.sum()), "n_events": rep2.n_events,
                "brier_model_calibrated": rep2.brier,
                "brier_market_implied_ask": rep2.baseline_brier,
                "ece_high": rep2.ece_high, "reliability_high": _bins_to_dicts(rep2.high_bins)}
        out[name] = sub
    return out
