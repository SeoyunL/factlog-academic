#!/usr/bin/env bash
# `factlog status` and check_conflicts must report the SAME number (#219).
# status ran a private counter that knew nothing of the value hierarchy, aliases or
# typed grouping, so it told the user "1 conflict" about a KB the gate had just
# cleared -- and told them to fix it by hand-editing candidates.csv.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${FACTLOG_PY:-${PYTHON:-python3}}"
export PYTHONPATH="$PWD"
fails=0
check() { if [ "$2" = "$3" ]; then echo "  ok: $1"; else echo "FAIL: $1 (want $3, got $2)"; fails=$((fails+1)); fi; }

KB="$(mktemp -d)/kb"
export XDG_CONFIG_HOME="$(mktemp -d)"
"$PY" -m factlog init --target "$KB" >/dev/null || { echo "FAIL: init"; exit 1; }
printf 'a\n' > "$KB/sources/a.md"
{ printf 'subject,relation,object,source,status,confidence,note\n'
  printf 'P1,연구유형,관찰연구,sources/a.md,accepted,0.9,\n'
  printf 'P1,연구유형,코호트연구,sources/a.md,accepted,0.9,\n'; } > "$KB/facts/candidates.csv"
printf '연구유형\n' > "$KB/policy/single-valued.md"
printf -- '- 연구유형: 코호트연구 ⊂ 관찰연구\n' > "$KB/policy/value-hierarchy.md"
FACTLOG_ROOT="$KB" "$PY" tools/compile_facts.py >/dev/null || { echo "FAIL: compile"; exit 1; }

gate_n() { FACTLOG_ROOT="$KB" "$PY" tools/check_conflicts.py 2>&1 | grep -oE '[0-9]+ conflict' | grep -oE '^[0-9]+'; }
status_n() { FACTLOG_ROOT="$KB" "$PY" -m factlog status 2>&1 | grep -oE 'conflicts:  [0-9]+' | grep -oE '[0-9]+'; }

check "(a) the gate clears a supertype/subtype pair" "$(gate_n)" "0"
check "(b) status agrees with the gate" "$(status_n)" "0"

# a genuine sibling: both must still see it
printf 'P2,연구유형,관찰연구,sources/a.md,accepted,0.9,\n' >> "$KB/facts/candidates.csv"
printf 'P2,연구유형,실험연구,sources/a.md,accepted,0.9,\n' >> "$KB/facts/candidates.csv"
FACTLOG_ROOT="$KB" "$PY" tools/compile_facts.py >/dev/null
check "(c) the gate still catches a real sibling conflict" "$(gate_n)" "1"
check "(d) status still catches it too" "$(status_n)" "1"

# nobody is told to hand-edit candidates.csv
OUT="$(FACTLOG_ROOT="$KB" "$PY" tools/check_conflicts.py 2>&1)"
printf '%s' "$OUT" | grep -q "superseded' in facts/candidates.csv" && { echo "FAIL: (e) still tells the user to hand-edit candidates.csv"; fails=$((fails+1)); } || echo "  ok: (e) the gate does not tell the user to hand-edit candidates.csv"
printf '%s' "$OUT" | grep -q "factlog eject --fact" && echo "  ok: (e) it names the human-gate command instead" || { echo "FAIL: (e) no gate command offered"; fails=$((fails+1)); }

echo
if [ "$fails" -eq 0 ]; then echo "status/conflict parity: all passed"; else echo "status/conflict parity: $fails failed"; exit 1; fi
