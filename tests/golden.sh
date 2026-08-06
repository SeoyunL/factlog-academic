#!/usr/bin/env bash
# tests/golden.sh — deterministic golden regression over two KBs (T12 / u13)
#
# Re-runs the deterministic engine steps and diffs each output byte-for-byte
# against the committed golden files in tests/golden/. Protects AC4 determinism:
# any engine change that alters accepted.dl or logic_report.txt is caught
# immediately.
#
# Also exercises generate_logic_policy.py --check (deterministic re-derivation)
# to confirm the committed logic-policy.dl matches what the fixture compiler
# would produce from logic-policy.md.
#
# WHAT THE GOLDEN KBs COVER — read this before citing a green run as evidence.
# Byte-identity is evidence only for the code paths the KB actually walks.
#
#   KB 1, the caller's FACTLOG_ROOT (examples/sample-kb), goldens in
#   tests/golden/: plain relation/3 facts, a requires_review policy rule
#   compiled from logic-policy.md, relation and review_required queries. Its
#   policy/ holds NO single-valued.md, typed-relations.md,
#   attribute-relations.md or relation-aliases.md, so every policy gate below is
#   inert on it — check_conflicts returns early, typed projection never runs,
#   no canonical/3 atom is emitted, and its query.dl has no count or path line.
#
#   KB 2, tests/golden-kb, goldens in tests/golden/policy-kb/: exists precisely
#   to walk those gates. It declares all four policy files and pins
#   single-valued contradiction detection (Step 4), typed projection of all four
#   literal types with thresholds tight against the one fact that satisfies
#   each, attribute-relation exclusion of a literal from the entity graph (the
#   refused path endpoint), alias canonicalisation (a rule written over the
#   canonical name firing on a fact stated with the surface form), and count/
#   path query rendering.
#
# A change to a path NEITHER KB walks is not covered by a green run here (#354).
#
# Usage:
#   FACTLOG_ROOT=examples/sample-kb bash tests/golden.sh
#   PYTHON=<interpreter> FACTLOG_ROOT=examples/sample-kb bash tests/golden.sh
#
# Returns 0 if all golden diffs pass and --check passes, 1 on first failure.
#
# Acceptance checks (from unit u13):
#   bash -n tests/golden.sh
#   cd /Users/joykim/git/semantic-reasoning/factlog && \
#     FACTLOG_ROOT=examples/sample-kb bash tests/golden.sh && echo GOLDEN-STABLE

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GOLDEN_DIR="$SCRIPT_DIR/golden"

# The policy-gate KB is harness-owned, not caller-supplied: it is the half of
# the coverage that FACTLOG_ROOT cannot provide, so it runs on every invocation
# regardless of where the caller points KB 1.
POLICY_KB="$SCRIPT_DIR/golden-kb"
POLICY_GOLDEN_DIR="$GOLDEN_DIR/policy-kb"

# FACTLOG_ROOT must be set by the caller; resolve to absolute path.
if [ -z "${FACTLOG_ROOT:-}" ]; then
  echo "FATAL: FACTLOG_ROOT is not set. Run as: FACTLOG_ROOT=examples/sample-kb bash tests/golden.sh" >&2
  exit 1
fi
# Resolve relative to cwd if not absolute.
case "$FACTLOG_ROOT" in
  /*) KB_ROOT="$FACTLOG_ROOT" ;;
  *)  KB_ROOT="$(pwd)/$FACTLOG_ROOT" ;;
esac
export FACTLOG_ROOT="$KB_ROOT"

# Python interpreter: the caller's PYTHON wins, then factlog-venv, then python3.
# The caller's value used to be overwritten unconditionally, so the
# `PYTHON=<interpreter> bash tests/x.sh` convention every other harness in this
# directory follows was silently discarded here — a run asked to use an
# interpreter with pyrewire fell through to a bare python3 without it, and Step 2
# died for a reason that had nothing to do with the branch under test (#354).
if [ -z "${PYTHON:-}" ]; then
  if [ -x "/tmp/factlog-venv/bin/python" ]; then
    PYTHON="/tmp/factlog-venv/bin/python"
  else
    PYTHON="python3"
  fi
fi

pass=0
fail=0

ok() {
  echo "PASS: $*"
  pass=$((pass + 1))
}

fail_msg() {
  echo "FAIL: $*" >&2
  fail=$((fail + 1))
}

# --- artifact freshness -----------------------------------------------------
# Every golden diff below must compare bytes THIS run produced. The committed
# copy of each artifact is identical to its golden file (that is what a green
# main means), so leaving it in place made the diff pass whenever the step that
# should have rewritten it died — a `PASS: facts/logic_report.txt matches golden`
# that held no matter what the branch changed, and that was cited as evidence
# (#354). So each artifact is moved out of the KB before its step runs: the step
# must recreate it, or the diff has nothing to compare and fails.
#
# A step that dies would then leave the KB missing a committed file, so the EXIT
# trap puts back anything that was not rewritten.
STASH_DIR="$(mktemp -d)"
STASH_COUNT=0

stash_artifact() {
  local path="$1"
  [ -f "$path" ] || return 0
  mv "$path" "$STASH_DIR/$STASH_COUNT"
  printf '%s\t%s\n' "$STASH_COUNT" "$path" >> "$STASH_DIR/manifest"
  STASH_COUNT=$((STASH_COUNT + 1))
}

restore_stashed() {
  if [ -f "$STASH_DIR/manifest" ]; then
    local idx path
    while IFS=$'\t' read -r idx path; do
      if [ ! -e "$path" ] && [ -e "$STASH_DIR/$idx" ]; then
        mv "$STASH_DIR/$idx" "$path"
      fi
    done < "$STASH_DIR/manifest"
  fi
  rm -rf "$STASH_DIR"
}
trap restore_stashed EXIT

assert_golden() {
  local label="$1"
  local actual="$2"
  local expected="$3"
  if [ ! -f "$actual" ]; then
    fail_msg "$label was not regenerated by this run — the step above did not write it, so there is nothing from this branch to compare"
    return 0
  fi
  if diff -u "$expected" "$actual" >/dev/null 2>&1; then
    ok "$label matches golden"
  else
    fail_msg "$label differs from golden"
    diff -u "$expected" "$actual" >&2 || true
  fi
}

# ---------------------------------------------------------------------------
# One pass = the three deterministic steps over one KB, each diffed against that
# KB's golden directory.
#   Step 1: compile_facts.py          → facts/accepted.dl
#   Step 2: run_logic_check.py        → facts/logic_report.txt
#   Step 3: generate_logic_policy.py --check (deterministic re-derivation)
# ---------------------------------------------------------------------------
run_pass() {
  local kb="$1"
  local golden="$2"

  echo "=== Step 1: compile_facts.py ==="
  stash_artifact "$kb/facts/accepted.dl"
  if FACTLOG_ROOT="$kb" "$PYTHON" "$PLUGIN_ROOT/tools/compile_facts.py" 2>&1; then
    ok "compile_facts.py exit 0"
  else
    fail_msg "compile_facts.py exited non-zero"
  fi
  assert_golden "facts/accepted.dl" \
    "$kb/facts/accepted.dl" \
    "$golden/accepted.dl"

  echo ""
  echo "=== Step 2: run_logic_check.py ==="
  stash_artifact "$kb/facts/logic_report.txt"
  if FACTLOG_ROOT="$kb" "$PYTHON" "$PLUGIN_ROOT/tools/run_logic_check.py" 2>&1; then
    ok "run_logic_check.py exit 0"
  else
    fail_msg "run_logic_check.py exited non-zero"
  fi
  assert_golden "facts/logic_report.txt" \
    "$kb/facts/logic_report.txt" \
    "$golden/logic_report.txt"

  echo ""
  echo "=== Step 3: generate_logic_policy.py --check ==="
  if FACTLOG_ROOT="$kb" "$PYTHON" "$PLUGIN_ROOT/tools/generate_logic_policy.py" --check 2>&1; then
    ok "generate_logic_policy.py --check exit 0"
  else
    fail_msg "generate_logic_policy.py --check exited non-zero (policy/logic-policy.dl is stale)"
  fi
}

echo "### KB 1/2: $KB_ROOT"
run_pass "$KB_ROOT" "$GOLDEN_DIR"

echo ""
echo "### KB 2/2: $POLICY_KB"
run_pass "$POLICY_KB" "$POLICY_GOLDEN_DIR"

# ---------------------------------------------------------------------------
# Step 4 (policy KB only): check_conflicts.py → single-valued contradictions
#
# The other steps never reach this gate: run_logic_check.py does not read
# policy/single-valued.md at all, so conflict detection has no golden coverage
# except here. The fixture KB asserts two maintainers for one subject, so the
# tool is EXPECTED to exit 1 — exit 0 would mean the contradiction went
# undetected, which is the regression this pins.
# ---------------------------------------------------------------------------
echo ""
echo "=== Step 4: check_conflicts.py (policy KB) ==="
conflicts_out="$STASH_DIR/conflicts.txt"
conflicts_rc=0
FACTLOG_ROOT="$POLICY_KB" "$PYTHON" "$PLUGIN_ROOT/tools/check_conflicts.py" \
  > "$conflicts_out" 2>&1 || conflicts_rc=$?
cat "$conflicts_out"
if [ "$conflicts_rc" -eq 1 ]; then
  ok "check_conflicts.py exit 1 (the declared contradiction is detected)"
else
  fail_msg "check_conflicts.py exited $conflicts_rc, expected 1 (single-valued contradiction not detected)"
fi
assert_golden "check_conflicts.py output" \
  "$conflicts_out" \
  "$POLICY_GOLDEN_DIR/conflicts.txt"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "Golden results: $pass passed, $fail failed"
echo "========================================"
if [ "$fail" -gt 0 ]; then
  exit 1
fi
exit 0
