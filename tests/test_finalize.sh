#!/usr/bin/env bash
# tests/test_finalize.sh — one-shot deterministic finalize chain (#29)
#
# After extraction writes runs/*.json, `finalize.py` chains merge -> ensure
# policy -> compile -> (logic check). This pins:
#   - candidates.csv and accepted.dl are produced (pyrewire-independent)
#   - policy/logic-policy.dl is ensured so the check can load (stub if no rules)
#   - with pyrewire>=1.0.1: logic_report.txt is produced; without it the check
#     is skipped gracefully (no hard failure) and facts are still compiled
#   - idempotent: re-running does not duplicate the fact
#
# Usage: bash tests/test_finalize.sh  -> 0 if all pass, 1 otherwise.

set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
FINALIZE="$PLUGIN_ROOT/tools/finalize.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf '# src\n\nAcme API uses FastAPI.\n' > "$KB/sources/acme.md"
printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/acme.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB/runs/r1.json"

out="$("$PYTHON" "$FINALIZE" --target "$KB" 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ok "finalize exits 0" || bad "finalize exited $rc"

[ -s "$KB/facts/candidates.csv" ] && ok "candidates.csv produced" || bad "no candidates.csv"
if [ -f "$KB/facts/accepted.dl" ] && grep -q 'relation("Acme API", "uses", "FastAPI")' "$KB/facts/accepted.dl"; then ok "accepted.dl compiled with the fact"; else bad "accepted.dl missing the fact"; fi
[ -f "$KB/policy/logic-policy.dl" ] && ok "policy/logic-policy.dl ensured" || bad "policy/logic-policy.dl not ensured"

if "$PYTHON" -c "import pyrewire; raise SystemExit(0 if tuple(int(x) for x in pyrewire.__version__.split('.')[:3])>=(1,0,1) else 1)" >/dev/null 2>&1; then
  [ -f "$KB/facts/logic_report.txt" ] && ok "logic_report.txt produced (pyrewire present)" || bad "logic_report.txt missing despite pyrewire"
  printf '%s' "$out" | grep -qF "logic-checked" && ok "summary reports logic-checked" || bad "summary missing logic-checked"
else
  printf '%s' "$out" | grep -qF "Logic check SKIPPED" && ok "logic check skipped gracefully without pyrewire" || bad "no graceful-skip note without pyrewire"
fi

# idempotency: re-run must not duplicate the fact
"$PYTHON" "$FINALIZE" --target "$KB" >/dev/null 2>&1 || true
n="$(grep -c 'relation("Acme API", "uses", "FastAPI")' "$KB/facts/accepted.dl")"
[ "$n" = "1" ] && ok "idempotent re-run (fact not duplicated)" || bad "re-run duplicated the fact ($n)"

echo ""
echo "========================================"
echo "test_finalize: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
