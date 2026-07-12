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

echo
if [ "$fails" -eq 0 ]; then echo "policy escape report: all passed"; else echo "policy escape report: $fails failed"; exit 1; fi
