# Probability models, calibration and q_safe

Code: `src/kalshi_edge/models/`. Strategies consume `ProbabilityEstimate` only through
`DecisionContext.estimates`; C/D trade only if `estimate.usable_for_live`
(independent AND `CalibrationStatus.ACCEPTED`). Prices below 0.80 or above `max_price` are
filtered before any model is consulted. NO TRADE is the normal outcome.

## Estimators
* **MarketImpliedEstimator** (baseline): `q = q_safe = best ask` (or mid). `independent=False`,
  status `NONE`: never tradeable; negative after fees by construction.
* **WeatherThresholdEstimator**: forecast key `f"{station}:max_temp_f:{YYYY-MM-DD}"`, where
  `station = station_map[market.series_ticker]` and the date is `strike["event_date"]` else the
  date of `expected_expiration_time`. Uses the latest observation with `collected_at <= as_of`
  (ties: higher revision); older than `max_forecast_age` or missing => `None`.
  Raw `P(YES)` is a normal CDF (`math.erf`) with continuity correction 0.5 for integer daily highs:
  `greater` `1-F(k+.5)`, `greater_or_equal` `1-F(k-.5)`, `less` `F(k-.5)`,
  `less_or_equal` `F(k+.5)`, `between` (inclusive) `F(cap+.5)-F(floor-.5)`.
  NO = `1 - YES`. Floats exist only inside the model; outputs are Decimals quantized to 1e-6.
  No calibrator => status `INSUFFICIENT`, `q_safe = 0`. With a calibrator, `q, q_safe =
  calibrator.transform(raw_p_of_purchased_outcome)` and the status is the calibrator's `status`
  (set to `ACCEPTED` only by the caller after `acceptance(...)` on out-of-sample data).
  The calibrator must be fit on samples oriented to the purchased outcome.

## q_safe (exact definition)
For raw prediction `p`, take its calibration bin and compute
`q_safe = max(0, min(q_cal, Wilson_lower(k, n_eff, z)) - haircut)` with defaults `z = 1.645`
(one-sided ~95%) and `haircut = 0.01`, where
* isotonic: bin = the PAV block containing `p` (rightmost block whose smallest training
  prediction is <= p), `q_cal` = pooled block frequency;
* Platt: `q_cal = sigmoid(a*logit(p)+b)` (IRLS fit); bin = training samples within +-0.025 of
  `p` (empty bin => `q_safe = 0`);
* `n_eff` = number of distinct groups (events) in the bin; `k` = sum over those groups of the
  group's mean outcome. Many contracts of one event count once.
* `Wilson_lower(k,n) = (phat + z^2/2n - z*sqrt(phat(1-phat)/n + z^2/4n^2)) / (1 + z^2/n)`.
This is a heuristic conservative bound, not a guarantee: it ignores model drift and selection.

## Splits and acceptance
`group_time_split(records, time_key, group_key, train_end, test_start)`: a group is train only if
all its times are `< train_end`, test only if all are `>= test_start`, otherwise excluded (also
the embargo gap). Use label-known (settlement) time for training rows. Calibrators are fit only
on what they are handed.

`build_report` gives Brier, log loss, ECE (10 equal bins and the 0.80-1.00 bins), reliability
tables for 0.80-0.85, 0.85-0.90, 0.90-0.95, 0.95-1.00 plus full range, and independent-event
counts. `acceptance(report, AcceptanceCriteria())` (predeclared defaults):
* `INSUFFICIENT` if fewer than 200 independent events with prediction >= 0.80, or any occupied
  high bin has fewer than 30 independent events;
* `REJECTED` if ECE (full or high) > 0.03, or Brier is not strictly better than the
  market-implied baseline on the same rows (a missing baseline also rejects);
* else `ACCEPTED`. Compute the report on held-out out-of-time data only.

## Sizing summary (strategies/sizing.py)
Budgets are risk dollars = all-in cash outlay (walked VWAP*qty + per-fill rounded fees).
`contracts_for_budget` rounds down to the quantity step. B: `min + clip((p-.80)/.20,0,1)*(max-min)`.
C/D Kelly: `equity * kelly_fraction * max(0,(q_safe-c)/(1-c))`, `c = p + f + slippage`.
Trade rule: `q_safe > p+f` strictly AND `q_safe-p-f-slippage >= min_edge`, re-checked on the
executable VWAP/fees of the actual quantity.
