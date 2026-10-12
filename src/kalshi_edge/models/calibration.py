"""Calibrators (isotonic/PAV, Platt), q_safe bounds, reports and acceptance criteria.

Calibrators are fit ONLY on the samples passed in; the caller must pass strictly-past, resolved
samples (see ``group_time_split``). Floats are used only inside the statistics; every public
output of ``transform`` is a ``Decimal`` in [0, 1] quantized to 1e-6.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from collections.abc import Callable, Hashable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol, runtime_checkable

import numpy as np

from kalshi_edge.domain import CalibrationStatus

_Q = Decimal("1e-6")
DEFAULT_Z = 1.645
DEFAULT_HAIRCUT = 0.01
Number = float | Decimal


def to_decimal(x: float) -> Decimal:
    """Float probability -> Decimal in [0,1] quantized to 1e-6 (model boundary only)."""
    x = min(1.0, max(0.0, x))
    return Decimal(repr(x)).quantize(_Q)


def wilson_lower(k: float, n: float, z: float = DEFAULT_Z) -> float:
    """Wilson score lower bound for ``k`` successes of ``n`` trials (0 when n == 0)."""
    if n <= 0:
        return 0.0
    phat = min(1.0, max(0.0, k / n))
    z2 = z * z
    denom = 1.0 + z2 / n
    centre = phat + z2 / (2.0 * n)
    margin = z * math.sqrt(phat * (1.0 - phat) / n + z2 / (4.0 * n * n))
    return max(0.0, (centre - margin) / denom)


def group_success_stats(outcomes: Sequence[int], groups: Sequence[Hashable],
                        idx: Sequence[int]) -> tuple[int, float]:
    """(n_eff, k): distinct groups among ``idx`` and the sum of per-group mean outcomes.

    Several contracts of one event are one independent observation, not many.
    """
    acc: dict[Hashable, list[int]] = defaultdict(list)
    for i in idx:
        acc[groups[i]].append(outcomes[i])
    k = sum(sum(v) / len(v) for v in acc.values())
    return len(acc), k


def q_safe_from(q_cal: float, k: float, n_eff: float, z: float, haircut: float) -> float:
    """``max(0, min(q_cal, Wilson_lower(k, n_eff, z)) - haircut)``."""
    return max(0.0, min(q_cal, wilson_lower(k, n_eff, z)) - haircut)


@runtime_checkable
class Calibrator(Protocol):
    status: CalibrationStatus
    n_events: int

    def fit(self, preds: Sequence[Number], outcomes: Sequence[int],
            groups: Sequence[Hashable]) -> Calibrator: ...

    def transform(self, p: Decimal) -> tuple[Decimal, Decimal]:
        """Return ``(q, q_safe)`` for a raw model probability ``p`` of the purchased outcome."""
        ...


def _validate(preds: Sequence[Number], outcomes: Sequence[int], groups: Sequence[Hashable]
              ) -> tuple[np.ndarray, list[int]]:
    if not (len(preds) == len(outcomes) == len(groups)):
        raise ValueError("preds, outcomes and groups must have equal length")
    if len(preds) == 0:
        raise ValueError("no samples")
    ys = [int(o) for o in outcomes]
    if any(y not in (0, 1) for y in ys):
        raise ValueError("outcomes must be 0/1")
    return np.array([float(p) for p in preds], dtype=float), ys


@dataclass(slots=True)
class _Block:
    lo: float  # smallest raw prediction in block
    hi: float
    value: float  # pooled mean outcome
    n_eff: int
    k: float


def pav(values: Sequence[float], weights: Sequence[float]) -> list[tuple[int, int, float]]:
    """Pool-adjacent-violators on a sequence already ordered by x.

    Returns blocks as (start_index, end_index_exclusive, pooled_mean), means nondecreasing.
    """
    blocks: list[list[float]] = []  # [weighted_sum, weight, start, end]
    for i, (v, w) in enumerate(zip(values, weights, strict=True)):
        blocks.append([v * w, w, i, i + 1])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] >= blocks[-1][0] / blocks[-1][1]:
            top = blocks.pop()
            blocks[-1][0] += top[0]
            blocks[-1][1] += top[1]
            blocks[-1][3] = top[3]
    return [(int(b[2]), int(b[3]), b[0] / b[1]) for b in blocks]


class IsotonicCalibrator:
    """Monotone step-function calibrator (PAV).

    ``transform(p)``: take the block containing ``p`` (the rightmost block whose smallest
    training prediction is <= p; the first block if p is below all). ``q`` = pooled block
    frequency. ``q_safe = max(0, min(q, Wilson_lower(k, n_eff, z)) - haircut)`` where
    ``n_eff`` = distinct groups (events) in the block and ``k`` = sum over those groups of the
    group's mean outcome.
    """

    def __init__(self, z: float = DEFAULT_Z, haircut: float = DEFAULT_HAIRCUT) -> None:
        self.z = z
        self.haircut = haircut
        self.status = CalibrationStatus.INSUFFICIENT
        self.n_events = 0
        self._blocks: list[_Block] = []
        self._los: list[float] = []

    def fit(self, preds: Sequence[Number], outcomes: Sequence[int],
            groups: Sequence[Hashable]) -> IsotonicCalibrator:
        x, ys = _validate(preds, outcomes, groups)
        order = np.argsort(x, kind="stable")
        # collapse equal predictions into one point so ties never straddle blocks
        pts: list[tuple[float, list[int]]] = []
        for i in order:
            xi = float(x[i])
            if pts and pts[-1][0] == xi:
                pts[-1][1].append(int(i))
            else:
                pts.append((xi, [int(i)]))
        vals = [sum(ys[j] for j in m) / len(m) for _, m in pts]
        wts = [float(len(m)) for _, m in pts]
        blocks: list[_Block] = []
        for s, e, mean in pav(vals, wts):
            members = [j for _, m in pts[s:e] for j in m]
            n_eff, k = group_success_stats(ys, groups, members)
            blocks.append(_Block(pts[s][0], pts[e - 1][0], mean, n_eff, k))
        self._blocks = blocks
        self._los = [b.lo for b in blocks]
        self.n_events = len(set(groups))
        return self

    @property
    def blocks(self) -> tuple[_Block, ...]:
        return tuple(self._blocks)

    def _block_for(self, p: float) -> _Block:
        if not self._blocks:
            raise RuntimeError("calibrator is not fitted")
        i = bisect_right(self._los, p) - 1
        return self._blocks[max(0, i)]

    def transform(self, p: Decimal) -> tuple[Decimal, Decimal]:
        b = self._block_for(float(p))
        qs = q_safe_from(b.value, b.k, b.n_eff, self.z, self.haircut)
        return to_decimal(b.value), min(to_decimal(qs), to_decimal(b.value))


def _logit(p: np.ndarray) -> np.ndarray:
    p = np.clip(p, 1e-6, 1 - 1e-6)
    out: np.ndarray = np.log(p / (1 - p))
    return out


class PlattCalibrator:
    """Logistic calibration ``q = sigmoid(a * logit(p) + b)`` fit by IRLS/Newton.

    ``q_safe`` uses the training samples whose raw prediction lies within ``+/- window/2`` of
    ``p`` as the "bin": ``max(0, min(q, Wilson_lower(k, n_eff, z)) - haircut)`` with the same
    group aggregation as the isotonic calibrator. An empty bin gives ``q_safe = 0``.
    """

    def __init__(self, z: float = DEFAULT_Z, haircut: float = DEFAULT_HAIRCUT,
                 window: float = 0.05, ridge: float = 1e-8) -> None:
        self.z = z
        self.haircut = haircut
        self.window = window
        self.ridge = ridge
        self.status = CalibrationStatus.INSUFFICIENT
        self.n_events = 0
        self.a = 1.0
        self.b = 0.0
        self._x = np.array([], dtype=float)
        self._ys: list[int] = []
        self._groups: list[Hashable] = []

    def fit(self, preds: Sequence[Number], outcomes: Sequence[int],
            groups: Sequence[Hashable]) -> PlattCalibrator:
        x, ys = _validate(preds, outcomes, groups)
        feat = np.column_stack([_logit(x), np.ones_like(x)])
        y = np.array(ys, dtype=float)
        w = np.zeros(2)
        eye = np.eye(2) * self.ridge
        for _ in range(100):
            mu = 1.0 / (1.0 + np.exp(-(feat @ w)))
            weight = mu * (1.0 - mu) + 1e-12
            grad = feat.T @ (y - mu) - self.ridge * w
            hess = feat.T @ (feat * weight[:, None]) + eye
            step = np.linalg.solve(hess, grad)
            w = w + step
            if float(np.max(np.abs(step))) < 1e-10:
                break
        self.a, self.b = float(w[0]), float(w[1])
        self._x = x
        self._ys = ys
        self._groups = list(groups)
        self.n_events = len(set(groups))
        return self

    def q_raw(self, p: float) -> float:
        z = self.a * float(_logit(np.array([p]))[0]) + self.b
        return 1.0 / (1.0 + math.exp(-z))

    def transform(self, p: Decimal) -> tuple[Decimal, Decimal]:
        pf = float(p)
        q = self.q_raw(pf)
        half = self.window / 2.0
        idx = [int(i) for i in np.nonzero(np.abs(self._x - pf) <= half + 1e-12)[0]]
        if idx:
            n_eff, k = group_success_stats(self._ys, self._groups, idx)
            qs = q_safe_from(q, k, n_eff, self.z, self.haircut)
        else:
            qs = 0.0
        qd = to_decimal(q)
        return qd, min(to_decimal(qs), qd)


# --------------------------------------------------------------------------------------------
# Reports and acceptance


HIGH_EDGES = (0.80, 0.85, 0.90, 0.95, 1.0)
FULL_EDGES = tuple(i / 10 for i in range(11))


@dataclass(frozen=True, slots=True)
class ReliabilityBin:
    lo: float
    hi: float
    n: int  # samples
    n_events: int  # distinct independent groups
    mean_pred: float
    freq: float


@dataclass(frozen=True, slots=True)
class CalibrationReport:
    n: int
    n_events: int
    brier: float
    log_loss: float
    ece: float  # over full-range bins
    ece_high: float  # over the 0.80-1.00 bins only
    high_bins: tuple[ReliabilityBin, ...]
    full_bins: tuple[ReliabilityBin, ...]
    n_events_high: int  # distinct groups with at least one prediction >= 0.80
    baseline_brier: float | None = None


@dataclass(frozen=True, slots=True)
class AcceptanceCriteria:
    """Predeclared before looking at test results."""

    min_events_high_total: int = 200
    min_events_per_occupied_bin: int = 30
    max_ece: float = 0.03
    require_beat_baseline_brier: bool = True


def brier_score(preds: Sequence[float], ys: Sequence[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(preds, ys, strict=True)) / len(preds)


def log_loss(preds: Sequence[float], ys: Sequence[int], eps: float = 1e-12) -> float:
    tot = 0.0
    for p, y in zip(preds, ys, strict=True):
        p = min(1 - eps, max(eps, p))
        tot -= math.log(p) if y else math.log(1 - p)
    return tot / len(preds)


def _bins(preds: Sequence[float], ys: Sequence[int], groups: Sequence[Hashable],
          edges: Sequence[float]) -> tuple[ReliabilityBin, ...]:
    out: list[ReliabilityBin] = []
    last = len(edges) - 2
    for j in range(len(edges) - 1):
        lo, hi = edges[j], edges[j + 1]
        idx = [i for i, p in enumerate(preds)
               if (lo <= p < hi) or (j == last and p == hi)]
        if idx:
            n = len(idx)
            out.append(ReliabilityBin(lo, hi, n, len({groups[i] for i in idx}),
                                      sum(preds[i] for i in idx) / n,
                                      sum(ys[i] for i in idx) / n))
        else:
            out.append(ReliabilityBin(lo, hi, 0, 0, 0.0, 0.0))
    return tuple(out)


def _ece(bins: Sequence[ReliabilityBin]) -> float:
    total = sum(b.n for b in bins)
    if total == 0:
        return 0.0
    return sum(b.n * abs(b.mean_pred - b.freq) for b in bins) / total


def build_report(preds: Sequence[Number], outcomes: Sequence[int], groups: Sequence[Hashable],
                 baseline_preds: Sequence[Number] | None = None) -> CalibrationReport:
    """Out-of-sample calibration report. ``baseline_preds`` = market-implied on the same rows."""
    if not (len(preds) == len(outcomes) == len(groups)) or len(preds) == 0:
        raise ValueError("preds, outcomes and groups must be equal-length and non-empty")
    ps = [float(p) for p in preds]
    ys = [int(o) for o in outcomes]
    high = _bins(ps, ys, groups, HIGH_EDGES)
    full = _bins(ps, ys, groups, FULL_EDGES)
    hi_idx = [i for i, p in enumerate(ps) if p >= HIGH_EDGES[0]]
    base = None
    if baseline_preds is not None:
        if len(baseline_preds) != len(ps):
            raise ValueError("baseline_preds length mismatch")
        base = brier_score([float(b) for b in baseline_preds], ys)
    return CalibrationReport(
        n=len(ps), n_events=len(set(groups)), brier=brier_score(ps, ys),
        log_loss=log_loss(ps, ys), ece=_ece(full), ece_high=_ece(high), high_bins=high,
        full_bins=full, n_events_high=len({groups[i] for i in hi_idx}), baseline_brier=base)


def acceptance(report: CalibrationReport, criteria: AcceptanceCriteria | None = None
               ) -> CalibrationStatus:
    """INSUFFICIENT if too few independent events; REJECTED if ECE/Brier criteria fail."""
    c = criteria or AcceptanceCriteria()
    if report.n_events_high < c.min_events_high_total:
        return CalibrationStatus.INSUFFICIENT
    for b in report.high_bins:
        if 0 < b.n_events < c.min_events_per_occupied_bin:
            return CalibrationStatus.INSUFFICIENT
    if report.ece > c.max_ece or report.ece_high > c.max_ece:
        return CalibrationStatus.REJECTED
    if c.require_beat_baseline_brier and (
            report.baseline_brier is None or report.brier >= report.baseline_brier):
        return CalibrationStatus.REJECTED
    return CalibrationStatus.ACCEPTED


# --------------------------------------------------------------------------------------------
# Leakage-safe splitting


@dataclass(frozen=True, slots=True)
class GroupSplit[R]:
    train: list[R] = field(default_factory=list)
    test: list[R] = field(default_factory=list)
    excluded: list[R] = field(default_factory=list)


def group_time_split[R](records: Sequence[R], time_key: Callable[[R], object],
                        group_key: Callable[[R], Hashable], train_end: object,
                        test_start: object) -> GroupSplit[R]:
    """Out-of-time, group-disjoint split.

    A group is TRAIN only if every record's time is < ``train_end``; TEST only if every
    record's time is >= ``test_start``; any other group (straddling, or inside the embargo gap)
    is excluded from both. ``train_end <= test_start`` is required. Time keys should be the
    moment the label became known (settlement time) for training rows.
    """
    if train_end > test_start:  # type: ignore[operator]
        raise ValueError("train_end must be <= test_start")
    by_group: dict[Hashable, list[R]] = defaultdict(list)
    for r in records:
        by_group[group_key(r)].append(r)
    split: GroupSplit[R] = GroupSplit()
    for recs in by_group.values():
        times = [time_key(r) for r in recs]
        if all(t < train_end for t in times):  # type: ignore[operator]
            split.train.extend(recs)
        elif all(t >= test_start for t in times):  # type: ignore[operator]
            split.test.extend(recs)
        else:
            split.excluded.extend(recs)
    return split
