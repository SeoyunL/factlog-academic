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
# Usage — always against a THROWAWAY COPY, never examples/sample-kb in place:
#   KB="$(mktemp -d)/sample-kb" && cp -R examples/sample-kb "$KB" && \
#     FACTLOG_ROOT="$KB" bash tests/golden.sh
#
# The run REWRITES <KB>/facts/logic_report.txt, and since #554 that file carries the
# absolute path of the factlog package that produced it. Pointed at the tracked
# examples/sample-kb, a run leaves a developer's home directory sitting in a committed
# fixture — so the copy is not a nicety, it is what keeps that path out of git.
#
# Returns 0 if all golden diffs pass and --check passes, 1 on first failure.
#
# Acceptance checks (from unit u13):
#   bash -n tests/golden.sh
#   KB="$(mktemp -d)/sample-kb" && cp -R examples/sample-kb "$KB" && \
#     FACTLOG_ROOT="$KB" bash tests/golden.sh && echo GOLDEN-STABLE

set -euo pipefail

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
# The `factlog: <version> (<path>)` header line (#554) is machine- and release-specific
# by construction, so it can never live in a byte-compared fixture: committing it would
# pin one developer's absolute path and break the golden on every version bump. It is
# stripped from BOTH sides before the diff — from the golden too, so this filter stays
# correct if the fixture is ever regenerated — and then asserted separately below
# against a measurement, which is stronger than a frozen string could be.
REPORT_TMP="$(mktemp -d)"
trap 'rm -rf "$REPORT_TMP"' EXIT
grep -v '^factlog: ' "$KB_ROOT/facts/logic_report.txt" > "$REPORT_TMP/actual.txt" || true
grep -v '^factlog: ' "$GOLDEN_DIR/logic_report.txt" > "$REPORT_TMP/expected.txt" || true
assert_golden "facts/logic_report.txt (factlog: line excluded)" \
  "$REPORT_TMP/actual.txt" \
  "$REPORT_TMP/expected.txt"

# ---------------------------------------------------------------------------
# Step 2b: the factlog: provenance line itself (#554)
# ---------------------------------------------------------------------------
# Asserted against what the reader would measure, not against a fixture: the whole
# point of the line is that it names the code that actually ran, so a hardcoded
# expectation would only pin the fixture-writer's machine.
#
# The measurement runs with cwd=$PLUGIN_ROOT because that is how the tools resolve
# the package: tools/factlog_config.py inserts the repo root at sys.path[0], so the
# report must name THIS checkout's factlog, not whatever an editable install elsewhere
# registered. If those two ever disagree, this is the failure that says so.
echo ""
echo "=== Step 2b: factlog: provenance line ==="
report_line="$(grep '^factlog: ' "$KB_ROOT/facts/logic_report.txt" || true)"
if [ -z "$report_line" ]; then
  fail_msg "logic_report.txt has no 'factlog: ' provenance line"
else
  expected_path="$(cd "$PLUGIN_ROOT" && "$PYTHON" -c 'import factlog; print(factlog.__file__)')"
  expected_version="$(cd "$PLUGIN_ROOT" && "$PYTHON" -c 'import factlog; print(factlog.__version__)')"
  expected_line="factlog: ${expected_version} (${expected_path})"
  if [ "$report_line" = "$expected_line" ]; then
    ok "factlog: line names the running package ($report_line)"
  else
    fail_msg "factlog: line disagrees with the running package"
    echo "  report:   $report_line" >&2
    echo "  measured: $expected_line" >&2
  fi
fi

# The line must appear exactly once, directly under the '==================' rule and
# above 'engine:' — a second copy, or one buried in the body, would make the report's
# producer ambiguous again.
if [ "$(grep -c '^factlog: ' "$KB_ROOT/facts/logic_report.txt")" = "1" ]; then
  ok "factlog: line appears exactly once"
else
  fail_msg "factlog: line must appear exactly once"
fi
if [ "$(sed -n '3p' "$KB_ROOT/facts/logic_report.txt")" = "$report_line" ] \
  && [ "$(sed -n '4p' "$KB_ROOT/facts/logic_report.txt")" = "engine: wirelog / pyrewire" ]; then
  ok "factlog: line sits directly above the engine: line"
else
  fail_msg "factlog: line is not on report line 3, above 'engine:'"
fi

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
