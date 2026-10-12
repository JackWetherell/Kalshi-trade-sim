# Deployment guide (paper and demo only)

This guide packages the **paper** and **demo** modes for unattended running on a personal Linux
machine. Live (real-money) trading is **not** deployable as a service; see the final section, which is a
description only. Nothing in `deploy/` provisions, requests or uses production credentials.

Environment note: the build container could not reach any Kalshi host (ADR-1). There, paper mode reports
`halted: market_discovery_failed` every step. That is correct fail-closed behaviour, and all smoke tests
work offline. Run `uv run kalshi-edge doctor --network` on a networked machine first.

## 1. Install

```bash
git clone <repo> && cd Kalshi-trade-sim
uv sync                      # Python >= 3.12, locked by uv.lock
uv run kalshi-edge doctor    # configs parse, safety posture, no production env vars
uv run pytest -q
deploy/smoke/paper_smoke.sh  # offline end-to-end paper smoke (about 20 s)
```

If `uv` fails with TLS errors behind a proxy, run it as `env -u UV_NATIVE_TLS uv ...`.

## 2. Configs and modes

| Config | Mode | Price source | Orders go to | Credentials |
|---|---|---|---|---|
| `configs/research.yaml` | n/a (recorder, `data sync`) | prod public data | nowhere | none |
| `configs/backtest.yaml` | backtest | historical / synthetic | replay engine | none |
| `configs/paper.yaml` | paper | prod **public** data | local virtual venue | none |
| `configs/demo.yaml` | demo | Kalshi demo env | demo trading API (mock funds) | demo-only, env vars |
| `configs/live.example.yaml` | live | prod | prod order API | **never in this repo**; see section 10 |

The `--mode` flag must equal the config's `mode` (otherwise the CLI rejects the run). Paper and demo
processes delete `KALSHI_PROD_KEY_ID` and `KALSHI_PROD_PRIVATE_KEY_PATH` from their environment at startup,
and the systemd units additionally `UnsetEnvironment=` them. `allow_live_trading` is `false` in every
committed config.

State lives in `paths.state_dir` (`var/paper`, `var/demo`): `kalshi_edge.sqlite3` (orders, fills, events,
risk state), `KILL_SWITCH`, `trader.lock`, `trader.log.jsonl`. All of `var/` is gitignored.

## 3. Paper procedure

```bash
uv run kalshi-edge run --mode paper --config configs/paper.yaml      # foreground, Ctrl-C to stop
uv run kalshi-edge status --config configs/paper.yaml                # read-only JSON snapshot
uv run kalshi-edge kill --config configs/paper.yaml --reason "why"   # engage the kill switch
```

Each step prints one JSON line to stdout (`{"mode": "paper", "ts": ..., "scanned": ..., "halted": ...}`).
Strategy C has no wired-in forecast feed (ADR-8), so **NO TRADE is the expected output**; an idle paper trader is
not a malfunction. `--steps N --interval S` bounds a run (used by smoke tests).

## 4. Demo procedure

1. A human creates **demo-only** API credentials on Kalshi's demo environment (mock funds).
2. Store them outside the repo, `chmod 600`:
   `~/.config/kalshi-edge/demo.env` with `KALSHI_DEMO_KEY_ID=...` and
   `KALSHI_DEMO_PRIVATE_KEY_PATH=/home/you/.config/kalshi-edge/demo_private_key.pem`.
3. Review each series' rules personally before adding it to `exchange.rules_verified_series` in `configs/demo.yaml`.
4. `set -a; . ~/.config/kalshi-edge/demo.env; set +a; deploy/smoke/demo_smoke.sh` (prints
   `SKIPPED: no demo credentials` and exits 0 when the variables are absent; it never fakes a pass).
5. Run `uv run kalshi-edge run --mode demo --config configs/demo.yaml`. Demo behaviour and P&L do not predict
   production behaviour.

## 5. Recorder

`uv run kalshi-edge data record --config configs/research.yaml --interval 60` snapshots public market metadata
and order books to `data/recorded/<YYYY-MM-DD>/snapshots-<HH>.jsonl.gz`. It is read-only and needs no
credentials. The policy's 60-day paper window assumes continuous recording, so run it as a service (below).

## 6. systemd (user) installation

Units: `deploy/systemd/kalshi-edge-{paper,demo,recorder}.service`, plus `kalshi-edge-health.{service,timer}`.
They use the placeholder `%REPO_DIR%` (not a systemd specifier), so substitute it while installing:

```bash
mkdir -p ~/.config/systemd/user ~/.config/kalshi-edge
for u in paper recorder health; do
  sed "s#%REPO_DIR%#$PWD#g" deploy/systemd/kalshi-edge-$u.service > ~/.config/systemd/user/kalshi-edge-$u.service
done
cp deploy/systemd/kalshi-edge-health.timer ~/.config/systemd/user/
# demo unit only after section 4 is done:
# sed "s#%REPO_DIR%#$PWD#g" deploy/systemd/kalshi-edge-demo.service > ~/.config/systemd/user/kalshi-edge-demo.service
systemd-analyze --user verify ~/.config/systemd/user/kalshi-edge-paper.service
systemctl --user daemon-reload
systemctl --user enable --now kalshi-edge-paper kalshi-edge-recorder kalshi-edge-health.timer
loginctl enable-linger "$USER"      # keep user services running without a login session
```

Properties of the units: `Restart=on-failure` with `StartLimitIntervalSec=600`, `StartLimitBurst=5` (the unit
stays failed after repeated crashes until an operator investigates); exit codes 2 (lock held) and 3 (not
armed) are never retried; `NoNewPrivileges`, `PrivateTmp`, `ProtectSystem=strict` with write access only to
`var`, `data`, `reports`, `.venv` and the uv cache; production credential variables are unset. Only one trader
per state directory can run, enforced by `fcntl` lock, so enabling both a service and a manual run is safe: the
second refuses to start.

**There is no live unit, on purpose.** Live trading needs a fresh human-typed arming ceremony in a TTY. Arming
tokens live in process memory only, are bound to the PID, expire within 24 h and vanish on any restart, so a
reboot, crash or `systemctl restart` cannot re-arm live trading. Never write a unit, cron job or wrapper that
tries to.

Optional container: `podman build -f deploy/Containerfile -t kalshi-edge .` (non-root user, no secrets baked in,
default command is paper mode). Mount `var/`, `data/`, `reports/` as volumes.

## 7. Logs and rotation

- Structured JSON lines (`ts, level, mode, logger, msg, data`) go to stderr (journald under systemd) and to
  `var/<mode>/trader.log.jsonl`. Secret-looking keys are redacted. View: `journalctl --user -u kalshi-edge-paper -f`,
  or `jq -c . var/paper/trader.log.jsonl | tail`.
- Alerts (`stale_feed`, `repeated_rejects`, `reconcile_failed`, ...) are logged at WARNING and stored as
  `alert:<kind>` events in SQLite, visible in `status` under `recent_events`.
- Rotation: `deploy/logrotate/kalshi-edge` (copytruncate, 14 daily files, compressed). Run it from a user cron/timer
  with `logrotate --state ~/.local/state/kalshi-edge-logrotate.state`. journald has its own limits
  (`SystemMaxUse=` in journald.conf).

## 8. Health checks

`deploy/healthcheck.sh [--config configs/paper.yaml] [--max-age 600]` runs `kalshi-edge status` and exits
nonzero when the kill switch is engaged, any order is unresolved (INTENT/SUBMITTING/UNKNOWN), a daily/weekly/
drawdown stop is latched, or the heartbeat is stale (newest event, or SQLite DB/WAL modification time, older
than `--max-age`). Exit 0 healthy, 1 unhealthy, 2 check could not run.

- systemd: `systemctl --user enable --now kalshi-edge-health.timer` (every 5 min); failures show in
  `systemctl --user --failed` and `journalctl --user -u kalshi-edge-health`.
- cron: `*/5 * * * * cd /ABS/PATH/TO/REPO && deploy/healthcheck.sh || your-notifier`.
- `uv run kalshi-edge doctor` (add `--network` on a networked host) checks configs and the credential posture.

Note: a halted step (kill switch, `market_discovery_failed`) writes no `step` event, so staleness is judged from
the DB/WAL mtime, which `persist()` updates every step.

## 9. Upgrade, rollback, retention

Upgrade: `systemctl --user stop kalshi-edge-paper`; `git fetch && git log HEAD..origin/main --stat` (read it);
`git checkout <tag-or-commit>`; `uv sync`; `uv run pytest -q && deploy/smoke/paper_smoke.sh`; `systemctl --user start ...`.
Record the previous commit and `config_hash` (shown by `status`) beforehand. Rollback: stop, `git checkout <previous>`,
`uv sync`, start; the SQLite store is forward-compatible within a release line, but copy it first
(`cp -a var/paper var/paper.bak-$(date +%F)`) because there is no downgrade migration. Never upgrade or change
configs while an order is unresolved: run `status`, resolve it (RUNBOOK.md), then proceed.

Retention: keep `kalshi_edge.sqlite3` (the audit trail) indefinitely; archive rather than delete. Recorded data under
`data/recorded/` is the research asset (about one gzip file per hour); prune or move to cold storage only after
backtests that depend on it are reproduced and `reports/` hashes are archived. Rotated logs: 14 days.
Back up `var/` and `data/` with `rsync`/`restic` while the trader is stopped or using `sqlite3 .backup`.

## 10. Guarded production arming sequence (DESCRIPTION ONLY — not performed)

This section describes what a human operator would do; no agent, script, service, cron job or CI job may do any
of it, and none of it has been done. There is deliberately no copy-paste command here. Prerequisites in
`docs/TRADING_POLICY.md` (60-day prospective window, ACCEPTED calibration, clean risk audit) must be met first.

1. **Complete `LIVE_TRADING_CHECKLIST.md`** by hand: compliance and eligibility, category allowlist, correct
   account, current fee schedule, signal health, a demo execution test, observed spread/depth, explicit risk
   limits, rollback steps, observed results, acknowledgement, emergency stop, with your own signature.
2. **Create a private live config outside the repository** by copying `configs/live.example.yaml`, and set
   `allow_live_trading: true` yourself, name explicit categories, and keep the canary caps
   (`max_orders_per_day: 1`, total risk at most $5). The committed example stays `false`.
3. **Provision a dedicated, low-privilege production API key** in the Kalshi account yourself, store it outside the
   repo (not on a shared or agent-accessible machine path), and export `KALSHI_PROD_KEY_ID` and
   `KALSHI_PROD_PRIVATE_KEY_PATH` **only in the one terminal** you will use. Never paste them into chats, logs or
   tickets; never give them to an AI agent. Setting them does not arm anything.
4. **Run `kalshi-edge run` with mode `live` and your private config path in an interactive terminal** (a real TTY
   on both stdin and stdout; the command refuses CI, agent environments and pipes). The arming ceremony displays the
   production host, strategy, config hash, bankroll, per-entry and aggregate caps, the canary cap, the **maximum
   potential loss** and the arming expiry; then it asks you to retype a random one-time challenge
   (`ARM <host> <hex>`) and to retype the maximum loss in dollars. Any mismatch aborts with "NOT ARMED".
5. **Expiry and restarts:** the arming token is in memory only, tied to the PID, valid at most 24 hours. Process
   exit, crash, reboot or service restart disarms; re-arming needs a new ceremony. Never wrap this step in a
   service, `expect` script or scheduler.
6. **Canary discipline:** one small event, one order at a time, at most $5 total risk. After each fill or settlement
   stop, run `status`, reconcile against the exchange UI and record the result in the checklist before continuing.
   Scale up only after a written review of real fills.
7. **Emergency stop:** `kalshi-edge kill` from any terminal (halts new orders, cancels resting entries, does not
   liquidate positions); reset is interactive only. Also revoke the API key in the Kalshi account if needed.
