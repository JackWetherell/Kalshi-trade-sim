#!/usr/bin/env bash
# Offline paper smoke test. Needs no network, no credentials, no production key.
# Uses a TEMP COPY of configs/paper.yaml with state_dir redirected to a temp dir, so repo state
# (var/) is never touched. In a network-restricted container every step reports
# `halted: market_discovery_failed` (fail-closed); that counts as PASS here.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
KE="env -u UV_NATIVE_TLS uv run --frozen kalshi-edge"
fail() { echo "FAIL: $*" >&2; exit 1; }

sed "s#state_dir: var/paper#state_dir: $TMP/state#" configs/paper.yaml > "$TMP/paper.yaml"
grep -q "$TMP/state" "$TMP/paper.yaml" || fail "could not redirect state_dir"
# never let the smoke test see a production credential
unset KALSHI_PROD_KEY_ID KALSHI_PROD_PRIVATE_KEY_PATH

echo "== 1. run --mode paper --steps 2 --interval 0"
$KE run --mode paper --config "$TMP/paper.yaml" --steps 2 --interval 0 >"$TMP/run.out" 2>"$TMP/run.err" \
  || fail "paper run exited nonzero"
grep '^{"mode"' "$TMP/run.out" >"$TMP/steps.jsonl" || true
[ "$(wc -l <"$TMP/steps.jsonl")" -eq 2 ] || fail "expected 2 step lines, got $(wc -l <"$TMP/steps.jsonl")"
python3 -I - "$TMP/steps.jsonl" <<'PY' || fail "step output invalid"
import json, sys
for line in open(sys.argv[1]):
    d = json.loads(line)
    assert d["mode"] == "paper", d
    assert d["submitted"] == 0, d          # nothing may be submitted offline
    assert "error" not in d, d
PY
echo "ok: 2 valid paper step records"

echo "== 2. status JSON"
$KE status --config "$TMP/paper.yaml" >"$TMP/status.json" || fail "status failed"
python3 -I - "$TMP/status.json" <<'PY' || fail "status JSON invalid"
import json, sys
s = json.load(open(sys.argv[1]))
assert s["mode"] == "paper" and s["live_trading_allowed_in_config"] is False
assert s["kill_switch"]["engaged"] is False
assert s["unresolved_orders"] == [] and s["open_orders"] == []
PY
echo "ok: status healthy, kill switch off"

echo "== 3. engage kill switch"
$KE kill --config "$TMP/paper.yaml" --reason "smoke test" >/dev/null || fail "kill failed"
$KE status --config "$TMP/paper.yaml" | python3 -I -c '
import json, sys
s = json.load(sys.stdin)
assert s["kill_switch"]["engaged"] is True and s["kill_switch"]["reason"] == "smoke test", s["kill_switch"]
' || fail "kill switch not engaged"
$KE run --mode paper --config "$TMP/paper.yaml" --steps 1 --interval 0 2>/dev/null | grep '^{"mode"' \
  | python3 -I -c '
import json, sys
d = json.loads(sys.stdin.readline())
assert d["halted"].startswith("kill_switch"), d
' || fail "run did not halt on kill switch"
echo "ok: kill switch engaged and honoured"

echo "== 4. single-instance lock"
python3 -I - "$TMP/state/trader.lock" <<'PY' &
import fcntl, os, sys, time
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
print("locked", flush=True)
time.sleep(30)
PY
HOLDER=$!
for _ in $(seq 50); do sleep 0.1; kill -0 "$HOLDER" 2>/dev/null || break; [ -s "$TMP/state/trader.lock" ] && break; done
sleep 0.5
set +e
$KE run --mode paper --config "$TMP/paper.yaml" --steps 1 --interval 0 >"$TMP/second.out" 2>&1
RC=$?
set -e
kill "$HOLDER" 2>/dev/null || true
wait "$HOLDER" 2>/dev/null || true
[ "$RC" -eq 2 ] || fail "second instance should exit 2, got $RC"
grep -q "refusing to start" "$TMP/second.out" || fail "missing 'refusing to start' message"
echo "ok: second instance refused (exit 2)"

echo "== 5. live is not startable non-interactively"
sed "s#state_dir: var/live#state_dir: $TMP/live#" configs/live.example.yaml >"$TMP/live.yaml"
set +e
$KE run --mode live --config "$TMP/live.yaml" --steps 1 </dev/null >"$TMP/live.out" 2>&1
RC=$?
set -e
[ "$RC" -ne 0 ] || fail "live run unexpectedly succeeded"
grep -q "NOT ARMED" "$TMP/live.out" || fail "expected NOT ARMED"
echo "ok: live refused (exit $RC, NOT ARMED)"

echo "PAPER SMOKE PASSED"
