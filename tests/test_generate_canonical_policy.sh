#!/usr/bin/env bash
# tests/test_generate_canonical_policy.sh — #243: canonical-bodied rules declared
# in logic-policy.md via an anchored, lowercase `{canonical}` prefix.
#
# Pins:
#   - --check treats the {canonical} flag as byte-load-bearing in BOTH directions:
#     adding/removing the marker without regenerating makes logic-policy.dl STALE.
#   - Lane A end-to-end: a {canonical} rule whose body names a canonical relation
#     FIRES over a fact stated with an aliased SURFACE variant; the same rule with
#     the marker removed (→ relation/3 body) does NOT fire. This proves the marker
#     actually emits canonical( and is not silently ignored.
#   - No-op when alias-absent: the generated canonical .dl present but no
#     relation-aliases.md → run_logic_check fires nothing (empty canonical/3).
#
# Mirrors tests/test_canonical_rule_firing.sh (Lane B / extra.dl) idioms.
# Requires pyrewire. Skipped cleanly if absent.
# Usage: PYTHON=<path> bash tests/test_generate_canonical_policy.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
RLC="$PLUGIN_ROOT/tools/run_logic_check.py"
CF="$PLUGIN_ROOT/tools/compile_facts.py"
GLP="$PLUGIN_ROOT/tools/generate_logic_policy.py"
HEADER="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok()  { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# Skip cleanly if pyrewire is absent
if ! "$PYTHON" -c "import pyrewire" >/dev/null 2>&1; then
  echo "SKIP: pyrewire not installed; test_generate_canonical_policy requires the engine"
  exit 0
fi

# The canonical bullet: {canonical}-prefixed, body names the canonical relation 결론.
CANON_MD='# Logic policy

## Rules

- [uses_concludes] {canonical} 문서가 `결론` 이면 본다.
'
# Same rule with the marker removed → plain relation/3 body.
PLAIN_MD='# Logic policy

## Rules

- [uses_concludes] 문서가 `결론` 이면 본다.
'

# ---------------------------------------------------------------------------
# KB1: alias present, fact stated with SURFACE variant `concludes`
# ---------------------------------------------------------------------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null 2>&1

printf '%s\n' "$HEADER" \
  'doc1,concludes,some_claim,sources/x.md,confirmed,0.9,' \
  > "$KB/facts/candidates.csv"
printf '# x\n' > "$KB/sources/x.md"

cat > "$KB/policy/relation-aliases.md" <<'ALIASES'
# Relation aliases
- `concludes` -> `결론`
ALIASES

printf '%s' "$CANON_MD" > "$KB/policy/logic-policy.md"

# Generate logic-policy.dl from the {canonical} bullet.
FACTLOG_ROOT="$KB" "$PYTHON" "$GLP" >/dev/null 2>&1

# ---------------------------------------------------------------------------
# Test 9a: generated .dl uses canonical( body, not relation(
# ---------------------------------------------------------------------------
if grep -qF 'canonical(X, "결론", _)' "$KB/policy/logic-policy.dl"; then
  ok "generated logic-policy.dl uses canonical( body for {canonical} bullet"
else
  bad "generated logic-policy.dl missing canonical( body"
fi

# ---------------------------------------------------------------------------
# Test 9b: --check roundtrip + stale in BOTH directions
# ---------------------------------------------------------------------------
set +e
FACTLOG_ROOT="$KB" "$PYTHON" "$GLP" --check >/dev/null 2>&1; rc=$?
set -e
[ "$rc" -eq 0 ] && ok "--check rc=0 after generate (canonical bullet)" \
                || bad "--check rc=$rc after generate (expected 0)"

# Remove the marker WITHOUT regenerating → .dl is now stale.
printf '%s' "$PLAIN_MD" > "$KB/policy/logic-policy.md"
set +e
FACTLOG_ROOT="$KB" "$PYTHON" "$GLP" --check >/dev/null 2>&1; rc=$?
set -e
[ "$rc" -ne 0 ] && ok "--check rc!=0 after removing marker (stale, direction 1)" \
                || bad "--check rc=0 after removing marker (expected stale)"

# Re-add the marker and regenerate → clean again.
printf '%s' "$CANON_MD" > "$KB/policy/logic-policy.md"
FACTLOG_ROOT="$KB" "$PYTHON" "$GLP" >/dev/null 2>&1
set +e
FACTLOG_ROOT="$KB" "$PYTHON" "$GLP" --check >/dev/null 2>&1; rc=$?
set -e
[ "$rc" -eq 0 ] && ok "--check rc=0 after re-adding marker + regenerate" \
                || bad "--check rc=$rc after re-adding marker (expected 0)"

# Adding a marker to a previously-relation bullet (edit .md, no regen) → stale.
# Current .dl is the canonical version; write the plain .md then regenerate the
# plain .dl so we have a relation-bodied baseline, then add the marker w/o regen.
printf '%s' "$PLAIN_MD" > "$KB/policy/logic-policy.md"
FACTLOG_ROOT="$KB" "$PYTHON" "$GLP" >/dev/null 2>&1   # baseline relation .dl
printf '%s' "$CANON_MD" > "$KB/policy/logic-policy.md" # add marker, no regen
set +e
FACTLOG_ROOT="$KB" "$PYTHON" "$GLP" --check >/dev/null 2>&1; rc=$?
set -e
[ "$rc" -ne 0 ] && ok "--check rc!=0 after adding marker to relation bullet (stale, direction 2)" \
                || bad "--check rc=0 after adding marker (expected stale)"

# Regenerate the canonical .dl so the firing test below is consistent.
FACTLOG_ROOT="$KB" "$PYTHON" "$GLP" >/dev/null 2>&1

# ---------------------------------------------------------------------------
# Test 10: surface-variant end-to-end firing (THE load-bearing Lane A proof)
# ---------------------------------------------------------------------------
FACTLOG_ROOT="$KB" "$PYTHON" "$CF" >/dev/null 2>&1

set +e
out="$(FACTLOG_ROOT="$KB" "$PYTHON" "$RLC" 2>&1)"; rc=$?
set -e
[ "$rc" -eq 0 ] && ok "run_logic_check exits 0 (canonical Lane A)" \
                || bad "run_logic_check exited $rc: $out"

if printf '%s' "$out" | grep -qF "uses_concludes"; then
  ok "canonical rule FIRES over surface variant 'concludes' (matched 결론)"
else
  bad "canonical rule did NOT fire over surface variant"
  printf '%s\n' "$out" >&2
fi

# Now remove {canonical} → relation(X,"결론",_) body cannot match a fact stored
# literally as 'concludes'. Regenerate, recompile, re-run → does NOT fire.
printf '%s' "$PLAIN_MD" > "$KB/policy/logic-policy.md"
FACTLOG_ROOT="$KB" "$PYTHON" "$GLP" >/dev/null 2>&1
if grep -qF 'relation(X, "결론", _)' "$KB/policy/logic-policy.dl"; then
  ok "after removing marker, .dl uses relation( body"
else
  bad "after removing marker, .dl did not switch to relation( body"
fi
FACTLOG_ROOT="$KB" "$PYTHON" "$CF" >/dev/null 2>&1
set +e
out2="$(FACTLOG_ROOT="$KB" "$PYTHON" "$RLC" 2>&1)"; rc=$?
set -e
if printf '%s' "$out2" | grep -qF "uses_concludes"; then
  bad "relation( rule wrongly fired over surface variant 'concludes'"
  printf '%s\n' "$out2" >&2
else
  ok "relation( rule does NOT fire over surface variant (proves marker emits canonical()"
fi

# ---------------------------------------------------------------------------
# Test 11: no-op when alias-absent — canonical .dl present, NO relation-aliases.md
# ---------------------------------------------------------------------------
KB2="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB2" >/dev/null 2>&1
printf '%s\n' "$HEADER" \
  'doc1,결론,some_claim,sources/x.md,confirmed,0.9,' \
  > "$KB2/facts/candidates.csv"
printf '# x\n' > "$KB2/sources/x.md"
# NO relation-aliases.md → canonical/3 EDB is empty.
printf '%s' "$CANON_MD" > "$KB2/policy/logic-policy.md"
FACTLOG_ROOT="$KB2" "$PYTHON" "$GLP" >/dev/null 2>&1
grep -qF 'canonical(X, "결론", _)' "$KB2/policy/logic-policy.dl" \
  && ok "KB2 generated canonical .dl present" \
  || bad "KB2 generated canonical .dl missing"
FACTLOG_ROOT="$KB2" "$PYTHON" "$CF" >/dev/null 2>&1
set +e
out3="$(FACTLOG_ROOT="$KB2" "$PYTHON" "$RLC" 2>&1)"; rc=$?
set -e
if printf '%s' "$out3" | grep -qF "uses_concludes"; then
  bad "canonical rule fired with empty canonical/3 (alias-absent no-op FAILED)"
  printf '%s\n' "$out3" >&2
else
  ok "alias-absent no-op: empty canonical/3 → canonical rule fires nothing"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "test_generate_canonical_policy: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
