#!/usr/bin/env bash
# tests/setup.sh — one-shot `factlog setup` orchestration test (u18)
#
# Verifies the `setup` subcommand performs doctor + init and exits 0 on an
# environment where pyrewire is ALREADY present, WITHOUT depending on the
# network or a real pip install: on an interpreter that already has pyrewire
# >=1.0.3 setup takes the "already satisfied, skip install" path — the pip
# branch is exercised by code review (PEP 668 guidance), not here. Point PYTHON
# at such an interpreter; on one without it, every assertion below fails for a
# reason that has nothing to do with the `setup` command.
#
# Asserts:
#   - setup exits 0
#   - the KB layout (sources/, facts/, policy/, etc.) is created by init
#   - re-running setup is idempotent (still exit 0)
#
# Usage:
#   bash tests/setup.sh
#   PYTHON=<interpreter> bash tests/setup.sh
#
# Returns 0 if all checks pass, 1 on first failure.

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Python interpreter: the engine (pyrewire) is required for setup's doctor
# checks to pass, so an unset PYTHON prefers the prepared factlog-venv and falls
# back to python3.
#
# The caller's value used to be overwritten unconditionally, so the
# `PYTHON=<interpreter> bash tests/x.sh` convention the rest of tests/ follows
# was silently discarded here (#361, the same defect golden.sh carried in #354).
# On a machine without /tmp/factlog-venv a run told to use an interpreter WITH
# pyrewire fell through to a bare python3 without it and reported 0 passed,
# 9 failed — and the reverse misattribution is worse: a 9/9 under a PATH that
# happened to hold a good python3 was read as "because I exported PYTHON", a
# mechanism that did not exist.
#
# golden.sh dropped its /tmp/factlog-venv branch entirely, on the ground that a
# regression harness must not silently choose a different interpreter from the
# other 39. This one keeps it: it is a dev-environment script whose whole
# premise is an environment where pyrewire is already installed, and that path
# is where this repo's dev setup puts it. The branch only runs when the caller
# named nothing.
if [ -z "${PYTHON:-}" ]; then
  if [ -x "/tmp/factlog-venv/bin/python" ]; then
    PYTHON="/tmp/factlog-venv/bin/python"
  else
    PYTHON="python3"
  fi
elif ! (cd "$PLUGIN_ROOT" && command -v "$PYTHON" >/dev/null 2>&1); then
  # A named interpreter that cannot run must stop the harness, not fall back:
  # dropping to python3 here would measure something other than what was asked
  # for and report the verdict as if it were about the branch under test.
  #
  # Checked from PLUGIN_ROOT because that is where every step below invokes it,
  # so a relative path is judged against the directory it will actually be
  # resolved in rather than against the caller's cwd.
  echo "FATAL: PYTHON is not an executable interpreter: $PYTHON" >&2
  exit 1
fi

# Overridable so a caller — the unit pin in particular — can keep the run inside
# its own scratch directory. The default is machine-wide: every checkout and
# every parallel lane on the box shares it, and Step 1 begins by deleting it.
SETUP_KB="${SETUP_KB:-/tmp/factlog-setup-test-kb}"

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
