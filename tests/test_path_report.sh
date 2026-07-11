#!/usr/bin/env bash
# The REPORT's path branch (#220). The unit tests call common.path_query_rows and
# ask_router directly, so reverting tools/run_logic_check.py's path branch to its old
# form left all 3079 of them green -- the body of the issue was not pinned at all.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${FACTLOG_PY:-${PYTHON:-python3}}"
export PYTHONPATH="$PWD"
fails=0
check() { if printf '%s' "$2" | grep -qF "$3"; then echo "  ok: $1"; else echo "FAIL: $1"; echo "     want: $3"; echo "     got : $2"; fails=$((fails+1)); fi; }

"$PY" -c "import pyrewire" >/dev/null 2>&1 || { echo "SKIP: pyrewire not installed"; exit 0; }

KB="$(mktemp -d)/kb"
export XDG_CONFIG_HOME="$(mktemp -d)"
"$PY" -m factlog init --target "$KB" >/dev/null || { echo "FAIL: init"; exit 1; }
printf 'a\n' > "$KB/sources/a.md"
{ printf 'subject,relation,object,source,status,confidence,note\n'
  printf 'A,uses,B,sources/a.md,accepted,0.9,\n'
  printf 'B,uses,C,sources/a.md,accepted,0.9,\n'; } > "$KB/facts/candidates.csv"
FACTLOG_ROOT="$KB" "$PY" tools/compile_facts.py >/dev/null || { echo "FAIL: compile"; exit 1; }

report() {
  printf '%s\n' "$1" > "$KB/facts/query.dl"
  FACTLOG_ROOT="$KB" "$PY" tools/run_logic_check.py >/dev/null 2>&1
  cat "$KB/facts/logic_report.txt"
}

R="$(report 'path("A", X)?')"
check "(a) a variable path query produces rows in the REPORT" "$R" "path results: 2 rows; A -> B; A -> C"
check "(b) it no longer claims the query file is missing" "$R" "Query evaluation:"
printf '%s' "$R" | grep -q "no facts/query.dl found" && { echo "FAIL: (b) still claims the file is missing"; fails=$((fails+1)); } || echo "  ok: (b) does not claim a present file is missing"

R="$(report 'path("A", "C")?')"
check "(c) a constant path query still renders the ROUTE" "$R" "path A -> C: A -> B -> C"

R="$(report 'path("A")?')"
check "(d) a malformed path query is an ERROR, not a verified negative" "$R" "path query must have start and target arguments"

R="$(report 'path(x, "C")?')"
check "(e) a bare token is an ERROR, like it is for relation" "$R" "path arguments must be variables or quoted strings"

# Report and ask both take reachability from the ENGINE, not from a private python
# closure -- that is what keeps them from disagreeing (#220). Since #226 reserved edge/
# path as engine predicates a policy cannot head, the engine's path and the python
# tracer are now the SAME computation (relation minus attr_rel), so they agree on every
# KB by construction. Here we just confirm report and ask give the same rows for a
# variable query, through the engine path.
ASK="$(FACTLOG_ROOT="$KB" "$PY" - <<'PYEOF'
import os, sys, json
sys.path.insert(0, os.getcwd()); sys.path.insert(0, "tools")
import factlog.common as c
from ask_router import evaluate
facts = c.load_accepted_facts()
print(json.dumps(sorted(evaluate('path("A", X)?', facts)["rows"])))
PYEOF
)"
R="$(report 'path("A", X)?')"
printf '%s' "$R" | grep -q "A -> B" && printf '%s' "$R" | grep -q "A -> C" \
  && [ "$ASK" = '[["A", "B"], ["A", "C"]]' ] \
  && echo "  ok: (i) report and ask give the same rows for a variable path query" \
  || { echo "FAIL: (i) report and ask disagree (ask=$ASK)"; fails=$((fails+1)); }

# A policy that HEADS edge is rejected -- that reservation (#226) is what makes the
# engine's path authoritative and un-forgeable, which is why report and ask can trust it.
printf 'edge(S, T) :- relation(T, "uses", S).\n' > "$KB/policy/logic-policy.extra.dl"
ERR="$(FACTLOG_ROOT="$KB" "$PY" tools/run_logic_check.py 2>&1 >/dev/null || true)"
printf '%s' "$ERR" | grep -q "edge is a reserved engine EDB predicate" \
  && echo "  ok: (j) a policy heading edge is rejected, keeping the engine path authoritative" \
  || { echo "FAIL: (j) a policy was allowed to redefine edge"; fails=$((fails+1)); }
rm -f "$KB/policy/logic-policy.extra.dl"

# The file exists but holds nothing evaluable — say that, not "not found".
: > "$KB/facts/query.dl"
FACTLOG_ROOT="$KB" "$PY" tools/run_logic_check.py >/dev/null 2>&1
check "(g) an empty query.dl says so" "$(cat "$KB/facts/logic_report.txt")" "facts/query.dl is empty"

rm -f "$KB/facts/query.dl"
FACTLOG_ROOT="$KB" "$PY" tools/run_logic_check.py >/dev/null 2>&1
check "(h) a genuinely absent query.dl still says not found" "$(cat "$KB/facts/logic_report.txt")" "no facts/query.dl found"

echo
if [ "$fails" -eq 0 ]; then echo "path report: all passed"; else echo "path report: $fails failed"; exit 1; fi
