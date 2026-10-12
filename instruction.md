# Kalshi High-Probability Trading Research & Automation System

**Instruction file for Claude Code**  
**Project:** `kalshi-edge-bot`  
**Prepared:** 2026-10-11  
**Initial reference bankroll:** **$1,009 USD** (operator-provided estimate; do not assume this is the live available balance)  
**Primary goal:** Research, build, test, backtest, paper-trade, and—only after explicit human authorization—deploy a tightly risk-limited automated trading service on Kalshi's **binary prediction markets**.

> **START HERE — IMPLEMENTATION DIRECTIVE FOR CLAUDE CODE**
>
> Read this file completely. Act as the lead engineer and orchestrator. Create specialized project subagents under `.claude/agents/`, delegate distinct implementation/review tasks, build the repository, run tests, and produce reproducible backtest reports. Do not stop at architecture or pseudocode if implementation is feasible. Work in phases and keep a running progress checklist in `docs/PROGRESS.md`. **Do not place real-money orders or arm production trading as part of implementing these instructions.** Live trading must remain technically disabled until the human operator separately approves it after reviewing evidence.

## 0. The hypothesis—and the essential correction

The original idea is:

1. Scan Kalshi for contracts trading at an **executable buy price greater than $0.80**.
2. Trade frequently, initially sizing wagers according to the distance above 80% (larger positions at higher advertised probabilities).
3. Capture many small settlement wins, accepting that an occasional loss may occur.

This is an **unproven trading hypothesis**, not a known source of steady profits. A contract's market price is not an independently verified probability. Buying at $0.90 with a true 90% success probability loses money after fees. A rare losing $0.95 contract can erase many $0.95 wins.

**Mandatory design change:** Implement the original strategy as an explicit *research baseline*; do **not** make it the default live strategy. The live-candidate strategy must trade only when an **independently generated, historically calibrated probability estimate** supports *positive expected value after realistic fees, spread, slippage, and model uncertainty*. It may choose **no trades at all**.

### Strategy variants (all required for comparison)

- **A / naive-80:** Buy eligible YES or NO contracts with executable price `p > 0.80`, subject to common execution/risk rules; constant notional per trade. **Backtest-only control.**
- **B / probability-scaled-80:** Same selection, with desired risk increasing as `p` moves from `0.80` to `1.00`; cap aggressively. E.g. normalized score `clip((p - 0.80)/0.20, 0, 1)`, then interpolate between preset minimum and maximum *risk budgets*, rounding down to whole contracts. **Backtest-only control.** An above-80 price is **not** evidence of edge.
- **C / positive-EV-80:** Consider `p > 0.80` only when conservative independently estimated win probability `q_safe` exceeds the true break-even probability by a configurable margin. **Only strategy eligible for eventual live trading.**
- **D / ablation:** C without the 80% threshold, for research only; determine whether 80% adds any benefit after controlling for edge, fees, liquidity, and resolution time.

All strategies must be compared on **the same timestamps, market universe, information availability, fees, and execution assumptions**.

### Mathematics to implement and test

For one ordinary binary contract held to an unambiguous $1/$0 settlement:

- `p` = executable acquisition price in dollars per contract (NOT midpoint, last trade, or UI probability).
- `f` = estimated all-in fees **per contract** for the actual order quantity, including order-specific rounding and, where relevant, forecast exit cost.
- `q` = calibrated estimate of the probability that the purchased outcome pays $1.
- `EV_per_contract = q - p - f` (when using hold-to-settlement economics with binary $1/$0 payouts).
- `breakeven_q = p + f`.
- `edge = q_safe - p - f - slippage_buffer_per_contract` (do not double count a slippage estimate already embedded in `p`).
- Expected dollars on `n` contracts: `n * (q - p) - fees_total(n) - modeled_slippage_total(n)`.
- **Maximum stake at risk** for an outright binary buy is the **total acquisition cash outlay plus applicable fees**, not just the potential winnings.

Payout exceptions, voids, corrections, nonstandard contracts, partial unwinds, and other market-specific settlement mechanics need explicit handling; otherwise exclude those markets. Use `decimal.Decimal` or fixed-point units for prices, quantities, cash, and fees—not binary floating point for accounting. Kalshi currently exposes sub-cent price grids and fractional quantities; read the current market `price_ranges` and fixed-point schema dynamically.

**Sizing:** Show a *raw*, never-live probability-scaled sizing curve for B. For C use a bounded fractional-Kelly-style or edge/confidence-based sizing rule, **after** every hard risk cap. For an ordinary $1/$0 binary contract with all-in cost `c` and estimated probability `q`, the theoretical bankroll fraction staked by full Kelly is `(q - c) / (1 - c)` when positive; this can become dangerously large near $1 and with inaccurate `q`. Default to **at most 10% of theoretical Kelly**, then apply dollar/exposure limits; support a zero-Kelly mode. Prefer conservative `q_safe` to point estimates. No martingale, chasing, loss recovery multiplier, leverage, doubling down, or automatic top-ups.

## 1. Operational boundaries

- Start with a **single local Python 3.12+ application** suitable for a personal Linux PC. Prefer `uv` or standard `venv`; use `pyproject.toml`, type hints, `pydantic`/dataclasses, `pytest`, `ruff`, and `mypy` (or equivalent). Use SQLite in WAL mode for a single-writer MVP and Parquet for archival market data; design abstractions to allow Postgres later. Avoid needless microservices.
- Distinguish **engineering agents** (Claude Code subagents that write/review code) from the **runtime trading pipeline** (normal deterministic software). An LLM must never have unchecked direct authority to submit an order, set a production credential, bypass risk limits, or invent a quantitative probability.
- Markets: initially ordinary event-based **binary YES/NO contracts only**. Exclude perpetually margined/perps, combos, multivariate structures, leveraged instruments, and unusual settlement types unless explicitly supported and tested in a future phase.
- Assume production exchange APIs, terms, market eligibility, and fee schedules can change. Verify official docs when implementing; pin SDK versions; record the API schema version used; fail closed on unsupported data shapes. Follow exchange rules and applicable law; no account/geolocation circumvention, spoofing, wash trading, manipulation, or forbidden scraping.
- Zero live execution by default, including CI, unit tests, demo work, and backtesting. A production order must be impossible without a **separate production credential**, a human-provided live configuration, and a deliberate, interactive arming procedure.
- Claude Code implementation agents may work concurrently on separate files/worktrees. The runtime trader must prevent **multiple instances** from independently submitting duplicate orders (single-instance lock or leader election).

## 2. First task: establish specialized Claude Code subagents

Claude Code recognizes project-scoped Markdown agents in `.claude/agents/`. Create the following **actual files** before the main build; do not merely describe agents in an architecture document. Give each a valid YAML frontmatter with `name`, `description`, `model: sonnet` (or an available equivalent), and narrowly scoped `tools` such as `Read, Glob, Grep, Write, Edit, Bash`. Use read-only tools for auditors. Include the specific mandate below in each agent's body, plus the universal rules in Section 0 and the live-trading safety constraints in Sections 5 and 9.

| Agent file | Primary responsibility | Deliverables |
|---|---|---|
| `.claude/agents/kalshi-api-engineer.md` | Confirm latest **official** REST/WebSocket/SDK docs; implement market metadata, order book, auth, pagination, demo adapter, order lifecycle, fills and position reconciliation; **demo-only integration work** | `src/kalshi_edge/exchange/`, fixtures, API contract tests, documented schema/fee nuances |
| `.claude/agents/strategy-builder.md` | Build research strategies A/B/C/D, feature pipelines, external-data probability estimators, calibration and confidence bounds, common candidate selection, deterministic sizing | `src/kalshi_edge/strategies/`, `models/`, examples, deterministic unit tests |
| `.claude/agents/test-engineer.md` | Independent adversarial testing: math, boundary conditions, simulated APIs, stale quotes, partial fills, race conditions, insufficient funds, duplicate orders, operator safety | `tests/`, property-based tests, integration tests against fakes/demo, coverage report |
| `.claude/agents/backtest-analyst.md` | Acquire historical data, construct event-time datasets, create conservative event-driven replay, detect look-ahead/survivorship bias, walk-forward experiments and sensitivity analysis | `src/kalshi_edge/backtest/`, reproducible experiment configs, `reports/` plots and analyses |
| `.claude/agents/risk-auditor.md` | **Read-only independent reviewer** of bankroll policies, EV math, position correlation, key isolation, production arming, drawdowns, kill switch, and failure modes; block release with findings | `docs/RISK_REVIEW.md`, severity-ranked checklist and documented go/no-go recommendation |
| `.claude/agents/deployment-engineer.md` | Container/systemd deployment, health checks, observability, incident runbooks, demo/paper startup and recovery; **must not provision or use production trading keys** | `deploy/`, `docs/DEPLOYMENT.md`, monitoring, paper/demo smoke tests |

Subagent frontmatter example (adapt the name, description and tools per agent):

```yaml
---
name: test-engineer
description: Independently test Kalshi trading, risk limits, order reconciliation and failure safety; use after any strategy or exchange changes.
tools: Read, Glob, Grep, Write, Edit, Bash
model: sonnet
---
You own independent tests. Never use real money. Read instruction.md and the implemented interfaces. Test from first principles. Do not trust implementation comments when verifying risk/accounting. Report broken invariants before fixing tests.
```

For the **risk-auditor**, use only `Read, Glob, Grep` by default; it reports findings, not patches. For deployment-engineer, demos and local configurations only. If Claude Code supports separate worktree isolation, use it for independent coding tasks; coordinate file ownership to avoid overwriting. Subagents can return findings to the lead; use agent teams only if available and helpful—do not make an experimental Claude feature a runtime dependency.

**Lead agent orchestration:**

1. Read official Kalshi and Anthropic documentation and write a concise technical decision record.
2. Dispatch API-engineer and strategy-builder on disjoint code paths.
3. Dispatch test-engineer after contracts/interfaces stabilize; have it independently challenge edge and safety assumptions.
4. Dispatch backtest-analyst when the data schema and strategies are available; require actual run logs and plots.
5. Have risk-auditor review before demo release and again before proposing a live canary.
6. Have deployment-engineer package and prove paper/demo deployment, **not live deployment activation**.
7. Continue implementation until acceptance gates are met, or clearly document exact blockers, incomplete tasks and commands to reproduce them. Never fabricate results or claim a backtest was run if it wasn't.

## 3. Repository and interface design

Create the following pragmatic initial structure (adjust only with justification in an ADR):

```text
kalshi-edge-bot/
├── instruction.md                    # This spec
├── CLAUDE.md                         # <=200 lines; summary + reference instruction.md
├── README.md
├── pyproject.toml
├── uv.lock                            # Or other reproducible dependency lock
├── .env.example                       # Safe defaults ONLY; no keys
├── .gitignore                         # Ignore secrets, data dumps, logs, reports containing private data
├── .claude/agents/
│   ├── kalshi-api-engineer.md
│   ├── strategy-builder.md
│   ├── test-engineer.md
│   ├── backtest-analyst.md
│   ├── risk-auditor.md
│   └── deployment-engineer.md
├── configs/
│   ├── research.yaml
│   ├── backtest.yaml
│   ├── paper.yaml
│   ├── demo.yaml
│   └── live.example.yaml             # Not armed / no credentials
├── src/kalshi_edge/
│   ├── cli.py                         # Typer/Click preferred
│   ├── config.py
│   ├── domain.py                      # Typed contracts, offers, probability estimates
│   ├── exchange/                      # Official APIs, auth, fees, markets, WebSocket
│   ├── data/                          # Historical ingest, live recording, provider adapters
│   ├── models/                        # Calibration, external forecast models
│   ├── strategies/                    # A/B/C/D; market screening, sizing
│   ├── risk/                          # Hard pre-trade limits and kill switch
│   ├── backtest/                      # Event-time matching & settlement engine
│   ├── execution/                     # Order state machine, idempotency, reconciliation
│   ├── runtime/                       # Scan -> score -> filter -> size -> execute
│   └── monitoring/                    # Metrics, events, alerting
├── tests/{unit,integration,property,chaos}/
├── scripts/
├── deploy/                            # Local service, ops docs, container if needed
├── docs/{PROGRESS.md,ADR.md,RISK_REVIEW.md,DEPLOYMENT.md,TRADING_POLICY.md}
├── data/                               # Ignored, with manifest/metadata templates tracked
└── reports/                            # Timestamped, reproducible runs
```

Define explicit interfaces so the exact same strategy/risk logic is used in backtests, paper trading, demo trading, and live trading:

```python
class MarketDataProvider(Protocol): ...
class ExternalForecastProvider(Protocol): ...
class ProbabilityEstimator(Protocol): ...
class Strategy(Protocol): ...
class RiskManager(Protocol): ...
class ExecutionVenue(Protocol): ...  # backtest, paper, demo, live
class PortfolioRepository(Protocol): ...
class Clock(Protocol): ...           # event-time replay vs wall clock
```

The **only** deployment-dependent component should be the exchange adapter, clock, and event source; no duplicate decision logic. Use a versioned configuration hash and model version in each decision/audit log. Identify orders with persisted unique `client_order_id`; do not blindly retry ambiguous submissions. On restart, fetch resting orders, fills, positions and cash from the exchange, deduplicate event streams, and reconcile before new trading is allowed.

## 4. Market ingestion and executable prices

1. Use official Kalshi Predictions REST/WebSocket APIs, not browser scraping. Prefer the official supported Python SDK when it supports the required APIs; otherwise create a small typed REST/WebSocket adapter.
2. Discover all eligible **open** binary markets with cursor pagination, lifecycle state, trading permissions, close/settlement times, market rules, fee model/multipliers, `price_ranges`, and timestamps. Enforce allow/deny lists for market categories and unsupported structures.
3. Fetch executable book depth. Kalshi binary books commonly expose **YES bids and NO bids**, with the opposite side implying the ask (a YES ask corresponds to a complementary NO bid); independently verify current book semantics and edge cases in the docs. Do not infer fills from `last_price`, midpoints, percentages on a webpage, or candle closes.
4. Inspect every eligible **YES** and **NO** purchase side; a market's 10¢ YES price can imply a >80¢ NO purchase, but spreads and depth matter. Compute the true best ask for the outcome to be bought, with depth available for requested quantity. Validate quote time and price grid.
5. Use order-book depth to calculate the weighted executable price for the intended size, including crossing the spread. Prefer **explicit limit orders** with a worst acceptable price, configured `time_in_force`, self-trade prevention, and (where supported) post-only or fill-or-kill protections. Do not assume a limit order fills at the quoted price.
6. Stream updates when practical; on reconnect or sequence gaps, fetch fresh snapshots and stop decision-making until book and private order state are synchronized. Use rate-limit-aware exponential backoff with jitter and honor `429`/server maintenance; never hammer endpoints.
7. Record immutable market snapshots and authoritative timestamps for later replay, plus exact originating source and late/stale flags. Build live order-book capture early because **historical candles/trades generally do not reconstruct full historical depth**.
8. Recheck event resolution rules, source-of-truth, close and settlement timing. Exclude ambiguous/disputed/paused/closed markets and any nonstandard payout until explicitly modeled.
9. Use fixed-point dollar strings, valid `price_ranges`, fractional count fields where supported, correct YES/NO directionality, and market-specific fee metadata. Do not hardcode an assumption that all prices use 1¢ ticks or all contracts have integral size.

## 5. Risk policy: hard guardrails, enforced in ordinary code

Start with the following **conservative research defaults** (configurable downward; raising them requires explicit operator review). Amounts illustrate a $1,009 bankroll and are not endorsements or a prediction of profit. Compare planned exposure using both the operator's designated **strategy capital** and the current exchange cash/position ledger; use the tighter constraint.

| Control | Initial default / behavior |
|---|---|
| Per-entry maximum potential loss | **1%** of strategy equity (about **$10.09** at $1,009); never above **2%** without a separate reviewed policy |
| Aggregate open worst-case loss budget | **10%** of strategy equity across all open positions and resting orders |
| Same-event or strongly correlated cluster | **5%** of strategy equity; count related YES/NO legs and logically overlapping events |
| Daily realized + conservative unrealized stop | **3%** below start-of-day equity: halt *new* orders and cancel resting entry orders |
| Weekly stop | **6%** from start-of-week equity: human review required |
| Peak-to-trough strategy drawdown | **10%**: latch kill switch; manual intervention required |
| Live orders per rolling day | **10** initially; rate-limit bot independently from the exchange limits |
| High-probability screen | Executable price **strictly > 0.80**; optionally cap at `0.97` until extreme-price behavior is validated |
| Min independent edge | **2 percentage points** *after* modeled acquisition fee, slippage and the conservative probability haircut; research/tune only on training folds |
| Fractional Kelly | `min(0.10 * max(0, raw_kelly_fraction), all_hard_caps)`; compute stake budget and integer/fractional contracts safely |
| Minimum book liquidity | Configurable notional depth/volume and maximum spread; reject thin markets by default |
| Quote staleness | Reject if book or external forecast stale beyond tested thresholds (e.g. 3 seconds for a streaming book; category-dependent for forecasts) |
| Settlement proximity | Do not open positions too close to trading halt/settlement or when outcome may already be known; start with a conservative configurable buffer |
| Markets eligible | Explicit allowlist during canary; no perps, combos, unsupported lifecycle states or unclear resolution rules |
| Kill switch | Persistent, fail-closed; halt new orders; cancel resting orders where safe; **do not auto-liquidate** illiquid positions without a separate operator decision |

**Important:** Correlation can make portfolio losses far larger than a naive assumption of independent bets. Aggregate exposure by event, source, common catalyst, category and mutually dependent outcomes. Also check the worst simultaneous plausible loss scenarios and both sides of a hedge. Model exit slippage and settlement delays; do not treat locked capital as available cash.

For trade acceptance, require **all** conditions:

```text
eligible_binary_market
AND market_open_and_not_paused
AND market_rules_verified
AND executable_side_price > 0.80
AND live_book_fresh_and_sufficient_depth
AND external_forecast_data_fresh
AND independent_model_calibrated_for_market_category
AND q_safe - effective_entry_cost_per_contract - slippage_buffer >= min_edge
AND all position/exposure/drawdown/correlation/order limits pass
AND available_cash_and_fee_reserve_sufficient
AND operational_health_ok
AND no kill switch / no stale reconciliation / no unresolved prior order
AND trading_mode_permits_submission
```

Run a **final pre-submit risk check on a newly refreshed quote and ledger snapshot**, not just at discovery time. All reasons for rejection must be logged. Controls must fail closed on missing balances, incomplete positions, API failure, model failure, market-rule uncertainty, unknown fees, stale timestamps, clock skew, and unhandled settlement cases.

## 6. Probability estimation (the actual research problem)

Do not label `market_price` as `true_probability` and then claim to have detected positive EV. Do not have a language model improvise a 93% probability. Implement an auditable estimator framework:

- **Baseline:** market-implied probability, carefully distinguishing bid/ask, for a negative-after-fee benchmark.
- **External-signal models:** start with one or two narrow, objectively resolvable market categories with timely independent data (for instance, weather forecast distributions or quantifiable published economic forecasts). Only use sources allowed by their licenses/terms. Store original value, source, collection timestamp, lead time, region/category, and any revisions. Do not use data published after the historical trade decision.
- **Calibrator:** isotonic, Platt/logistic or hierarchical/Bayesian calibration as appropriate, using strictly past outcomes and leakage-safe train/validation/test splits. Estimate uncertainty; shrink model predictions toward a justified prior or reject when confidence is insufficient.
- **`q_safe`:** lower conservative bound or stress-adjusted probability from calibration/model uncertainty; document exactly how it is computed. Avoid claiming mathematical guarantees from small samples.
- **Monitoring:** reliability diagrams for `0.80–0.85`, `0.85–0.90`, `0.90–0.95`, `0.95–1.00`; Brier score, log loss, expected calibration error, counts of *independent* resolved events, feature drift, empirical frequency versus predicted frequency.
- **No usable independent model = no eligible live-candidate orders.** Baseline A/B may still run as research controls and in simulations.

Train models only on mature/settled outcomes available **as of** training time. Prevent group leakage from multiple contracts of the same event and near-duplicate topics. Evaluate by event group and out-of-time periods, not random row splitting. Consider rare-event sample size and uncertainty: a 95% win rate estimated over a handful of events is not a reliable edge.

## 7. Historical ingestion, backtester and experiments

Build an event-driven, walk-forward backtester that can replay information available at time `t` and simulate order lifecycle—not a spreadsheet that counts later winners. Kalshi partitions historical and live market data by cutoff; retrieve and deduplicate from both where required, record retrieval dates, preserve original timestamps and immutable raw files.

### Required realistic behavior

- Reconstruct the **historical universe** including markets that later settled or disappeared; do not screen only today's active markets.
- Restrict candidate discovery to metadata available at that point in history; include market opening/closing, halts, events, rules and all outcomes.
- Use the buy **ask** and depth, not market midpoint/last trade or final settlement price. With only candles/trades, mark fill estimates as **uncertain proxies**, not executable backtests. Use conservative spread, adverse selection and no-fill sensitivity scenarios. Record true order books prospectively for stronger tests.
- Event-time feed, order placement latency, partial fills, expired/canceled/rejected orders, queue uncertainty, valid ticks, fee-specific rounding, buy/sell costs, cash locking, settlement delays, possible voids, and correction policies.
- Track both realized and marked equity conservatively, segregating cash, locked capital, pending orders, available buying power and unresolved positions.
- Distinguish capital gain from cash released at settlement. Use per-event correlation rather than treating contracts on the same event as independent samples.
- Account for API/account availability issues and a realistic trading schedule. No instantaneous arbitrage or impossible look-ahead executions.

### Mandatory backtests

1. **Naive high-probability baseline** A.
2. **Original probability-scaled sizing** baseline B, with sizing curve plotted.
3. **Independent EV-filtered candidate** C, with fixed stake vs fractional-Kelly variants.
4. **No 80% filter** ablation D.
5. Perturbations: fees +50%, spread +1 tick, fill probability reductions, delayed quotes, forecast error of ±1/±3/±5 percentage points, clustered losses, 1–5 simultaneous unexpected losing events, delayed settlement and categories excluded.
6. Walk-forward testing by calendar period with no tuning on final holdout; separately assess each category and event horizon.
7. Parameter sensitivity grids (thresholds 0.80/0.85/0.90/0.95; minimum EV thresholds 0/1/2/3pp); report all tried settings, not just the winner.

### Required reports

Each reproducible run should write data manifest, git commit, environment, config hash, universe size, timestamps, model version, excluded rows and reasons, and:

- Final bankroll, net P&L (fees included), ROI on designated capital, return on **deployed** capital, realized and unresolved P&L.
- Profit per trade/market/category, order fill ratio, average time capital locked, average edge estimate, breakeven edge and realized frequency.
- Maximum drawdown (dollars and percent), worst day/week, exposure over time, probability and duration of losing streaks, and **max simultaneous correlated loss**.
- Win rate **and** payoff ratio, expected-vs-realized EV, Brier score, calibration plot, uncertainty intervals by independent event grouping.
- Equity curve, drawdown curve, EV vs market price, histogram of returns and resolved-event sample counts.
- Bootstrap confidence intervals clustered by event/time, not row-naive. Test whether out-of-sample positive returns plausibly exceed fees/uncertainty.

A strategy is **not validated** merely because it backtests positive. A sparse or poorly reconstructable historical sample must be marked **inconclusive**. Present evidence for losing strategies as well.

## 8. Modes, CLI, services and observability

Implement four mutually exclusive runtime modes, chosen through a validated config and visible in every log/UI:

| Mode | Price source | Order destination | Notes |
|---|---|---|---|
| `backtest` | Historical event-time data | Internal replay engine | Deterministic given seed/config/data |
| `paper` | **Live production public data** | Local virtual exchange only | Absolutely no production order API access or production trading key required |
| `demo` | Kalshi demo environment | **Demo** trading API | Separate demo credentials; behavior may not resemble production |
| `live` | Live production feed | **Production** order API | Disabled by default; operator-controlled, separate secrets and safeguards |

Suggested CLI (names/arguments may be adjusted but document them in README):

```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run kalshi-edge doctor
uv run kalshi-edge data sync --config configs/research.yaml
uv run kalshi-edge backtest --config configs/backtest.yaml --strategy naive-80
uv run kalshi-edge backtest --config configs/backtest.yaml --strategy scaled-80
uv run kalshi-edge backtest --config configs/backtest.yaml --strategy positive-ev-80
uv run kalshi-edge report --latest
uv run kalshi-edge run --mode paper --config configs/paper.yaml
uv run kalshi-edge run --mode demo --config configs/demo.yaml
uv run kalshi-edge status
uv run kalshi-edge kill --reason "operator stop"
```

**Do not create a copy-paste shell command that could start live trading without explicit human interaction.** Describe the separately guarded production arming sequence in `docs/DEPLOYMENT.md` after it has been designed and tested; the operator must enter a fresh authorization interactively in a TTY. An agent must not auto-generate the arming approval or provide its own confirmation.

Runtime pipeline:

```text
Market discovery + live snapshots
      ↓
Eligibility and tradability filters
      ↓
Independent external forecast + calibration
      ↓
Executable-book / total-fee EV computation
      ↓
Conservative risk gates + exposure aggregation
      ↓
Order intent with immutable decision snapshot
      ↓
Execution adapter (paper / demo / human-armed live)
      ↓
Private fill stream + REST reconciliation + settlement
      ↓
Portfolio, audit trail, dashboards, alerts, kill switch
```

Export structured JSON logs, a local read-only status dashboard (simple terminal or localhost web interface), and alerts for orders/fills, daily drawdown, stale API feed, repeated rejects, unmatched fills, account mismatch, and kill switch. Do not require cloud infrastructure for initial MVP. For later deployment, a single systemd service on a personal Linux machine is adequate; use restart limits and **do not automatically re-arm live mode on reboot**.

## 9. Production authorization and key isolation

Implement the **code path** for live trading and test it with fakes and the official demo API, but **do not activate it** during the build.

Mandatory production safeguards:

1. Runtime default is `paper`; production trading requires a separate, inaccessible-to-CI credential provisioned by the human operator outside the repository. Don't echo, log, commit, or send API secrets to an LLM. `.env.example` contains only placeholders. Never expose secrets in screenshots or reports.
2. Production exchange host and credentials live in an isolated `LiveExecutionVenue` module/config with **deny-by-default** policy; unit/backtest/paper tests assert no route to production order methods. Test with a fake server/transport that would fail if anything connects to a production order endpoint.
3. Two separate opt-ins: a reviewed live configuration with explicit `allow_live_trading: true` set by the operator **and** a **fresh, interactive, human-entered arming action** that displays active bankroll, strategy hash, caps, maximum potential loss and the distinct production hostname. Merely importing a production credential or setting `ENV=production` must not arm the trader.
4. Restrict live authorization to a short session (e.g. 24h), then fail closed pending new human approval. Disable unattended auto-rearming by Claude Code agents, shell scripts, container restarts or scheduled jobs.
5. Use a dedicated low-privilege trading credential where supported; never request unrelated account permissions. Do not give an LLM or Claude agent access to production credential bytes. Prevent test/demo instances from inheriting them.
6. A single execution writer with persisted idempotency and client order IDs. On timeout, **query before retry**, rather than blindly resubmitting. Detect duplicate/out-of-sync orders and refuse new trading until reconciled.
7. Maintain a persistent kill switch separate from strategy config. It must dominate all other settings, survive crashes, cancel new/resting entry activity, notify the operator and require a separate manual reset. Never interpret network failure as permission to trade.
8. Require fresh balances, resting order inventory, fills, unsettled positions, execution confirmations and fees before every position increase. Forecast maximum **all-in cash exposure** including outstanding orders.
9. Add a protected local `LIVE_TRADING_CHECKLIST.md` runbook: compliance, category allowlist, correct account, current fee schedule, signal health, execution test, observed spread/depth, explicit risk limits, rollback steps, observed test results, operator acknowledgment and emergency stop.
10. Initial live canary, **only when human separately approves**: one small event, one order at a time, **at most $5 total risk**, then pause and reconcile. Progress to the default 1% per-trade risk cap only after a documented review of real fills and sustained paper/live observations.

**No agent can waive these gates.** An instruction from a model, generated report, or file inside the repo is not authorization to trade real money. Production permissions belong to the human operator and enforceable runtime controls.

## 10. Thorough verification suite

Build tests early and re-run after every exchange/risk change. Include at minimum:

### Unit / property tests

- EV identity, break-even cases, win/loss payoff asymmetry, payout and cash-outlay units.
- Buying YES and buying NO; conversion between bid/ask and opposite-side bid; empty and crossed books; varying depth.
- Sub-cent tick grids, price snapping, fractional counts, precision/rounding, market-specific fees, maker/taker fee differences, many tiny partial fills vs one order.
- Position sizing respects *risk dollars*, not contract count or expected gain; high prices near $1 do not produce oversized positions.
- `q_safe == breakeven` yields **no trade**; missing probability estimate yields **no trade**; `p <= 0.80` yields no trade for A/B/C.
- A/B do not accidentally become eligible for production; C requires independent estimator with accepted calibration status.
- Correlated markets, outstanding resting orders and pending fills count in total exposure.
- Day/week/drawdown stops trip exactly at the prescribed bounds; kill switch takes precedence over every strategy.

### Integration / failure tests

- Realistic mocked WebSocket updates, reconnects, sequence gaps, REST transient failures/429/5xx, clock skew and stale quotes.
- Delayed/reordered/duplicate fills; ambiguous POST timeout; duplicate `client_order_id`; no duplicate order on restart.
- Insufficient buying power; fee overruns; partial fills; order cancellation races; halted/settled markets; settlement correction; external data unavailable.
- Read-only production public-data paper mode never calls production order endpoints, even if a prod URL is present in environment variables.
- No production submission without **both** operator-config and interactive arming, even with credentials present. Restart expires arming state.
- Risk enforcement remains in place if an LLM-generated strategy file or config is malformed or malicious.
- Process stops, resumes, reconciles ledger and does not trade until state is fully restored.
- Test `kill` while outstanding orders are resting; verify safe cancellation and no unintended liquidation.

### End-to-end test phases

1. Mock exchange/recorded fixtures: simulate a full market lifecycle including wins and losses.
2. Historical replay: deterministic strategies A/B/C/D, reproducible report hashes.
3. Paper using live *public* exchange data: process quotes and simulate orders with conservative fills and fees.
4. Kalshi demo with human-provisioned **demo-only** credentials: place/cancel benign mock-funds orders and reconcile; do not claim demo P&L predicts real performance.
5. Separate **operator-approved** production canary after all previous gates—not part of autonomous implementation.

Report test counts, coverage, known gaps and exact commands used. CI must not possess production credentials and must hard-fail any outbound request to a production order endpoint.

## 11. Phased execution / release gates

**Phase 0 — Verify the ground truth**

- Fetch official docs, identify current API V2 ordering path and field names; write `docs/ADR.md` with live/documentation dates, selected libraries and account safety model.
- Create `.claude/agents/*.md`, small `CLAUDE.md`, skeleton package, all sample configs. Initialize git if appropriate; never commit secrets.
- Create `docs/PROGRESS.md` with phase checkboxes, owner, actual test/research evidence and blockers.

**Phase 1 — Data, modeling and research baselines**

- Build market discovery, normalizer, order book, metadata, historical/live ingest and versioned storage.
- Implement mathematical baselines A/B and strategy C's inference interface. If no independently validated forecast model exists, leave C **inactive**, not mocked up as profitable.
- Unit tests on fees, EV, price conversion, market screening and stake sizing. Make basic backtest CLI work with honest data provenance.

**Phase 2 — Reproducible backtesting**

- Implement executable-ish event-time replay with explicit liquidity limitations; obtain as much suitable settled history as terms allow.
- Create train/validation/test partitions and walk-forward calibration for at least one well-specified external-data market category if feasible.
- Run all four strategies and stress tests. Generate timestamped `reports/` artifacts, charts and a frank `docs/RESULTS.md` stating whether apparent edge remains after cost and uncertainty.
- If data or sample size cannot support a credible conclusion, state **NO EVIDENCE YET**; do not silently substitute assumptions.

**Phase 3 — Paper and demo**

- Run/replay on **current** public books, start prospective tick/depth collection, reconcile virtual order state and settlement. Provide a run command that can operate as an ordinary local service.
- Complete independent tests, risk review, incident simulation, demo order lifecycle and local deployment runbook.
- Require a substantial predeclared observation window and sufficient **independent settled events** for calibration (propose a defensible criterion in `docs/TRADING_POLICY.md`, rather than inventing statistical confidence from an arbitrary number of trades). `NO TRADE` must be a normal output.

**Phase 4 — Live capability, NOT live activation**

- Implement hardened LiveExecutionVenue, manual arming, hard limits, stop/kill, logging and private reconciliation behind defaults that reject production use.
- Independently test against mocks. Have risk-auditor produce `docs/RISK_REVIEW.md` with unresolved issues and a human-signable release checklist.
- At the end of implementation, give the human operator a concise status: tests actually passed, model edge evidence, paper stats, unresolved risks and steps for *separate* human deployment approval. **Leave live disarmed**.

**Phase 5 — Human-controlled live canary (future separate session only)**

- Only after the operator independently authorizes a live-money trial, use the guarded procedures described in Section 9. Do not assume authorizing the software build constitutes authorization to place trades. Start with ≤$5 maximum total risk; pause and reconcile before any scale-up.

## 12. Success / fail acceptance criteria

Declare **research prototype complete** only when:

- Every specialized `.claude/agents/` definition exists and the corresponding work/report is visible.
- Install, test, backtest and paper commands work on a clean checkout **without** a production API key.
- Strategy A/B/C/D are selectable and correctly distinguish price threshold, size scaling and independently estimated EV.
- Research replay uses realistic and transparently limited assumptions; its outputs are reproducible and include losing scenarios, fees, locked capital and confidence intervals.
- The high-probability strategy cannot trade real money by default and cannot bypass independent risk checks.
- Portfolio/order reconciliation, idempotency and kill-switch behavior have passing integration tests.
- The docs explain data quality, remaining unknowns, eligibility/terms, monitoring, failure recovery and operator procedures.
- The lead's final response describes **what was actually built and executed** (with output paths), not promises.

Declare **live candidate validated** only if independent holdout and prospective paper trading show convincing positive net expected returns with sufficient independent resolved events, well-behaved calibration, bounded correlated loss exposure, and a clean risk audit. Even then, future gains are uncertain; there is no guarantee of slow, steady profits.

## 13. Canonical starting references (verify freshness before use)

- Kalshi Predictions API home: https://docs.kalshi.com/
- Kalshi documentation index: https://docs.kalshi.com/llms.txt
- API demo environment: https://docs.kalshi.com/getting_started/demo_env
- API environments: https://docs.kalshi.com/getting_started/api_environments
- Order creation V2: https://docs.kalshi.com/api-reference/orders/create-order-v2
- Orderbook mechanics: https://docs.kalshi.com/getting_started/orderbook_responses
- Fixed-point prices, quantity, ticks: https://docs.kalshi.com/getting_started/fixed_point_migration
- Fee rounding: https://docs.kalshi.com/getting_started/fee_rounding
- Historical data: https://docs.kalshi.com/getting_started/historical_data
- Rate limits: https://docs.kalshi.com/getting_started/rate_limits
- Official Kalshi fee schedule: https://kalshi.com/fee-schedule
- Kalshi developer agreement: https://kalshi.com/developer-agreement
- Claude Code custom subagents: https://code.claude.com/docs/en/sub-agents
- Claude Code project memory / CLAUDE.md: https://code.claude.com/docs/en/memory

**Final instruction to Claude Code:** Build the working research and paper/demo platform first, with independent agents and reproducible measurements. A high win rate alone is not success. Live trading must remain disabled and unarmed unless the human operator returns later and explicitly authorizes a bounded real-money trial.
