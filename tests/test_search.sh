#!/usr/bin/env bash
# tests/test_search.sh — `factlog search` fuzzy/substring discovery (#85)
#
# Pins (XDG-isolated; synthetic data):
#   - case-insensitive substring match (term 'fast' matches object 'FastAPI')
#   - matches across subject OR relation OR object
#   - groups distinct facts with their statuses + distinct-source count
#   - all statuses are searched (a superseded/needs_review fact is found)
#   - a multi-source fact reports its distinct-source count
#   - no match -> rc 1; empty term -> rc 2; non-KB path errors
#
# Usage: bash tests/test_search.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
H="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'a\n' > "$KB/sources/a.md"
printf 'b\n' > "$KB/sources/b.md"
printf '%s\n%s\n%s\n%s\n%s\n' "$H" \
  'Acme API,uses,FastAPI,sources/a.md,confirmed,0.9,' \
  'Acme API,uses,FastAPI,sources/b.md,confirmed,0.9,' \
  'Acme API,depends_on,Postgres,sources/a.md,needs_review,0.5,' \
  'Globex,retired_rel,OldThing,sources/a.md,superseded,0.8,' > "$KB/facts/candidates.csv"

# --- case-insensitive substring on the OBJECT --------------------------------
out="$("$PYTHON" -m factlog search fast --target "$KB" 2>&1)"
printf '%s\n' "$out"; echo "---"
printf '%s' "$out" | grep -qF "Acme API / uses / FastAPI" && ok "case-insensitive substring ('fast' -> 'FastAPI')" || bad "object substring match failed"
printf '%s' "$out" | grep -qF "1 fact(s) matching" && ok "duplicate-source rows collapse to one fact" || bad "fact grouping wrong"
printf '%s' "$out" | grep -qF "(2 sources)" && ok "multi-source fact reports distinct-source count" || bad "source count wrong"
printf '%s' "$out" | grep -qF "[confirmed]" && ok "shows the fact's status" || bad "status missing"

# --- match on the SUBJECT ----------------------------------------------------
"$PYTHON" -m factlog search acme --target "$KB" 2>&1 | grep -qF "2 fact(s) matching" && ok "subject substring matches both Acme facts" || bad "subject match wrong"

# --- match on the RELATION ---------------------------------------------------
"$PYTHON" -m factlog search depends --target "$KB" 2>&1 | grep -qF "Acme API / depends_on / Postgres" && ok "relation substring match" || bad "relation match failed"

# --- all statuses searched (needs_review + superseded found) ------------------
"$PYTHON" -m factlog search postgres --target "$KB" 2>&1 | grep -qF "[needs_review]" && ok "needs_review fact is searchable" || bad "needs_review not searched"
"$PYTHON" -m factlog search oldthing --target "$KB" 2>&1 | grep -qF "[superseded]" && ok "superseded fact is searchable" || bad "superseded not searched"

# --- no match / empty term / non-KB ------------------------------------------
set +e
"$PYTHON" -m factlog search zzz-nope --target "$KB" >/dev/null 2>&1; [ $? -eq 1 ] && ok "no match exits rc 1" || bad "no-match rc wrong"
"$PYTHON" -m factlog search "" --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "empty term exits rc 2" || bad "empty-term rc wrong"
"$PYTHON" -m factlog search acme --target "$(mktemp -d)" >/dev/null 2>&1; [ $? -ne 0 ] && ok "search on a non-KB path errors" || bad "non-KB path should error"
set -e

echo ""
echo "========================================"
echo "test_search: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
