#!/usr/bin/env bash
# kalshi-edge health check. Read-only: runs `kalshi-edge status` and inspects the JSON.
# Exit 0 = healthy; 1 = unhealthy (problems printed to stderr); 2 = could not run the check.
#
# Checks:
#   * kill switch not engaged
#   * no unresolved (UNKNOWN / ambiguous) orders
#   * risk state not halted (daily / weekly / drawdown)
#   * heartbeat fresh: newest of (latest event timestamp, SQLite db/WAL mtime) is younger than
#     --max-age seconds (default 600). The trader persists state every step, so a live process
#     keeps the DB mtime moving even when a step halts early (e.g. market_discovery_failed).
#   * live_trading_allowed_in_config is reported (informational; WARN if true in paper/demo)
#
# Usage: deploy/healthcheck.sh [--config configs/paper.yaml] [--max-age SECONDS]
# Cron:  */5 * * * * cd /ABS/PATH/TO/REPO && deploy/healthcheck.sh || notify-send "kalshi-edge unhealthy"
# systemd: deploy/systemd/kalshi-edge-health.{service,timer}
set -euo pipefail

CONFIG="configs/paper.yaml"
MAX_AGE=600
while [ $# -gt 0 ]; do
  case "$1" in
    --config|-c) CONFIG="${2:?missing value}"; shift 2 ;;
    --max-age) MAX_AGE="${2:?missing value}"; shift 2 ;;
    -h|--help) sed -n '2,17p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

STATUS_CMD="${KALSHI_EDGE_STATUS_CMD:-uv run --frozen kalshi-edge}"
# shellcheck disable=SC2086
if ! JSON="$(env -u UV_NATIVE_TLS $STATUS_CMD status --config "$CONFIG" 2>/dev/null)"; then
  echo "UNHEALTHY: 'kalshi-edge status' failed" >&2
  exit 2
fi

printf '%s' "$JSON" | python3 -I -c '
import json, os, sys, time
from datetime import datetime, timezone

max_age = float(sys.argv[1])
st = json.load(sys.stdin)
problems = []

ks = st.get("kill_switch", {})
if ks.get("engaged"):
    problems.append("kill switch ENGAGED: %s (since %s)" % (ks.get("reason"), ks.get("since")))
unresolved = st.get("unresolved_orders") or []
if unresolved:
    problems.append("unresolved orders: %s" % ", ".join(unresolved[:5]))
rs = st.get("risk_state") or {}
for k in ("daily_halt", "weekly_halt", "drawdown_kill"):
    if rs.get(k):
        problems.append("risk stop latched: %s" % k)
if st.get("mode") in ("paper", "demo") and st.get("live_trading_allowed_in_config"):
    problems.append("allow_live_trading is true in a %s config" % st.get("mode"))

if "store" in st:  # "no state yet"
    problems.append("no state yet: trader has not run with this config")
else:
    stamps = []
    for ev in st.get("recent_events") or []:
        try:
            stamps.append(datetime.fromisoformat(ev["ts"]).timestamp())
        except (KeyError, ValueError):
            pass
    db = os.path.join(st["state_dir"], "kalshi_edge.sqlite3")
    for f in (db, db + "-wal"):
        if os.path.exists(f):
            stamps.append(os.path.getmtime(f))
    if not stamps:
        problems.append("no heartbeat found")
    else:
        age = time.time() - max(stamps)
        if age > max_age:
            problems.append("heartbeat stale: %.0fs old (max %.0fs)" % (age, max_age))
        else:
            print("heartbeat age %.0fs" % age)

if problems:
    for p in problems:
        print("UNHEALTHY: " + p, file=sys.stderr)
    sys.exit(1)
print("OK mode=%s kill_switch=off unresolved=0" % st.get("mode"))
' "$MAX_AGE"
