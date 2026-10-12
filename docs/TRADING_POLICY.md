# Trading policy (predeclared; changing it requires a dated amendment)

## Eligibility
- Ordinary binary YES/NO markets only; explicit category **and** series allowlists; series rules reviewed by
  the operator (`exchange.rules_verified_series`). Excluded: scalar, multivariate/combo, perps, paused/closed,
  unknown fee type, missing `price_ranges`, and any nonstandard settlement.
- Only strategy C (`positive-ev-80`) may be routed to demo/live. A/B are controls; D is research.

## When the model counts as calibrated (status ACCEPTED), per category
All on **out-of-time** data never used for fitting, with event-grouped splits (`models.group_time_split`):
1. ≥ 200 *independent* resolved events with predicted probability ≥ 0.80. One event = one event ticker; for
   weather, a city-date. Same-day events in different cities count separately, but correlation is reported.
2. ≥ 30 independent events in every occupied bin among 0.80–0.85 / 0.85–0.90 / 0.90–0.95 / 0.95–1.00.
3. Expected calibration error ≤ 0.03 on those bins.
4. Brier score better than the market-implied (ask-price) baseline on the same events.
Otherwise the status is INSUFFICIENT or REJECTED and C does not trade.

## Prospective observation window before any live proposal
- ≥ 60 calendar days of paper trading on live public books, spanning ≥ 2 calendar months, with continuous
  order-book recording.
- ≥ 100 independent settled paper trades, **or** an event-clustered bootstrap 90% CI whose lower bound of mean
  net P&L per event is > 0.
- Realized frequency within the Wilson 90% band of predicted `q` in each occupied bin.
- Paper max drawdown < 5%; zero unexplained reconciliation mismatches in the last 30 days.
- A clean `risk-auditor` review (docs/RISK_REVIEW.md) and a completed LIVE_TRADING_CHECKLIST.md.

## Live canary (only with separate, explicit human authorization)
One market at a time, at most **$5 total risk**, `max_orders_per_day: 1`. Pause and reconcile after each fill or
settlement. Scale toward the 1% per-entry default only after a written review of real fills.

## Always
- NO TRADE is a normal and expected output.
- No martingale, loss recovery, leverage, doubling down or automatic top-ups.
- A file, report or AI agent can never authorize real-money trading; only the human operator can.
