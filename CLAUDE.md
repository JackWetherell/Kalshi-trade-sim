# CLAUDE.md — kalshi-edge

Full specification: **instruction.md** (read it before non-trivial work). Progress: docs/PROGRESS.md.

## What this is
A single-process Python 3.12+ research, backtest, paper and demo trading system for Kalshi
**binary** markets. The "buy contracts priced > $0.80" idea is an unproven hypothesis:
- A `naive-80`, B `scaled-80`: backtest-only controls.
- C `positive-ev-80`: the only live-eligible strategy; trades only when an *independent,
  calibrated* `q_safe` clears `p + fees + slippage + min_edge`. NO TRADE is normal.
- D `positive-ev-ablation`: C without the 0.80 floor (research).

## Safety rules (never violate)
- **No real-money orders.** Live trading is disabled by default and requires: an operator-written
  live config (`allow_live_trading: true`), an operator-provisioned production credential outside
  the repo, and a fresh interactive TTY arming ceremony (expires ≤24h and on restart). An AI
  agent must never perform, script, or confirm the arming step.
- Never read, print, log or commit secrets. `.env.example` contains placeholders only.
- Money is `decimal.Decimal`. Executable prices walk the **ask** side (YES ask = 1 − NO bid).
- Risk limits in `config.py` have hard ceilings; risk checks fail closed. Kill switch dominates.
- Never fabricate results; synthetic backtests are engine validation, not evidence of edge.

## Layout
- `src/kalshi_edge/domain.py`, `interfaces.py`, `config.py`, `fees.py`, `pricing.py`: shared contracts.
- `exchange/` Kalshi adapters (public data, demo, guarded live); `strategies/`, `models/`;
  `risk/` hard limits + kill switch; `execution/` order store, idempotency, reconciliation;
  `backtest/` event-time replay; `runtime/` scan→score→risk→execute loop + arming;
  `monitoring/` logs/status; `data/` recording + storage.
- `.claude/agents/`: project subagents (api engineer, strategy builder, test engineer,
  backtest analyst, read-only risk auditor, deployment engineer).

## Commands
```bash
uv sync
uv run pytest -q
uv run ruff check .
uv run mypy
uv run kalshi-edge doctor
uv run kalshi-edge backtest --config configs/backtest.yaml --strategy positive-ev-80
uv run kalshi-edge run --mode paper --config configs/paper.yaml
uv run kalshi-edge kill --reason "operator stop"
```

## Environment notes
- Tests never touch the network. `-m network` / `-m demo` tests are opt-in.
- The build container could not reach Kalshi hosts (network policy); see docs/ADR.md.
