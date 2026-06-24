#!/usr/bin/env bash
# Egress-invariant test — the property that makes a tool-using reviewer safe.
# Opens a review-posture bubble (same flags as review_in_bubble) and asserts the network boundary:
#   - the TauCeti-scoped auth proxy is reachable  (PROXY_OK)
#   - arbitrary internet egress is denied         (EGRESS_BLOCKED)
# Needs bubble + Colima up and `gh` logged in. Run from the repo root. Re-run when the bubble image
# or security policy changes, and once review gains tool use.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
PY=$(command -v python3)
WID="egress-test"
export TAUCETI_WORKER_ID="$WID"

if ! command -v bubble >/dev/null 2>&1; then
  echo "  [SKIP] bubble not on PATH — egress invariant is bubble-only"; exit 0
fi

echo "== egress invariant: proxy reachable, arbitrary egress denied =="
out=$("$PY" ./tauceti _egress-probe --worker-id "$WID" 2>&1)
rc=$?
echo "$out" | sed 's/^/    /'

pass=0; fail=0
echo "$out" | grep -q "PROXY_OK"      && { echo "  [PASS] TauCeti proxy reachable"; pass=$((pass+1)); } \
                                       || { echo "  [FAIL] proxy not reachable (PROXY_OK missing)"; fail=$((fail+1)); }
echo "$out" | grep -q "EGRESS_BLOCKED" && { echo "  [PASS] arbitrary egress denied"; pass=$((pass+1)); } \
                                       || { echo "  [FAIL] arbitrary egress NOT denied (EGRESS_BLOCKED missing)"; fail=$((fail+1)); }
echo "$out" | grep -q "EGRESS_LEAK"    && { echo "  [FAIL] example.com was REACHABLE — egress leak!"; fail=$((fail+1)); }

echo
echo "egress: $pass passed, $fail failed (probe rc=$rc)"
exit $(( fail > 0 ))
