# kalshi-edge

Research, backtest, paper and demo trading system for **Kalshi binary (YES/NO) markets**.
Full specification: [`instruction.md`](instruction.md). Status: [`docs/PROGRESS.md`](docs/PROGRESS.md).

> **Live trading is disabled.** Nothing in this repository can place a real-money order without
> (1) an operator-written live config with `allow_live_trading: true`, (2) an operator-provisioned
> production key kept outside the repo, and (3) a fresh interactive TTY arming ceremony that expires
> in ≤24h and on restart. See [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

## The hypothesis, and why it's a control
"Buy anything priced above $0.80 and collect small wins" is **unproven**. A price is not a probability:
buying at $0.90 when the true probability is 90% loses money after fees. So:

| Strategy | Id | Role |
|---|---|---|
| A | `naive-80` | buy executable price > 0.80, constant notional — **control only** |
| B | `scaled-80` | as A, risk budget rising with price — **control only** |
| C | `positive-ev-80` | trades only if an *independent, calibrated* `q_safe` ≥ `p + fees + slippage + 2pp` — **only live-eligible strategy** |
| D | `positive-ev-ablation` | C without the 0.80 floor — research |

`EV = q − p − f`, `breakeven = p + f`, `edge = q_safe − p − f − slippage`. A long binary can lose its
full cash outlay plus fees. `NO TRADE` is a normal output.

## Quick start
```bash
uv sync
uv run pytest -q                 # whole suite, no network needed
uv run ruff check . && uv run mypy
uv run kalshi-edge doctor        # environment + config + safety posture (add --network to probe Kalshi)
```

## Commands
```bash
# Backtests (deterministic; synthetic worlds unless backtest.dataset=recorded)
uv run kalshi-edge backtest --config configs/backtest.yaml --strategy naive-80
uv run kalshi-edge backtest --config configs/backtest.yaml --strategy scaled-80
uv run kalshi-edge backtest --config configs/backtest.yaml --strategy positive-ev-80
uv run kalshi-edge backtest --config configs/backtest.yaml --strategy positive-ev-ablation --scenario efficient
uv run kalshi-edge report --latest
uv run python scripts/run_experiments.py --config configs/backtest.yaml --out reports/experiments

# Data (public endpoints only, no credentials)
uv run kalshi-edge data sync   --config configs/research.yaml      # historical cutoff + settled markets
uv run kalshi-edge data record --config configs/research.yaml      # prospective order-book capture

# Paper: live public data, local virtual exchange
uv run kalshi-edge run --mode paper --config configs/paper.yaml
uv run kalshi-edge run --mode paper --config configs/paper_baseline.yaml   # A as a plumbing check

# Demo: Kalshi demo environment with DEMO-only credentials in env vars
uv run kalshi-edge run --mode demo --config configs/demo.yaml

uv run kalshi-edge status --config configs/paper.yaml
uv run kalshi-edge kill   --config configs/paper.yaml --reason "operator stop"
uv run kalshi-edge kill   --config configs/paper.yaml --reset     # interactive only
```

## Modes
| Mode | Prices | Orders go to | Credentials |
|---|---|---|---|
| `backtest` | replayed event-time data | in-process `PaperVenue` | none |
| `paper` | live **public** production data | in-process `PaperVenue` | none (prod creds are stripped from env) |
| `demo` | Kalshi demo | Kalshi **demo** API | demo-only key |
| `live` | production | production API | operator-only, plus interactive arming |

## Layout
```
src/kalshi_edge/
  domain.py interfaces.py config.py fees.py pricing.py   shared contracts
  exchange/     REST/WS adapters, RSA-PSS auth, strict normalizers, guarded demo/live venues
  strategies/   A/B/C/D, candidate selection, sizing
  models/       market-implied baseline, weather threshold model, isotonic/Platt calibration, q_safe
  risk/         hard limits, exposure clusters, loss stops, persistent kill switch
  execution/    SQLite store, idempotent engine, reconciliation, paper venue
  backtest/     event-time replay, metrics, reports
  runtime/      shared TradingLoop, per-mode factory, lock, live arming ceremony
  data/         recorder, recorded-data loader, synthetic worlds
  monitoring/   JSON logs, alerts, status
.claude/agents/ project subagents (api engineer, strategy builder, test engineer, backtest analyst,
                read-only risk auditor, deployment engineer)
```

## Documents
[ADR](docs/ADR.md) · [Models](docs/MODELS.md) · [Trading policy](docs/TRADING_POLICY.md) ·
[Results](docs/RESULTS.md) · [Risk review](docs/RISK_REVIEW.md) · [Tests](docs/TEST_REPORT.md) ·
[Deployment](docs/DEPLOYMENT.md) · [Runbook](docs/RUNBOOK.md)

## Known limitations
- The build environment could not reach Kalshi or any forecast source. The exchange adapter follows the
  documented schemas but has **not been checked against live responses**, and every backtest so far uses
  **synthetic** data (engine validation only). Real-market edge: **NO EVIDENCE YET**.
- No live external-forecast feed is wired in, so strategy C trades nothing in paper, demo or live.

Nothing here is financial advice, and positive backtests do not guarantee future profits.
