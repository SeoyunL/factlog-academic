#!/usr/bin/env bash
# tests/test_ask_router.sh — deterministic /factlog ask routing core
#
# Proves the reason-class routing and relation evaluation of tools/ask_router.py:
#   - matching relation            -> route=engine, negative=false
#   - accepted vocab, fact absent  -> route=engine, negative=TRUE (verified
#                                     negative — NEVER wiki)
#   - unknown entity/predicate/no '?' -> route=wiki
#   - review_required predicate    -> route=wiki
#   - works with NO compiled policy (fresh KB), i.e. no hard exit
#   - evaluate returns matching rows / 0 rows
#   - render emits the greppable VERIFIED — engine marker (positive & negative)
#   - ask_router never writes facts/query.dl or mutates facts/accepted.dl
#
# Runs from the working tree via PYTHONPATH (no install / no pyrewire needed for
# the relation path).
#
# Usage: bash tests/test_ask_router.sh
#   Returns 0 if all checks pass, 1 if any fail.

set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
ROUTER="$PLUGIN_ROOT/tools/ask_router.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# A minimal KB with two accepted relation facts and NO compiled policy
# (policy/logic-policy.dl intentionally absent — ask must tolerate it).
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf '// test\nrelation("Acme API", "uses", "FastAPI").\nrelation("Acme API", "depends_on", "Postgres").\n' \
  > "$KB/facts/accepted.dl"
ACCEPTED_BEFORE="$(cat "$KB/facts/accepted.dl")"

router() { "$PYTHON" "$ROUTER" "$@" --target "$KB"; }

# field <json> <key> : print a top-level JSON value
field() { "$PYTHON" -c "import json,sys; print(json.load(sys.stdin).get(sys.argv[1]))" "$1"; }

check_field() {  # check_field <desc> <subcmd> <draft> <key> <expected>
  local desc="$1" sub="$2" draft="$3" key="$4" expected="$5"
  local got; got="$(router "$sub" "$draft" | field "$key")"
  if [ "$got" = "$expected" ]; then ok "$desc ($key=$got)"; else bad "$desc — expected $key=$expected, got $got"; fi
}

# --- routing classification ---
check_field "matching relation routes engine" validate 'relation("Acme API", "uses", V)?' route engine
check_field "matching relation not negative"  validate 'relation("Acme API", "uses", V)?' negative False
check_field "absent fact = verified negative (engine, not wiki)" validate 'relation("Acme API", "uses", "Postgres")?' route engine
check_field "absent fact flagged negative"    validate 'relation("Acme API", "uses", "Postgres")?' negative True
check_field "unknown entity routes wiki"      validate 'relation("Nope", "uses", V)?' route wiki
check_field "unknown predicate routes wiki"   validate 'bogus("Acme API")?' route wiki
check_field "missing question mark routes wiki" validate 'relation("Acme API", "uses", V)' route wiki
check_field "review_required routes wiki"     validate 'review_required("why does it matter?")?' route wiki

# --- tolerance of missing compiled policy ---
if router validate 'relation("Acme API", "uses", V)?' >/dev/null 2>&1; then
  ok "validate works with no policy/logic-policy.dl (no hard exit)"
else
  bad "validate hard-failed on a KB without compiled policy"
fi

# --- evaluation ---
check_field "evaluate matching returns 1 row" evaluate 'relation("Acme API", "uses", V)?' count 1
check_field "evaluate non-matching returns 0 rows" evaluate 'relation("Acme API", "uses", "Nope")?' count 0

# --- render markers ---
if router render 'relation("Acme API", "uses", V)?' | grep -qF "VERIFIED — engine"; then ok "render positive carries VERIFIED — engine marker"; else bad "render positive missing VERIFIED marker"; fi
if router render 'relation("Acme API", "uses", V)?' | grep -qF "Acme API, uses, FastAPI"; then ok "render positive shows the matched row"; else bad "render positive missing matched row"; fi
neg="$(router render 'relation("Acme API", "uses", "Postgres")?')"
if printf '%s' "$neg" | grep -qF "VERIFIED — engine" && printf '%s' "$neg" | grep -qF "verified negative"; then ok "render verified-negative is engine-marked"; else bad "render verified-negative not engine-marked"; fi

# --- read-only invariant ---
if [ -f "$KB/facts/query.dl" ]; then bad "ask_router wrote facts/query.dl (must be read-only)"; else ok "facts/query.dl never written"; fi
if [ "$(cat "$KB/facts/accepted.dl")" = "$ACCEPTED_BEFORE" ]; then ok "facts/accepted.dl unchanged"; else bad "facts/accepted.dl was mutated"; fi

echo ""
echo "========================================"
echo "test_ask_router: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
