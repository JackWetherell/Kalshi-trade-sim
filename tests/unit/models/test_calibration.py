import math
import random
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pytest

from kalshi_edge.domain import CalibrationStatus
from kalshi_edge.models import (
    AcceptanceCriteria,
    IsotonicCalibrator,
    PlattCalibrator,
    acceptance,
    build_report,
    group_time_split,
    pav,
    wilson_lower,
)

D = Decimal


def test_pav_known_example() -> None:
    blocks = pav([1, 3, 2, 4, 3, 5], [1] * 6)
    assert blocks == [(0, 1, 1.0), (1, 3, 2.5), (3, 5, 3.5), (5, 6, 5.0)]
    # weighted pooling
    assert pav([2.0, 1.0], [1.0, 3.0]) == [(0, 2, 1.25)]


def test_isotonic_monotone_and_pools() -> None:
    preds = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6]
    ys = [0, 1, 0, 1, 1, 1]
    groups = list(range(6))
    cal = IsotonicCalibrator().fit(preds, ys, groups)
    qs = [cal.transform(D(str(p)))[0] for p in (0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.99)]
    assert qs == sorted(qs)
    assert cal.transform(D("0.2"))[0] == cal.transform(D("0.3"))[0] == D("0.5")
    assert cal.transform(D("0.5"))[0] == D(1)
    assert cal.n_events == 6


def test_isotonic_q_safe_conservative_and_group_aware() -> None:
    n = 200
    preds = [0.95] * n
    ys = [1] * n
    cal = IsotonicCalibrator().fit(preds, ys, list(range(n)))
    q, qs = cal.transform(D("0.95"))
    assert q == D(1)
    expected = wilson_lower(200, 200) - 0.01
    assert abs(float(qs) - expected) < 1e-5
    # same 200 contracts but only 10 distinct events -> much weaker bound
    cal2 = IsotonicCalibrator().fit(preds, ys, [i % 10 for i in range(n)])
    assert cal2.transform(D("0.95"))[1] < qs
    assert cal2.transform(D("0.95"))[1] <= q


def test_wilson_bound_values() -> None:
    assert wilson_lower(100, 100) == pytest.approx(1 / (1 + 1.645**2 / 100))
    assert wilson_lower(0, 0) == 0.0
    assert wilson_lower(0, 10) == 0.0
    assert wilson_lower(9, 10) < wilson_lower(90, 100) < 0.9
    assert wilson_lower(95, 100) > wilson_lower(19, 20)


def test_platt_recovers_params() -> None:
    rng = np.random.default_rng(7)
    a, b = 1.5, -0.3
    p = rng.uniform(0.05, 0.95, 40000)
    z = a * np.log(p / (1 - p)) + b
    y = (rng.uniform(size=p.size) < 1 / (1 + np.exp(-z))).astype(int)
    cal = PlattCalibrator().fit(list(p), [int(v) for v in y], list(range(p.size)))
    assert cal.a == pytest.approx(a, abs=0.08)
    assert cal.b == pytest.approx(b, abs=0.08)
    q, qs = cal.transform(D("0.90"))
    assert 0 <= qs <= q <= 1
    assert float(q) == pytest.approx(1 / (1 + math.exp(-(a * math.log(9) + b))), abs=0.01)


def test_platt_empty_bin_gives_zero_q_safe() -> None:
    cal = PlattCalibrator().fit([0.5, 0.6], [1, 0], [1, 2])
    assert cal.transform(D("0.99"))[1] == 0


def test_report_and_acceptance() -> None:
    rng = random.Random(3)
    preds, ys, groups, base = [], [], [], []
    for i in range(1200):
        p = rng.uniform(0.80, 0.99)
        preds.append(p)
        ys.append(1 if rng.random() < p else 0)
        groups.append(i)
        base.append(0.5)  # a bad baseline
    rep = build_report(preds, ys, groups, base)
    assert rep.n_events_high == 1200
    assert [(b.lo, b.hi) for b in rep.high_bins] == [(0.8, 0.85), (0.85, 0.9), (0.9, 0.95),
                                                      (0.95, 1.0)]
    assert sum(b.n for b in rep.high_bins) == 1200
    assert rep.ece < 0.05 and 0 < rep.brier < 0.25 and rep.log_loss > 0
    assert acceptance(rep, AcceptanceCriteria(max_ece=0.1)) is CalibrationStatus.ACCEPTED
    assert acceptance(rep, AcceptanceCriteria(max_ece=1e-6)) is CalibrationStatus.REJECTED
    # baseline better than model => rejected
    rep2 = build_report(preds, ys, groups, ys)
    assert acceptance(rep2, AcceptanceCriteria(max_ece=0.1)) is CalibrationStatus.REJECTED
    # no baseline => cannot beat it
    assert acceptance(build_report(preds, ys, groups),
                      AcceptanceCriteria(max_ece=0.1)) is CalibrationStatus.REJECTED


def test_acceptance_insufficient_events() -> None:
    preds = [0.9] * 100
    rep = build_report(preds, [1] * 100, list(range(100)), [0.5] * 100)
    assert acceptance(rep) is CalibrationStatus.INSUFFICIENT
    # many samples but few independent events
    rep2 = build_report([0.9] * 1000, [1] * 1000, [i % 20 for i in range(1000)], [0.5] * 1000)
    assert rep2.n_events_high == 20
    assert acceptance(rep2) is CalibrationStatus.INSUFFICIENT


def test_group_time_split_is_leak_free() -> None:
    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    recs = [(f"e{g}", t0 + timedelta(days=d)) for g, d in
            [(1, 1), (1, 2), (2, 3), (2, 9), (3, 8), (3, 10), (4, 5), (5, 6)]]
    s = group_time_split(recs, lambda r: r[1], lambda r: r[0], t0 + timedelta(days=5),
                         t0 + timedelta(days=8))
    tr, te = {r[0] for r in s.train}, {r[0] for r in s.test}
    assert tr == {"e1"} and te == {"e3"} and not (tr & te)
    assert {r[0] for r in s.excluded} == {"e2", "e4", "e5"}
    assert all(r[1] < t0 + timedelta(days=5) for r in s.train)
    assert all(r[1] >= t0 + timedelta(days=8) for r in s.test)
    with pytest.raises(ValueError):
        group_time_split(recs, lambda r: r[1], lambda r: r[0], t0 + timedelta(days=9), t0)
