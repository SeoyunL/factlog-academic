#!/usr/bin/env bash
# #227: a typed literal that does not parse must appear in facts/logic_report.txt.
# The unit test alone cannot catch this — it passes even when the collector is
# never wired into the report, which was the bug. This pins the report itself.
set -uo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
PY="${FACTLOG_PY:-${PYTHON:-python3}}"
export PYTHONPATH="$ROOT"
fails=0
check() { if [ "$2" = "$3" ]; then echo "  ok: $1"; else echo "FAIL: $1 (want $3, got $2)"; fails=$((fails+1)); fi; }

KB="$(mktemp -d)/kb"
export XDG_CONFIG_HOME="$(mktemp -d)"
"$PY" -m factlog init --target "$KB" >/dev/null || { echo "FAIL: init failed"; exit 1; }
printf 'a\n' > "$KB/sources/a.md"
printf 'subject,relation,object,source,status,confidence,note\n' > "$KB/facts/candidates.csv"
printf 'A,league_rank,rank 3,sources/a.md,accepted,0.9,\n' >> "$KB/facts/candidates.csv"
printf 'B,league_rank,3rd,sources/a.md,accepted,0.9,\n' >> "$KB/facts/candidates.csv"
# A pending row is NOT engine input, so warning about it would be crying wolf and
# would quietly undo the meaning of the accept gate.
printf 'C,league_rank,rank 9,sources/a.md,candidate,0.9,\n' >> "$KB/facts/candidates.csv"
printf 'league_rank\n' > "$KB/policy/attribute-relations.md"
printf -- '- `league_rank` : ordinal as rankval\n' > "$KB/policy/typed-relations.md"

# Skip ONLY when the engine is genuinely absent. Swallowing every failure as a skip
# is how this harness passed in CI while the bug it pins was fully restored.
if ! "$PY" -c "import pyrewire" >/dev/null 2>&1; then
  echo "SKIP: pyrewire not installed"
  exit 0
fi
FACTLOG_ROOT="$KB" "$PY" tools/compile_facts.py >/dev/null || { echo "FAIL: compile_facts.py failed"; exit 1; }
FACTLOG_ROOT="$KB" "$PY" tools/run_logic_check.py >/dev/null || { echo "FAIL: run_logic_check.py failed"; exit 1; }
REPORT="$KB/facts/logic_report.txt"
[ -f "$REPORT" ] || { echo "FAIL: the engine ran but wrote no report"; exit 1; }

# (a) the unparseable fact is named in the report, not just on stderr
grep -q 'rank 3' "$REPORT"; check "(a) the dropped fact is named in the report" "$?" "0"
# (b) the report does not claim zero warnings while a fact is missing from typed queries
grep -qE '^warnings: 0$' "$REPORT"; [ "$?" -ne 0 ]; check "(b) report does not say warnings: 0" "$?" "0"
# (c) the consequence — exclusion from the comparison predicate — is stated
grep -q 'EXCLUDED' "$REPORT"; check "(c) the report states the fact is excluded" "$?" "0"
# (d) the parseable fact is NOT warned about (no crying wolf)
grep -q '3rd' "$REPORT"; [ "$?" -ne 0 ]; check "(d) the parseable fact is not warned about" "$?" "0"

# (e) a pending row is not warned about -- it was never engine input
grep -q 'rank 9' "$REPORT"; [ "$?" -ne 0 ]; check "(e) a pending row is not warned about" "$?" "0"

echo
if [ "$fails" -eq 0 ]; then echo "typed-parse report: all passed"; else echo "typed-parse report: $fails failed"; exit 1; fi
