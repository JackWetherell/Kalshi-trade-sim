# Test report (test-engineer, independent verification)

Generated from a real run on 2026-10-12 (Python 3.13 in the build container). Nothing here was
estimated; numbers come from the commands at the bottom. No test touches the network: an
autouse fixture in `tests/conftest.py` makes `socket.connect`, `create_connection` and
`getaddrinfo` raise, so any outbound attempt hard-fails the test. Kalshi hosts are never
contacted; production order endpoints are only exercised through `httpx.MockTransport`
recorders that assert zero requests reached the wire.

## Counts

Full suite (`uv run pytest`): **512 passed, 24 xfailed (strict), 0 failed**, ~90 s with coverage.
One transient failure was seen in a single coverage run while other agents were editing
`src/` concurrently; it did not reproduce in 5 subsequent full runs (3 with coverage) and was not identified.

| Area (owned by test-engineer) | Path | passed | xfailed |
|---|---|---|---|
| Domain / books / EV / fees / pricing / config | `tests/unit/core` | 101 | 15 |
| Risk manager, exposure, stops, kill switch | `tests/unit/risk` | 94 | 5 |
| Store, engine, PaperVenue, reconcile | `tests/unit/execution` | 39 | 1 |
| Arming, token, lock, clock | `tests/unit/runtime` | 39 | 0 |
| Hypothesis properties | `tests/property` | 12 | 0 |
| Failure injection / isolation / malicious input | `tests/chaos` | 64 | 3 |
| Integration (loop, CLI, factory, logging) | `tests/integration/test_int_*.py` | 50 | 0 |

Other agents' suites in the same run: exchange 46, strategies 21, models 18, backtest/deploy
smoke tests (not owned or counted here). `xfail(strict=True)` markers are known bugs: they
turn into hard failures (XPASS) the moment the bug is fixed, so the lead must delete the
marker when fixing.

## Coverage (`--cov=kalshi_edge`, whole suite): TOTAL 88%

Safety-critical modules (all suites combined):

| Module | Cover | Notes |
|---|---|---|
| `risk/manager.py`, `risk/state.py`, `risk/killswitch.py` | 100% | |
| `risk/exposure.py` | 98% | line 76 (zero-quantity position skip) |
| `runtime/arming.py`, `runtime/lock.py`, `runtime/clock.py` | 100% | |
| `runtime/loop.py` | 99% | |
| `runtime/factory.py` | 84% | DEMO/LIVE real-venue construction branches (need creds/network) |
| `execution/paper.py` | 100% | |
| `execution/engine.py` / `reconcile.py` | 99% / 98% | |
| `execution/store.py` | 92% | `tx()` context manager and an unused query |
| `fees.py`, `pricing.py` | 100% | |
| `config.py` | 99% | |
| `domain.py` | 98% | |
| `exchange/hosts.py` | 96% | |
| `exchange/venues.py` | 95% | |
| `exchange/rest.py` / `ws.py` / `schema.py` | 84% / 85% / 75% | owned by the api engineer's suite |
| `monitoring/*` | 100% | |
| `cli.py` | 65% | `backtest`, `report`, `data sync/record`, `doctor --network` untested here |
| `data/recorder.py` | 0% | not covered by any suite |
| `backtest/report.py` | 21% | other agent's area |

## Bugs found (most important first)

Each has a strict-xfail reproducer test. File:line are from the code as read on 2026-10-12.

1. **`allow_live_trading` accepts truthy strings and ints in a live config** (HIGH; the
   second operator gate). `config.py:173` `_bool_strict` is a default (after-mode)
   `field_validator`, so pydantic has already coerced `"true"`, `"yes"`, `"on"`, `1`, `"1"` to
   `True` before the strictness check runs. `AppConfig.model_validate({"mode":"live",
   "exchange":{"environment":"prod"},"allow_live_trading":"true"})` validates.
   Fix: `mode="before"` validator or `StrictBool`.
   Tests: `tests/unit/core/test_core_config.py::test_allow_live_trading_must_be_a_literal_boolean[*]` (6 params).
2. **Risk manager never re-checks the >0.80 screen against the fresh book** (MEDIUM).
   `risk/manager.py:147-151` compares only `proposal.expected_price` and `limit_price` with
   `min_price_exclusive`. A proposal with limit/expected 0.85 is approved when the fresh best
   ask is 0.70 (and edge is computed from the real 0.70 vwap), so a strategy bug or a book
   that fell between discovery and submit buys outside the "executable price strictly > 0.80"
   rule (instruction.md section 5). Fix: also require `best_ask > floor` (and probably
   `vwap > floor`) on the fresh book.
   Test: `tests/unit/risk/test_risk_manager.py::test_executable_price_on_fresh_book_must_still_exceed_floor`.
3. **`reconcile()` silently adopts fills with no local order** (MEDIUM). `execution/reconcile.py`
   flags untracked *open* venue orders but not a filled/terminal order the store has never
   heard of (second process, lost DB) or a fill that arrives for a locally `REJECTED`/retired
   order after the unknown-order grace window. Both reconcile `ok=True` and trading continues;
   the `unmatched_fill` / `account_mismatch` alert kinds in `monitoring/alerts.py:16` are never
   raised. Section 9.6 says refuse to trade on out-of-sync orders.
   Tests: `tests/chaos/test_chaos_reconcile_gaps.py::test_fill_for_unknown_order_must_fail_reconcile`,
   `::test_late_acceptance_of_retired_order_must_be_flagged`.
4. **`FeeConfig` has no validation** (MEDIUM). `config.py:94`. `taker_rate`/`maker_rate` can be
   zero or negative (negative fees inflate EV and the edge check), `stress_multiplier` 0 zeroes
   fees, `rounding_increment <= 0` is accepted and only explodes later inside `fee()`. A YAML
   edit therefore weakens the fee model without touching reviewed code.
   Tests: `test_core_config.py::test_fee_config_rejects_nonsensical_rates[*]`,
   `::test_fee_config_rejects_zero_rounding_increment`.
5. **Check-weakening values have no hard ceiling** (MEDIUM/LOW). `config.py:52` `live_canary_total_risk`
   is unbounded (instruction.md 9.10 says at most $5), and `fee_reserve`, `min_depth_notional`
   can be negative, `max_book_age_seconds` / `max_forecast_age_seconds` / `min_seconds_to_close`
   / `max_clock_skew_seconds` / `max_spread` have no bounds.
   Tests: `::test_live_canary_total_risk_has_a_hard_ceiling`,
   `::test_risk_limits_reject_check_weakening_values[*]`.
6. **`KillSwitch.state()` crashes on valid non-object JSON** (LOW; still fails closed because
   the caller raises, but `status`/`kill` CLI and the loop get an `AttributeError` instead of
   `engaged=True`). `risk/killswitch.py:46`: `data.get` on a list/null/number.
   Tests: `tests/unit/risk/test_risk_state_killswitch.py::test_non_object_json_is_still_engaged[*]`.
7. **`PaperVenue.fees_paid` leaks on an atomically refused fill** (LOW, bookkeeping only).
   `execution/paper.py:138` adds to `fees_paid` before the `cost > cash` check at line 139 and
   never rolls back. Test: `tests/unit/execution/test_exec_store_engine_paper.py::test_refused_fill_does_not_leak_into_fees_paid`.
8. **`ArmingToken.max_total_risk` is not enforced** (LOW). The amount the operator retypes
   during arming is displayed but the live risk cap comes only from config
   (`risk/manager.py` canary cap). If config allows more than what was confirmed, the larger
   number wins. Test: `tests/chaos/test_chaos_reconcile_gaps.py::test_live_risk_is_bounded_by_the_amount_confirmed_at_arming`.

### Observations (no failing test; judgement calls for the lead)

* Conservative equity mark values a position at zero when no book exists
  (`runtime/loop.py` `mark_equity`). A transient missing book (market closed, feed gap) on a
  held position can trip the daily/weekly stop and the **latched drawdown kill** spuriously.
* There is no supported way to clear a latched `weekly_halt` / `drawdown_kill` in the
  persisted `risk_state`; `kill --reset` only removes the file and the loop still halts with
  `loss_stop` (documented by `test_latched_flags_survive_kill_reset_and_restart`).
* `TradingLoop.step()` lets exceptions from the strategy, `md.get_orderbook`, or a
  non-ambiguous venue error (e.g. `RateLimited`/`TransportFailure` on POST) propagate. It fails
  closed (no trade, order stays SUBMITTING and blocks trading until resolved or retired) and
  the CLI catches it, but the loop itself is not resilient. Tested as "raises, no trade".
* `ExecutableQuote.cash_outlay` uses `vwap * qty`, which carries Decimal-28 division error
  (1e-27 scale); harmless but not exact.
* The WS/REST suites are the api engineer's; I only added cross-checks (seq gap halts the
  loop's decisions; GET 429/5xx backoff vs POST never retried; POST timeout then query by
  client id and no second POST).

## What is covered (instruction.md section 10 mapping)

* EV identity `q - p - f`, break-even, win/loss asymmetry, YES vs NO purchase: verified by
  buying and settling in PaperVenue both ways (`test_core_books_ev.py`).
* Bid to ask conversion, empty / one-sided / crossed / locked books, depth-weighted VWAP,
  fractional quantities, sub-cent and tapered grids, snapping, off-grid, missing
  `price_ranges` fail closed (`test_core_*`, `test_prop_invariants.py`).
* Fees: Kalshi example 100 @ 0.55 = $1.74, round up, maker vs taker, multiplier and stress,
  per-fill rounding >= aggregate (property, 100 tiny fills = $2.00 vs $1.74).
* Sizing by risk dollars (not contracts), near $1 prices, caps only shrink: manager unit tests
  and Hypothesis invariant over random books/caps/exposure.
* `q_safe == breakeven` no trade, missing estimate no trade, `p <= 0.80` no trade for A/B/C
  and every mode, A/B/D never live-eligible and rejected in DEMO/LIVE, C needs independent +
  ACCEPTED calibration, live canary cap.
* Exposure: same event (both legs, no hedge netting), same category+date, resting orders,
  pending/unknown orders, positions; per-entry / aggregate / cluster / cash caps each shown
  binding.
* Stops trip exactly at equality (daily 3%, weekly 6%, drawdown 10%), latch, persist across
  restart; kill switch beats every strategy and mode, survives a new instance, fails closed
  when unreadable, cancels resting orders without liquidating.
* Config ceilings, unknown keys, live/prod consistency, shipped YAMLs load unarmed.
* Execution: ambiguous POST timeout leads to UNKNOWN, query before retry, grace retirement,
  no duplicate on restart (new Store/Loop on same DB), `DuplicateIntent`, duplicate/reordered
  fills deduped, insufficient buying power, fee overrun refused atomically, partial fills,
  cancel races, untracked venue order blocks trading, position mismatch blocks trading.
* Isolation: paper lifecycle makes zero HTTP calls with prod env vars set; AST checks that
  `paper.py` and strategy/risk/models/execution never import HTTP or the live venue; guard
  blocks POST/DELETE/PUT/PATCH and any `/portfolio` on prod and look-alike hosts before the
  transport; `LiveExecutionVenue` refuses no/expired/wrong-pid/wrong-host token, wrong
  environment, missing creds, and re-checks on every mutation; `build_runtime(live)` refuses
  without flag/token; demo venue ignores prod credentials and refuses a prod URL; CLI
  `run --mode live` cannot arm non-interactively; arming refuses under CI/CLAUDECODE/pytest
  markers, non-TTY, flag false, challenge/risk mismatch; token expiry, other pid, over-24h;
  token never referenced by storage code; single-instance lock (same process and across processes).
* Malicious strategies through `TradingLoop`: huge quantity, limit 1.00, off-grid, negative,
  zero, wrong ticker, wrong side estimate, fake independence, 200-proposal flood (capped by
  order count and 5% cluster), raising strategy.
* End to end: three markets, wins and losses, settlement, and the identity
  `cash_end = cash_start - sum(cost) - sum(fees) + sum(payouts)`; also a Hypothesis version
  over random IOC/FOK sequences.

## Known gaps (not tested; do not read as passing)

* No test against the real Kalshi API or demo (no network, no credentials); schema shapes
  remain "live-unverified" per `docs/ADR_exchange.md`.
* `DemoExecutionVenue` happy paths beyond what the exchange suite and my mocked flows do;
  `runtime/factory.py` real DEMO/LIVE venue construction branches.
* GTC resting-order fills are not simulated by PaperVenue, so cancel races against a fill of
  a resting order are only tested at the "already terminal" level.
* Settlement correction / VOID handling beyond PaperVenue VOID arithmetic; external data
  outage beyond estimator returning None or raising.
* WebSocket reconnect with real sockets (only logic-level `BookBuilder` and a scripted
  stream); clock skew is an injected number, not a real NTP check.
* `cli.py` `backtest`, `report`, `data sync`, `data record`, `doctor --network`; `data/recorder.py` (0%).
* The arming "ceremony" is exercised with scripted input and fake TTY objects only, never a
  real terminal (by design: an agent must not perform the real ceremony).
* Process-level crash injection (SIGKILL mid-submit) is simulated by dropping the Store/Loop
  objects, not by killing a process.
* mypy is configured for `src` only; tests are not type-checked. Ruff is clean on all files
  I own; remaining `ruff check .` findings are in `src/kalshi_edge/backtest` and
  `data/synthetic.py` (other agents).

## Exact commands

```bash
env -u UV_NATIVE_TLS uv run pytest -q                                    # everything
env -u UV_NATIVE_TLS uv run pytest -q --cov=kalshi_edge --cov-report=term-missing
env -u UV_NATIVE_TLS uv run pytest -q tests/unit/core tests/unit/risk tests/unit/execution \
    tests/unit/runtime tests/property tests/chaos tests/integration/test_int_*.py   # my suites
env -u UV_NATIVE_TLS uv run pytest -q -rx -o addopts=""                  # list known-bug xfails
env -u UV_NATIVE_TLS uv run pytest -q --runxfail <file>                  # see the raw failures
env -u UV_NATIVE_TLS uv run ruff check tests/
```

Shared builders live in `tests/support.py`; the network guard, sqlite cleanup and other autouse
fixtures live in `tests/conftest.py`.
