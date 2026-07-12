#!/usr/bin/env bash
# A policy finding whose reason holds an escaped quote must print its TEXT, not a bare
# integer (#250). run_wirelog interns policy string literals so the engine's symbol id
# decodes back to text; the old findall interned the wrong pieces for an escaped literal,
# so the id had no entry and printed raw.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${FACTLOG_PY:-${PYTHON:-python3}}"
export PYTHONPATH="$PWD"
fails=0
check() { if printf '%s' "$2" | grep -qF "$3"; then echo "  ok: $1"; else echo "FAIL: $1"; echo "  want: $3"; echo "  got : $2"; fails=$((fails+1)); fi; }

"$PY" -c "import pyrewire" >/dev/null 2>&1 || { echo "SKIP: pyrewire not installed"; exit 0; }

KB="$(mktemp -d)/kb"
export XDG_CONFIG_HOME="$(mktemp -d)"
"$PY" -m factlog init --target "$KB" >/dev/null || { echo "FAIL: init"; exit 1; }
printf 'a\n' > "$KB/sources/a.md"
printf 'subject,relation,object,source,status,confidence,note\nWidget,status,active,sources/a.md,accepted,0.9,\n' > "$KB/facts/candidates.csv"
printf '.decl flagged(entity: symbol, reason: symbol)\nflagged(X, "size 5\\" bolt") :- relation(X, "status", "active").\n' > "$KB/policy/logic-policy.extra.dl"
FACTLOG_ROOT="$KB" "$PY" tools/compile_facts.py >/dev/null || { echo "FAIL: compile"; exit 1; }
FACTLOG_ROOT="$KB" "$PY" tools/run_logic_check.py >/dev/null || { echo "FAIL: run_logic_check"; exit 1; }
REPORT="$(cat "$KB/facts/logic_report.txt")"

check "(a) the escaped-quote reason prints its text" "$REPORT" 'flagged: Widget (size 5" bolt)'
# the bug printed a bare integer for the reason
printf '%s' "$REPORT" | grep -qE 'flagged: Widget \([0-9]+\)$' \
  && { echo "FAIL: (b) the reason printed as a raw integer"; fails=$((fails+1)); } \
  || echo "  ok: (b) the reason did not print as a raw integer"

# (c) a NON-json escape (\t) must ALSO print (backslash preserved), not a bare integer.
# json.loads would decode \t to a tab, mismatch the engine's stored symbol, and reprint
# the id -- the regression the first cut introduced (#250 review).
KB2="$(mktemp -d)/kb"
"$PY" -m factlog init --target "$KB2" >/dev/null || { echo "FAIL: init2"; exit 1; }
printf 'a\n' > "$KB2/sources/a.md"
printf 'subject,relation,object,source,status,confidence,note\nWidget,status,active,sources/a.md,accepted,0.9,\n' > "$KB2/facts/candidates.csv"
printf '.decl flagged(entity: symbol, reason: symbol)\nflagged(X, "col\\tval") :- relation(X, "status", "active").\n' > "$KB2/policy/logic-policy.extra.dl"
FACTLOG_ROOT="$KB2" "$PY" tools/compile_facts.py >/dev/null 2>&1
FACTLOG_ROOT="$KB2" "$PY" tools/run_logic_check.py >/dev/null 2>&1
R2="$(cat "$KB2/facts/logic_report.txt")"
# positive assertion: the text must be PRESENT (a weaker "no bare int" line passes
# vacuously if the finding is missing entirely).
check "(c) a non-json escape (\\t) reason prints its text, backslash preserved" "$R2" 'flagged: Widget (col\tval)' 

# (d) an odd quote in a COMMENT must not shift literal boundaries and hide the finding.
KB3="$(mktemp -d)/kb"
"$PY" -m factlog init --target "$KB3" >/dev/null || { echo "FAIL: init3"; exit 1; }
printf 'a\n' > "$KB3/sources/a.md"
printf 'subject,relation,object,source,status,confidence,note\nWidget,status,active,sources/a.md,accepted,0.9,\n' > "$KB3/facts/candidates.csv"
printf '.decl flagged(entity: symbol, reason: symbol)\n// the 5" bolt rule\nflagged(X, "real") :- relation(X, "status", "active").\n' > "$KB3/policy/logic-policy.extra.dl"
FACTLOG_ROOT="$KB3" "$PY" tools/compile_facts.py >/dev/null 2>&1
FACTLOG_ROOT="$KB3" "$PY" tools/run_logic_check.py >/dev/null 2>&1
R3="$(cat "$KB3/facts/logic_report.txt")"
check "(d) an odd quote in a comment does not hide the finding" "$R3" 'flagged: Widget (real)'

echo
if [ "$fails" -eq 0 ]; then echo "policy escape report: all passed"; else echo "policy escape report: $fails failed"; exit 1; fi
