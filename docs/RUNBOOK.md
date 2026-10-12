# Incident runbook (paper and demo)

Conventions: `CFG=configs/paper.yaml` (or `configs/demo.yaml`). `status` = `uv run kalshi-edge status --config $CFG`.
Logs: `var/<mode>/trader.log.jsonl` and `journalctl --user -u kalshi-edge-paper`. Events/alerts:
`status | jq .recent_events`. Golden rules: the kill switch dominates; never blind-resubmit an order; never
resolve an incident by editing the SQLite file or deleting `KILL_SWITCH` by hand; network failure never means
"trade anyway". Nothing here applies to live trading, which has not been performed.

## 1. Stale feed / `market_discovery_failed`
Symptom: step JSON `"halted": "market_discovery_failed"`, `alert:stale_feed` events, healthcheck heartbeat fine
but `scanned` is 0. The trader fails closed: no new orders. Expected permanently inside the build container.
1. `uv run kalshi-edge doctor --network` (DNS, proxy, TLS, HTTP status of public endpoints).
2. Check Kalshi status page, local clock (section 11), and rate limiting (429s in logs; lower
   `exchange.max_requests_per_second`).
3. If an API schema change is suspected (`schema.SCHEMA_VERSION` is "live-unverified"), stop the trader, record
   one round with `data record --rounds 1`, and compare payload shapes to `tests/fixtures/kalshi`.
4. The trader resumes by itself when discovery succeeds; no manual reset. If down for long, check the
   `risk_state` day/week rollover in `status`.

## 2. Repeated rejects
Symptom: `alert:repeated_rejects`, rising `rejections` counts in step JSON.
1. Read the reason keys in `rejections` (risk reasons are deterministic: price grid, exposure cap, stale book,
   unknown fees, insufficient cash).
2. Venue rejects (demo): inspect `orders` in `status`/SQLite for the venue message. Do not loosen risk limits to
   make rejects go away; limits can only be lowered by YAML.
3. If they persist, engage the kill switch (section 7), fix the cause, test on paper, then reset.

## 3. Ambiguous submit / UNKNOWN orders
Symptom: `alert:ambiguous_submit`; `status` shows ids in `unresolved_orders`; healthcheck exits 1; new trading
is blocked by design.
1. Do NOT restart-loop and do not resubmit by hand. The engine queries the venue by `client_order_id` each step and
   during startup reconciliation; most cases clear themselves within a step or two.
2. If still unresolved: open the demo/exchange UI and look for the order by `client_order_id` (from status).
   Found filled/resting: reconciliation will import it. Not found after repeated queries: wait for the venue's
   propagation window, keep the kill switch engaged, and escalate (the order record must be resolved by the code
   path, not by editing the DB).
3. After resolution `unresolved_orders` is empty and the healthcheck passes.

## 4. Unmatched fills / position mismatch
Symptom: `alert:unmatched_fill` or `reconcile_failed` with issues listing a ticker.
1. Engage the kill switch. Run `status`; note `fills`, `paper_account.positions` and the issues text.
2. Compare to the exchange UI (demo) fills/positions. Causes: delayed/duplicate fills (idempotent by fill id),
   a manual order placed in the UI, partial fill after cancel race.
3. If the exchange has activity the store does not know about, it is an external action: record it, do not trade
   until reconciliation is `ok=True` on restart (`startup reconcile ok=...` line).
4. Paper mode mismatch means a bug: preserve `var/paper` (copy), file an issue with the state dir and logs.

## 5. Account mismatch
Symptom: `alert:account_mismatch` (cash or position totals differ between ledger and venue).
1. Kill switch on. Do not top up or withdraw to "fix" the numbers.
2. Verify the credentials point to the intended demo account (wrong key file is the usual cause) and that no other
   client trades it. Compare cash in the UI with `status` accounting.
3. Resolve per section 4; trade only after a clean startup reconcile.

## 6. Daily / weekly / drawdown stop
Symptom: `"halted": "loss_stop"`, `alert:daily_stop|weekly_stop|max_drawdown`; `risk_state.daily_halt|weekly_halt|
drawdown_kill` true.
- Daily: clears at the next UTC day rollover. Weekly: latched until the next week. Both are automatic and must not
  be bypassed.
- Drawdown (10%): engages the kill switch. Review the trades (`status`, SQLite `decisions`/`fills`, reports) before
  any reset; write down what happened. A reset after a drawdown kill is a decision, not a routine.
- Never increase size to "recover".

## 7. Kill switch engage / reset
- Engage (anytime, any terminal): `uv run kalshi-edge kill --config $CFG --reason "text"`. The trader halts new
  orders and cancels resting entries on its next step; positions are not liquidated. It survives crashes and
  restarts (a file in `state_dir`); an unreadable switch counts as engaged.
- Inspect: `status | jq .kill_switch`.
- Reset: only in an interactive terminal: `uv run kalshi-edge kill --config $CFG --reset`, then type `RESET`.
  Non-interactive reset is refused (exit 2). Confirm the cause is fixed and reconciliation is clean first.

## 8. Crash recovery and reconciliation
1. `systemctl --user status kalshi-edge-paper`; `journalctl --user -u kalshi-edge-paper -n 200`.
2. A crash loop stops after 5 starts in 10 minutes (unit enters `failed`): fix the cause, then
   `systemctl --user reset-failed kalshi-edge-paper && systemctl --user start kalshi-edge-paper`.
3. On start the trader takes the single-instance lock, runs a startup reconcile
   (`[paper] startup reconcile ok=True issues=[]`) and loads persisted risk state. It does not trade until the
   store is consistent. "refusing to start: another trader holds ..." (exit 2) means a live process holds
   `trader.lock`: find it with `pgrep -af "kalshi-edge run"`; the lock is released by the kernel if the process died.
4. SQLite corruption is rare (WAL, single writer). Stop the trader, `cp -a` the state dir, run
   `sqlite3 var/paper/kalshi_edge.sqlite3 "pragma integrity_check"`. Restore from backup if it fails.

## 9. Disk full
Symptoms: write errors in logs, SQLite "database or disk is full", recorder failures. `df -h .`, `du -sh var data reports`.
1. Engage the kill switch first (it needs only a tiny write; if that fails, stop the service).
2. Free space: compress/remove rotated logs (`var/*/trader.log.jsonl.*`), prune old `reports/` runs, move old
   `data/recorded/<date>` directories to other storage. Never delete `kalshi_edge.sqlite3` or `KILL_SWITCH`.
3. Restart, verify `startup reconcile ok=True`, run the healthcheck, then reset the kill switch if you engaged it.
4. Prevent: logrotate (DEPLOYMENT.md section 7), a disk alert at 80%.

## 10. Unexpected live-mode signals
If `status` ever shows `"live_trading_allowed_in_config": true` for a paper/demo config, or `doctor` warns that
production credential variables are set: stop the service, find who changed the config or environment, unset the
variables (`env | grep KALSHI_PROD` without printing values elsewhere), and treat it as a security incident.
Paper/demo ignore production credentials, but their presence in a non-live shell is itself a hygiene failure.

## 11. Clock skew
Quotes are timestamp-checked (`max_book_age_seconds`), so skew causes stale-feed halts or wrong staleness decisions.
1. `timedatectl status` (look for `System clock synchronized: yes`), `chronyc tracking` (offset should be
   well under 1 s), `chronyc sources -v`.
2. Install/enable chrony: `sudo apt install chrony && sudo systemctl enable --now chrony` (or the distro
   equivalent); `sudo chronyc makestep` to step a large offset (stop the trader first).
3. In VMs/containers fix the host clock; after sleep/resume wait for chrony to converge before restarting.
