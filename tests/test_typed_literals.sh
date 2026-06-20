#!/usr/bin/env bash
# tests/test_typed_literals.sh — typed-comparison smoke harness (#121)
#
# Pins the CI-active value of typed literals without re-proving what pytest
# already covers (threshold/determinism/graceful-degrade live in
# tests/unit/test_typed_projection.py). Two layers:
#
#   1. compile layer (ALWAYS runs, NO pyrewire): the REAL compile_facts.py
#      filters candidates.csv by status, so a `superseded` typed row never
#      reaches accepted.dl (the engine input). Positive control: an `accepted`
#      row IS present. This is the section the CI `test` job actually exercises
#      (that job has no pyrewire).
#   2. sample-kb golden guard (NO pyrewire): examples/sample-kb must stay
#      untyped (no policy/typed-relations.md) so golden.sh stays byte-identical.
#   3. engine layer (pyrewire-GATED, skips cleanly): with the engine present,
#      run_logic_check.py surfaces `after2030: 을서비스 (launch_after_2030)` and
#      NOT the superseded 구서비스.
#
# Per-section gating (NOT a top-level exit 0): the compile assertions always run.
#
# Usage: bash tests/test_typed_literals.sh
#   With engine: PATH="/path/to/venv/bin:$PATH" bash tests/test_typed_literals.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# --- temp KB with one accepted + one superseded typed row ---------------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
H="subject,relation,object,source,status,confidence,note"
# 을서비스 (accepted, 2030.1 -> 20300101) clears the threshold.
# 구서비스 (superseded, 2032.1 -> 20320101) WOULD clear but is retired, so it
# must never reach accepted.dl and must never appear in a comparison finding.
printf '%s\n%s\n%s\n' "$H" \
  '을서비스,정식_운영,2030.1,sources/a.md,accepted,0.9,' \
  '구서비스,정식_운영,2032.1,sources/a.md,superseded,0.9,retired' > "$KB/facts/candidates.csv"
printf 'x\n' > "$KB/sources/a.md"
printf -- '- `정식_운영` : date as launch_date\n' > "$KB/policy/typed-relations.md"

# --- section 0: scaffold + empty typed_relations() (no pyrewire) --------------
# A freshly init-ed KB ships an all-commented policy/typed-relations.md whose
# typed_relations() parses to {} with NO stderr warning (BOM/parse cleanliness),
# so a fresh KB stays byte-identical (#116 invariant 1).
SKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$SKB" >/dev/null
if [ -f "$SKB/policy/typed-relations.md" ]; then
  ok "init scaffolds policy/typed-relations.md"
else
  bad "policy/typed-relations.md not scaffolded"
fi
scaffold_err="$(mktemp)"
verdict="$(FACTLOG_ROOT="$SKB" "$PYTHON" - <<'PY' 2>"$scaffold_err"
import sys; sys.path.insert(0, "tools")
import common as c
print("OK" if c.typed_relations() == {} else f"FAIL: {c.typed_relations()!r}")
PY
)"
[ "$verdict" = "OK" ] && ok "scaffolded stub -> typed_relations() == {}" || bad "$verdict"
[ -s "$scaffold_err" ] && bad "stderr warning loading scaffolded stub: $(cat "$scaffold_err")" \
  || ok "no stderr warning loading scaffolded stub (BOM/parse clean)"
rm -f "$scaffold_err"

# --- section 1: compile layer (ALWAYS runs, no pyrewire) ----------------------
# logic-policy.dl needs at least one valid .decl; compile_facts does not read it
# but keeping the KB engine-shaped lets section 3 reuse it.
printf '%s\n%s\n' \
  '// generated from policy/logic-policy.md' \
  '.decl requires_review(entity: symbol, reason: symbol)' > "$KB/policy/logic-policy.dl"

if FACTLOG_ROOT="$KB" "$PYTHON" "$PLUGIN_ROOT/tools/compile_facts.py" >/dev/null 2>&1; then
  ok "compile_facts.py exits 0"
else
  bad "compile_facts.py failed (exit non-zero)"
fi

if grep -q '"구서비스"' "$KB/facts/accepted.dl"; then
  bad "superseded typed row leaked into accepted.dl"
else
  ok "superseded typed row excluded from accepted.dl"
fi

if grep -q '"을서비스"' "$KB/facts/accepted.dl"; then
  ok "accepted typed row present in accepted.dl (positive control)"
else
  bad "accepted typed row missing from accepted.dl"
fi

# --- section 2: sample-kb golden guard (no pyrewire) --------------------------
# examples/sample-kb MUST stay untyped so golden.sh (run in the unit job) stays
# byte-identical. Guard the existence only; never run golden.sh against the
# tracked tree from here (it would mutate it).
if [ -f "$PLUGIN_ROOT/examples/sample-kb/policy/typed-relations.md" ]; then
  bad "examples/sample-kb gained policy/typed-relations.md (must stay untyped)"
else
  ok "examples/sample-kb stays untyped (no policy/typed-relations.md)"
fi

# --- section 3: engine layer (pyrewire-GATED, skips cleanly) -------------------
if "$PYTHON" -c 'import pyrewire' 2>/dev/null; then
  # Arity-2 (entity, reason) head with a quoted reason; the scalar D stays in the
  # body (an arity-1 head crashes run_logic_check.py's 2-tuple unpack).
  printf '%s\n%s\n' \
    '.decl after2030(entity: symbol, reason: symbol)' \
    'after2030(S, "launch_after_2030") :- launch_date(S, D), D >= 20300101.' \
    > "$KB/policy/logic-policy.extra.dl"

  if FACTLOG_ROOT="$KB" "$PYTHON" "$PLUGIN_ROOT/tools/run_logic_check.py" >/dev/null 2>&1; then
    REPORT="$KB/facts/logic_report.txt"
    findings="$(sed -n '/Policy Findings:/,$p' "$REPORT")"
    if printf '%s' "$findings" | grep -q 'after2030: 을서비스 (launch_after_2030)'; then
      ok "engine finding lists 을서비스 (launch_after_2030)"
      # Absence check is only non-vacuous when the positive finding is present:
      # if Policy Findings: were missing entirely, findings would be empty and the
      # grep-q below would pass silently. Nesting it here guarantees the section
      # is live before we assert the retired row is absent.
      if printf '%s' "$findings" | grep -q '구서비스'; then
        bad "superseded 구서비스 surfaced in Policy Findings"
      else
        ok "superseded 구서비스 absent from Policy Findings"
      fi
    else
      bad "engine finding missing 을서비스 (launch_after_2030)"
    fi
  else
    bad "run_logic_check.py failed (exit non-zero)"
  fi
else
  echo "SKIP: engine assertions (no pyrewire)"
fi

echo ""
echo "========================================"
echo "test_typed_literals: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
