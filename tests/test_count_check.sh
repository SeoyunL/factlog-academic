#!/usr/bin/env bash
# tests/test_count_check.sh — count() predicate in /factlog check (#55)
#
# Pins (no pyrewire needed — validate_query and evaluate_queries' count branch
# are pure; only run_logic_check.main() touches the engine):
#   - validate_query accepts count() (count is in QUERY_PREDICATES) and flags
#     wrong arity
#   - evaluate_queries counts DISTINCT objects for (subject, relation), matching
#     ask_router.evaluate semantics; duplicate objects (multi-source) collapse;
#     a (subject, relation) with no facts is a verified 0
#
# Synthetic data only. Usage: bash tests/test_count_check.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
cat > "$KB/facts/query.dl" <<'EOF'
count("갑봇", "포함")?
count("갑봇", "없는관계")?
EOF

verdict="$(FACTLOG_TEST_KB="$KB" FACTLOG_TEST_ROOT="$PLUGIN_ROOT" "$PYTHON" <<'PY'
import os, sys
os.environ["FACTLOG_ROOT"] = os.environ["FACTLOG_TEST_KB"]
sys.path.insert(0, os.path.join(os.environ["FACTLOG_TEST_ROOT"], "tools"))
import common as c
import run_logic_check as r

problems = []

# count is an accepted engine query predicate
if "count" not in c.QUERY_PREDICATES:
    problems.append("count not in QUERY_PREDICATES")

# validate_query: accepts count, flags wrong arity
e, _ = r.validate_query('count("갑봇", "포함")?', set(), set())
if e:
    problems.append(f"valid count rejected: {e}")
e2, _ = r.validate_query('count("갑봇")?', set(), set())
if not any("subject and relation" in x for x in e2):
    problems.append("count arity error not raised")

# evaluate_queries: distinct objects; duplicate (multi-source) collapses; 0 is valid
facts = [
    {"subject": "갑봇", "relation": "포함", "object": "값가"},
    {"subject": "갑봇", "relation": "포함", "object": "값나"},
    {"subject": "갑봇", "relation": "포함", "object": "값가"},  # duplicate object
    {"subject": "을서비스", "relation": "포함", "object": "값다"},  # different subject
]
res = r.evaluate_queries(facts, {}, set())
# Each count result line echoes the query it answers (#328): a malformed line is
# dropped from this list, so position alone no longer identifies the query.
if 'count results (query: count("갑봇", "포함")?): 2 (distinct objects)' not in res:
    problems.append(f"expected distinct count 2, got: {res}")
if 'count results (query: count("갑봇", "없는관계")?): 0 (distinct objects)' not in res:
    problems.append(f"expected verified 0 for absent relation, got: {res}")

print("OK" if not problems else "FAIL: " + " | ".join(problems))
PY
)"
echo "$verdict" | grep -q "^OK$" && ok "count: QUERY_PREDICATES + validate arity + distinct-object eval (incl. verified 0)" || bad "$verdict"

echo ""
echo "========================================"
echo "test_count_check: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
