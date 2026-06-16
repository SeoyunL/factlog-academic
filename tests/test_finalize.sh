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

# idempotency with a REAL compilable policy: generate_logic_policy writes
# runs/natural-language-to-policy-response.json (a JSON object); the SECOND
# finalize must not choke on it at the merge step.
KB2="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB2" >/dev/null
printf '# src\n\nAcme API deployed on AWS.\n' > "$KB2/sources/d.md"
printf '[{"subject":"Acme API","relation":"deployed_on","object":"AWS","source":"sources/d.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB2/runs/r1.json"
printf '# Logic policy\n\n## Rules\n\n- [hosting_check] 어떤 항목이 `deployed_on` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB2/policy/logic-policy.md"
"$PYTHON" "$FINALIZE" --target "$KB2" >/dev/null 2>&1; r1=$?
"$PYTHON" "$FINALIZE" --target "$KB2" >/dev/null 2>&1; r2=$?
if [ "$r1" -eq 0 ] && [ "$r2" -eq 0 ]; then ok "idempotent with a real policy (2nd finalize survives policy-response JSON in runs/)"; else bad "policy-rule KB: finalize not idempotent (rc1=$r1 rc2=$r2)"; fi
[ -f "$KB2/policy/logic-policy.dl" ] && grep -q "requires_review" "$KB2/policy/logic-policy.dl" && ok "real policy compiled (not stubbed over)" || bad "real policy not compiled"

echo ""
echo "========================================"
echo "test_finalize: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
