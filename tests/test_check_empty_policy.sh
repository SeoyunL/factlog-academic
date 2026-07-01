#!/usr/bin/env bash
# tests/test_check_empty_policy.sh — a freshly `init`ed KB with no policy rules
# must let `/factlog check` (run_logic_check) complete, not hard-fail (#190).
#
# Repro: init leaves policy/logic-policy.md as prose (no compilable bullets) and
# writes NO policy/logic-policy.dl. Before the fix, run_wirelog -> load_logic_policy
# raised FactlogError and check exited 1, while `ask` was already graceful. This
# pins the asymmetry fix:
#   - empty/prose policy + absent .dl  -> check completes, `policy findings: 0`
#   - ask stays consistent (0 policy predicates) on the same KB
#   - rules-in-.md but absent .dl      -> still fails loud (do not drop the policy)
#   - none of the three loader errors point at `init --target <kb> --force`
#
# Usage: bash tests/test_check_empty_policy.sh  -> 0 if all pass, 1 otherwise.

set -uo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
RLC="$PLUGIN_ROOT/tools/run_logic_check.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# Skip cleanly if the engine is absent (offline installs): the graceful-completion
# assertions need pyrewire to actually run the check.
if ! "$PYTHON" -c "import pyrewire" >/dev/null 2>&1; then
  echo "SKIP: pyrewire not installed; test_check_empty_policy requires the engine"
  exit 0
fi

make_kb() {
  local kb="$1"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null 2>&1
  # accepted.dl present (as after compile), candidates.csv present — but NO
  # policy/logic-policy.dl and a prose-only logic-policy.md (init defaults).
  printf 'subject,relation,object,source,status,confidence,note\nA,uses,B,sources/x.md,confirmed,0.9,\n' > "$kb/facts/candidates.csv"
  printf 'relation("A", "uses", "B").\n' > "$kb/facts/accepted.dl"
}

# --- 1. empty policy → check completes with 0 findings ------------------------
KB="$(mktemp -d)/wiki"
make_kb "$KB"
[ ! -f "$KB/policy/logic-policy.dl" ] || bad "precondition: logic-policy.dl should be absent"

out="$(FACTLOG_ROOT="$KB" "$PYTHON" "$RLC" 2>&1)"; rc=$?
[ "$rc" -eq 0 ] && ok "run_logic_check exits 0 on a no-policy KB (#190)" || bad "run_logic_check exited $rc: $out"
[ -f "$KB/facts/logic_report.txt" ] && ok "logic_report.txt produced" || bad "logic_report.txt missing"
grep -qF "policy findings: 0" "$KB/facts/logic_report.txt" && ok "report shows 'policy findings: 0'" || bad "report missing 'policy findings: 0'"

# --- 2. ask/check consistency: 0 policy predicates on the same KB -------------
preds="$(FACTLOG_ROOT="$KB" "$PYTHON" -c "from factlog import common; print(len(common.policy_predicates(common.load_logic_policy())))" 2>&1)"
[ "$preds" = "0" ] && ok "ask path sees 0 policy predicates (empty policy)" || bad "expected 0 policy predicates, got: $preds"

# --- 3. genuine error not swallowed: rules in .md but no .dl ------------------
KB2="$(mktemp -d)/wiki"
make_kb "$KB2"
printf '# Logic policy\n\n## Rules\n\n- [c1] flag when `requires_review`\n' > "$KB2/policy/logic-policy.md"
out2="$(FACTLOG_ROOT="$KB2" "$PYTHON" "$RLC" 2>&1)"; rc2=$?
[ "$rc2" -ne 0 ] && ok "uncompiled rules still fail loud (not swallowed)" || bad "uncompiled rules should fail, got rc=$rc2"
printf '%s' "$out2" | grep -qF "generate_logic_policy" && ok "error points at generate_logic_policy (or /factlog add)" || bad "error did not guide to generate_logic_policy: $out2"
printf '%s' "$out2" | grep -qF -- "--force" && bad "error still mentions --force" || ok "error does not mention --force"

# --- 4. no loader error points at the nonexistent 'init --force' flag ---------
if grep -rn -- "init --target <kb> --force" "$PLUGIN_ROOT/factlog/common.py" >/dev/null 2>&1; then
  bad "common.py still references 'init --target <kb> --force'"
else
  ok "no 'init --target <kb> --force' guidance remains in common.py"
fi

echo
echo "========================================"
echo "test_check_empty_policy: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
