#!/usr/bin/env bash
# tests/golden.sh — deterministic golden regression bound to examples/sample-kb (T12 / u13)
#
# Re-runs the deterministic engine steps against examples/sample-kb and diffs
# each output byte-for-byte against the committed golden files in tests/golden/.
# Protects AC4 determinism: any engine change that alters accepted.dl or
# logic_report.txt is caught immediately.
#
# Also exercises generate_logic_policy.py --check (deterministic re-derivation)
# to confirm the committed logic-policy.dl matches what the fixture compiler
# would produce from logic-policy.md.
#
# Usage:
#   FACTLOG_ROOT=examples/sample-kb bash tests/golden.sh
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

# Python interpreter: prefer factlog-venv if available, fall back to python3.
if [ -x "/tmp/factlog-venv/bin/python" ]; then
  PYTHON="/tmp/factlog-venv/bin/python"
else
  PYTHON="python3"
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

assert_golden() {
  local label="$1"
  local actual="$2"
  local expected="$3"
  if diff -u "$expected" "$actual" >/dev/null 2>&1; then
    ok "$label matches golden"
  else
    fail_msg "$label differs from golden"
    diff -u "$expected" "$actual" >&2 || true
  fi
}

# ---------------------------------------------------------------------------
# Step 1: compile_facts.py → facts/accepted.dl
# ---------------------------------------------------------------------------
echo "=== Step 1: compile_facts.py ==="
if "$PYTHON" "$PLUGIN_ROOT/tools/compile_facts.py" 2>&1; then
  ok "compile_facts.py exit 0"
else
  fail_msg "compile_facts.py exited non-zero"
fi
assert_golden "facts/accepted.dl" \
  "$KB_ROOT/facts/accepted.dl" \
  "$GOLDEN_DIR/accepted.dl"

# ---------------------------------------------------------------------------
# Step 2: run_logic_check.py → facts/logic_report.txt
# ---------------------------------------------------------------------------
echo ""
echo "=== Step 2: run_logic_check.py ==="
if "$PYTHON" "$PLUGIN_ROOT/tools/run_logic_check.py" 2>&1; then
  ok "run_logic_check.py exit 0"
else
  fail_msg "run_logic_check.py exited non-zero"
fi
assert_golden "facts/logic_report.txt" \
  "$KB_ROOT/facts/logic_report.txt" \
  "$GOLDEN_DIR/logic_report.txt"

# ---------------------------------------------------------------------------
# Step 3: generate_logic_policy.py --check (deterministic re-derivation)
# ---------------------------------------------------------------------------
echo ""
echo "=== Step 3: generate_logic_policy.py --check ==="
if "$PYTHON" "$PLUGIN_ROOT/tools/generate_logic_policy.py" --check 2>&1; then
  ok "generate_logic_policy.py --check exit 0"
else
  fail_msg "generate_logic_policy.py --check exited non-zero (policy/logic-policy.dl is stale)"
fi

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
