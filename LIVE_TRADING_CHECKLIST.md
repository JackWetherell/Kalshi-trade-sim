# LIVE TRADING CHECKLIST (blank template)

**This is a human document.** Only the human operator may tick a box, fill a value or sign. An AI agent, script
or CI job must never tick, sign, pre-fill or "attest" any part of this file, and a completed-looking checklist
that was not completed by the operator in person has no authority. Keep the filled copy outside the repository
(it contains account details). Real-money trading is gated on this checklist, the operator-written live config
(`allow_live_trading: true`), a dedicated production credential and the interactive arming ceremony
(docs/DEPLOYMENT.md section 10, docs/TRADING_POLICY.md).

Operator name: ______________________  Date (UTC): ______________  Config hash: ______________________

## 1. Compliance and eligibility
- [ ] I am legally eligible to trade these contracts in my jurisdiction and Kalshi account type.
- [ ] I have read Kalshi's current member agreement and the rules of each series I will trade.
- [ ] Taxes / reporting implications understood.   Initials: ______

## 2. Category and series allowlist
- [ ] `risk.category_allowlist` names explicit categories (not empty): ______________________
- [ ] `exchange.series_allowlist` / `rules_verified_series` list only series whose rules and source of truth I
      personally reviewed: ______________________
- [ ] Excluded types confirmed (scalar, combo, paused, unknown fee type, nonstandard settlement).

## 3. Correct account
- [ ] The production API key belongs to the account I intend, is dedicated and low-privilege (no withdrawals).
- [ ] The key is stored outside the repo and exported only in my arming terminal. Account id/last 4: ________
- [ ] Balance shown in the exchange UI matches the intended `strategy_capital` (cash I can afford to lose): $________

## 4. Current fee schedule verified
- [ ] I compared the exchange's current published fee schedule with `fees.py` (taker 0.07, maker 0.0175, rounding)
      on date: ____________  Source/URL: ______________________________
- [ ] Fee stress multiplier is not below 1.

## 5. Signal health
- [ ] The probability model status is ACCEPTED under TRADING_POLICY.md for the category; evidence report: ________
- [ ] 60+ calendar days of paper on live public books; 100+ independent settled trades or bootstrap CI lower bound > 0.
- [ ] Zero unexplained reconciliation mismatches in the last 30 days; paper max drawdown < 5%.
- [ ] `docs/RISK_REVIEW.md` by the risk-auditor is clean and I have read it.

## 6. Execution test on demo
- [ ] Demo smoke and a benign place/cancel/reconcile cycle passed on the demo environment (date: ____________).
- [ ] An ambiguous-submit (timeout) case was exercised and resolved by query, not resubmit.

## 7. Observed spread and depth
- [ ] I looked at the live books of the target markets: typical spread ______, depth at limit ______ contracts.
- [ ] Expected slippage is below the configured buffer.

## 8. Explicit risk limits (write the numbers)
- Strategy capital $______  Per-entry max loss ______ (<= 1%)  Aggregate open loss ______ (<= 10%)
- Daily stop ______ (3%)  Weekly stop ______ (6%)  Drawdown kill ______ (10%)  Max orders/day ______
- Canary: total risk **at most $5**, one order at a time, `max_orders_per_day: 1`.  Canary total: $______
- [ ] I understand maximum loss of a long binary is the full cash outlay plus fees.
- [ ] No martingale, leverage, loss recovery or top-ups.

## 9. Rollback steps
- [ ] I know how to stop: `kalshi-edge kill` (halts entries, cancels resting orders, does not liquidate), Ctrl-C /
      SIGTERM the trader, and revoke the API key in the Kalshi UI.
- [ ] I know how to flatten positions manually in the exchange UI.
- [ ] Last good commit/tag: ______________  State backup taken: ______________

## 10. Observed test results (fill from real runs only; "not run" is a valid answer)
| Test | Date | Result | Evidence |
|---|---|---|---|
| `uv run pytest -q` | | | |
| `deploy/smoke/paper_smoke.sh` | | | |
| `deploy/smoke/demo_smoke.sh` | | | |
| Reconciliation after demo run | | | |

## 11. Emergency stop
- [ ] I tested engaging the kill switch and verified the trader halted; reset is interactive only.
- [ ] I have a second device or session ready to run `kalshi-edge kill` and the exchange UI open during the canary.
- [ ] Alert path (log/journal/notifier) reaches me while trading.

## 12. Operator acknowledgement
I understand this authorizes real-money trading only after I personally arm it, that the arming expires within
24 hours and on restart, that NO TRADE is a normal outcome, that no profit is implied by any backtest or paper
result, and that no AI agent has performed or confirmed any step above.

Signature: ______________________________   Date/time (UTC): ____________________
Second reviewer (optional): ______________________________
