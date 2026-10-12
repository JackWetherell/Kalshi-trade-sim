#!/usr/bin/env bash
# Demo smoke test. Requires HUMAN-PROVISIONED DEMO-ONLY credentials in the environment
# (KALSHI_DEMO_KEY_ID, KALSHI_DEMO_PRIVATE_KEY_PATH). Without them it SKIPS (exit 0) and never
# pretends to have tested anything. It never reads or prints credential values and unsets any
# production credential variables.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
unset KALSHI_PROD_KEY_ID KALSHI_PROD_PRIVATE_KEY_PATH

if [ -z "${KALSHI_DEMO_KEY_ID:-}" ] || [ -z "${KALSHI_DEMO_PRIVATE_KEY_PATH:-}" ]; then
  echo "SKIPPED: no demo credentials"
  exit 0
fi
if [ ! -r "$KALSHI_DEMO_PRIVATE_KEY_PATH" ]; then
  echo "FAIL: KALSHI_DEMO_PRIVATE_KEY_PATH is not a readable file" >&2
  exit 1
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
sed "s#state_dir: var/demo#state_dir: $TMP/state#" configs/demo.yaml >"$TMP/demo.yaml"
echo "== demo: 1 step against the Kalshi DEMO environment (mock funds)"
env -u UV_NATIVE_TLS uv run --frozen kalshi-edge run --mode demo --config "$TMP/demo.yaml" --steps 1 --interval 0 \
  | tee "$TMP/out.txt"
grep '^{"mode"' "$TMP/out.txt" | python3 -I -c '
import json, sys
d = json.loads(sys.stdin.readline())
assert d["mode"] == "demo" and "error" not in d, d
print("demo step record valid; halted=%s" % d.get("halted"))
'
env -u UV_NATIVE_TLS uv run --frozen kalshi-edge status --config "$TMP/demo.yaml" >/dev/null
echo "DEMO SMOKE PASSED (1 step; strategy C without an estimator produces NO TRADE by design)"
