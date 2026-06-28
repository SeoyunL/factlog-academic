#!/usr/bin/env bash
# tests/setup.sh — one-shot `factlog setup` orchestration test (u18)
#
# Verifies the `setup` subcommand performs doctor + init and exits 0 on an
# environment where pyrewire is ALREADY present, WITHOUT depending on the
# network or a real pip install. The venv at /tmp/factlog-venv already has
# pyrewire >=1.0.3, so setup takes the "already satisfied, skip install" path —
# the pip branch is exercised by code review (PEP 668 guidance), not here.
#
# Asserts:
#   - setup exits 0
#   - the KB layout (sources/, facts/, policy/, etc.) is created by init
#   - re-running setup is idempotent (still exit 0)
#
# Usage:
#   bash tests/setup.sh
#
# Returns 0 if all checks pass, 1 on first failure.

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Python interpreter: the engine (pyrewire) is required for setup's doctor
# checks to pass, so prefer the prepared factlog-venv; fall back to python3.
if [ -x "/tmp/factlog-venv/bin/python" ]; then
  PYTHON="/tmp/factlog-venv/bin/python"
else
  PYTHON="python3"
fi

SETUP_KB="/tmp/factlog-setup-test-kb"

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

assert_dir() {
  local path="$1"
  local label="${2:-$1}"
  if [ -d "$path" ]; then
    ok "$label exists"
  else
    fail_msg "$label missing (expected dir at $path)"
  fi
}

# ---------------------------------------------------------------------------
# Step 1: fresh setup → exit 0, doctor OK (pyrewire present), KB created
# ---------------------------------------------------------------------------
echo "=== Step 1: factlog setup (fresh) ==="
rm -rf "$SETUP_KB"
if (cd "$PLUGIN_ROOT" && "$PYTHON" -m factlog setup --target "$SETUP_KB"); then
  ok "factlog setup exit 0"
else
  fail_msg "factlog setup exited non-zero"
fi

# ---------------------------------------------------------------------------
# Step 2: KB layout created by init
# ---------------------------------------------------------------------------
echo ""
echo "=== Step 2: KB layout ==="
for d in sources pages facts decisions policy policy/prompts runs; do
  assert_dir "$SETUP_KB/$d" "$d/"
done

# ---------------------------------------------------------------------------
# Step 3: idempotent re-run → still exit 0
# ---------------------------------------------------------------------------
echo ""
echo "=== Step 3: factlog setup (idempotent re-run) ==="
if (cd "$PLUGIN_ROOT" && "$PYTHON" -m factlog setup --target "$SETUP_KB"); then
  ok "factlog setup re-run exit 0 (idempotent)"
else
  fail_msg "factlog setup re-run exited non-zero"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "========================================"
echo "Setup results: $pass passed, $fail failed"
echo "========================================"
if [ "$fail" -gt 0 ]; then
  exit 1
fi
exit 0
