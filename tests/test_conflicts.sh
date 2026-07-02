#!/usr/bin/env bash
# tests/test_conflicts.sh — single-valued contradiction detection + supersession (#30)
#
# Pins:
#   - a single-valued relation with 2 distinct objects for one subject = conflict
#   - marking the outdated row status='superseded' resolves it (non-destructive)
#   - a multi-valued (undeclared) relation with 2 objects is NOT a conflict
#   - no policy/single-valued.md => nothing to check (exit 0)
#   - 'superseded' rows survive merge and are excluded from accepted.dl
#   - validate.py accepts the 'superseded' status
#
# Deterministic; no pyrewire required.  Usage: bash tests/test_conflicts.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
HEADER="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
CONFLICTS="$PLUGIN_ROOT/tools/check_conflicts.py"
csv() { printf '%s\n' "$HEADER" "$@" > "$KB/facts/candidates.csv"; }
run_conflicts() { "$PYTHON" "$CONFLICTS" --wiki "$KB" >/dev/null 2>&1; }

# no single-valued.md yet -> nothing to check
if run_conflicts; then ok "no single-valued.md => exit 0 (nothing to check)"; else bad "should be 0 without single-valued.md"; fi

# declare 주_속성 single-valued
printf '# single-valued relations\n\n- 주_속성\n' > "$KB/policy/single-valued.md"

# multi-valued (undeclared) relation with 2 objects -> NOT a conflict
csv '갑봇,구성_요소,ToolA,sources/x.md,confirmed,0.9,' '갑봇,구성_요소,을서비스,sources/x.md,confirmed,0.9,'
printf '# x\n' > "$KB/sources/x.md"
if run_conflicts; then ok "multi-valued relation with 2 objects is not flagged"; else bad "multi-valued wrongly flagged"; fi

# single-valued relation with 2 distinct objects -> CONFLICT
csv '을서비스,주_속성,값가,sources/x.md,confirmed,0.9,' '을서비스,주_속성,값나,sources/x.md,confirmed,0.9,'
if run_conflicts; then bad "single-valued conflict NOT detected"; else ok "single-valued conflict detected (exit non-zero)"; fi
cout="$("$PYTHON" "$CONFLICTS" --wiki "$KB" 2>&1 || true)"
if printf '%s' "$cout" | grep -qF "CONFLICT"; then ok "conflict reported with CONFLICT line"; else bad "no CONFLICT line"; fi

# resolve by superseding the outdated row -> conflict clears (non-destructively)
csv '을서비스,주_속성,값가,sources/x.md,superseded,0.9,old' '을서비스,주_속성,값나,sources/x.md,confirmed,0.9,current'
if run_conflicts; then ok "superseding the outdated row resolves the conflict"; else bad "superseded row still conflicts"; fi

# superseded survives merge and is excluded from accepted.dl
printf '[{"subject":"을서비스","relation":"주_속성","object":"값가","source":"sources/x.md","status":"superseded","confidence":0.9,"note":"old"},{"subject":"을서비스","relation":"주_속성","object":"값나","source":"sources/x.md","status":"confirmed","confidence":0.9,"note":"current"}]' > "$KB/runs/r.json"
"$PYTHON" "$PLUGIN_ROOT/tools/merge_candidates.py" --wiki "$KB" >/dev/null 2>&1 || true
if grep -q ',superseded,' "$KB/facts/candidates.csv"; then ok "merge preserves superseded status"; else bad "merge dropped/renamed superseded status"; fi
FACTLOG_ROOT="$KB" "$PYTHON" "$PLUGIN_ROOT/tools/compile_facts.py" >/dev/null 2>&1
if grep -q '"값가"' "$KB/facts/accepted.dl"; then bad "superseded fact leaked into accepted.dl"; else ok "superseded fact excluded from accepted.dl"; fi
if grep -q '"값나"' "$KB/facts/accepted.dl"; then ok "current fact compiled to accepted.dl"; else bad "current fact missing from accepted.dl"; fi

# H1: a human-marked superseded row is PRESERVED across a re-merge even when the
# originating run JSON re-asserts it as confirmed (resolution is durable).
csv '을서비스,주_속성,값가,sources/x.md,superseded,0.9,old' '을서비스,주_속성,값나,sources/x.md,confirmed,0.9,current'
printf '[{"subject":"을서비스","relation":"주_속성","object":"값가","source":"sources/x.md","status":"confirmed","confidence":0.9,"note":"re-asserted"}]' > "$KB/runs/r.json"
"$PYTHON" "$PLUGIN_ROOT/tools/merge_candidates.py" --wiki "$KB" >/dev/null 2>&1 || true
if grep -q '을서비스,주_속성,값가,.*,superseded,' "$KB/facts/candidates.csv"; then ok "re-merge preserves human-marked superseded (durable resolution)"; else bad "re-merge reverted the superseded row"; fi
if run_conflicts; then ok "conflict stays resolved after re-merge"; else bad "conflict reappeared after re-merge"; fi
rm -f "$KB/runs/r.json"

# M1: single-valued name parses from a backtick token with a trailing description
printf '# single-valued\n\n- `주_속성` (the base LLM)\n' > "$KB/policy/single-valued.md"
got="$("$PYTHON" -c "import sys; sys.path.insert(0,'$PLUGIN_ROOT/tools'); import os; os.environ['FACTLOG_ROOT']='$KB'; import common; print('주_속성' in common.single_valued_relations())")"
[ "$got" = "True" ] && ok "single-valued name parsed from backtick+description line" || bad "backtick+description line mis-parsed (false negative)"
printf '# single-valued\n\n- 주_속성\n' > "$KB/policy/single-valued.md"

# validate.py accepts the superseded status (no invalid-status error)
if "$PYTHON" "$PLUGIN_ROOT/tools/validate.py" "$KB" 2>&1 | grep -q "invalid status"; then bad "validate rejected superseded status"; else ok "validate accepts superseded status"; fi

# --- #204: typed single-valued amount compares on the canonical scalar, not the
# raw string, so equivalent notations (억 ↔ 조) don't false-positive while real
# value differences still fire and the report preserves the original notation.
printf '# single-valued\n\n- 주_속성\n- 매출\n' > "$KB/policy/single-valued.md"
printf -- '- `매출` : amount as revenue\n' > "$KB/policy/typed-relations.md"

# equivalent notations of the same value -> NOT a conflict (1 value)
csv '갑사,매출,"amount(5400,""억"")",sources/x.md,confirmed,0.9,' '갑사,매출,"amount(0.54,""조"")",sources/x.md,confirmed,0.9,'
if run_conflicts; then ok "typed amount: equivalent notations (5400억 == 0.54조) not flagged"; else bad "typed amount: equivalent notations wrongly flagged as conflict"; fi

# real value difference -> CONFLICT, and the message keeps the ORIGINAL notation
csv '갑사,매출,"amount(5000,""억"")",sources/x.md,confirmed,0.9,' '갑사,매출,"amount(5400,""억"")",sources/x.md,confirmed,0.9,'
if run_conflicts; then bad "typed amount: real value difference (5000억 vs 5400억) NOT detected"; else ok "typed amount: real value difference detected"; fi
cout="$("$PYTHON" "$CONFLICTS" --wiki "$KB" 2>&1 || true)"
if printf '%s' "$cout" | grep -qF 'amount(5000,"억")' && printf '%s' "$cout" | grep -qF 'amount(5400,"억")'; then ok "typed amount: report preserves original notation (provenance)"; else bad "typed amount: report lost original notation"; fi

# unparseable typed object degrades to the raw-string key (still detected)
csv '갑사,매출,"amount(5400,""억"")",sources/x.md,confirmed,0.9,' '갑사,매출,not-a-number,sources/x.md,confirmed,0.9,'
if run_conflicts; then bad "typed amount: parseable-vs-unparseable difference NOT detected"; else ok "typed amount: unparseable object degrades to raw key (detected)"; fi

# restore the baseline single-valued policy for any later additions
printf '# single-valued\n\n- 주_속성\n' > "$KB/policy/single-valued.md"
rm -f "$KB/policy/typed-relations.md"

echo ""
echo "========================================"
echo "test_conflicts: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
