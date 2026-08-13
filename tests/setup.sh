#!/usr/bin/env bash
# tests/setup.sh — one-shot `factlog setup` orchestration test (u18)
#
# Verifies the `setup` subcommand performs doctor + init and exits 0 on an
# environment where pyrewire is ALREADY present, WITHOUT depending on the
# network or a real pip install: on an interpreter that already has pyrewire
# >=1.0.3 setup takes the "already satisfied, skip install" path — the pip
# branch is exercised by code review (PEP 668 guidance), not here. Point PYTHON
# at such an interpreter; on one without the engine the run skips rather than
# reporting failures about `setup` that are really about the interpreter.
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
# Returns 0 if all checks pass or pyrewire is absent (SKIP), 1 on first failure
# or if PYTHON names something that cannot run.

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Python interpreter: the form the rest of tests/ uses. The caller's value used
# to be overwritten unconditionally, so `PYTHON=<interpreter> bash tests/x.sh`
# was silently discarded here — a run pointed at an interpreter WITH pyrewire
# fell through to a bare python3 without it, and every assertion below failed for
# a reason that had nothing to do with the `setup` command (#361, the defect
# golden.sh carried in #354).
#
# No /tmp/factlog-venv branch: an implicit preference for a path the machine
# shares would make this harness select a different interpreter from every other
# one on any machine that has it. #354 tried that branch and took it back out
# (4036ac5); a caller who wants that venv passes it, as everywhere else.
PYTHON="${PYTHON:-python3}"

# Say which interpreter ran. The misattribution this harness produced was not
# caused by the choice but by the silence about it: a verdict printed without
# the interpreter beside it gets explained by whatever the reader assumes.
echo "PYTHON: $PYTHON"

# An interpreter that cannot run at all is a different fact from one that runs
# without the engine, and folding them together is how a mistyped path gets
# reported as "this machine has no pyrewire". `import pyrewire` alone cannot
# tell them apart — it fails identically — so the executable check comes first
# and is fatal, and only a real interpreter can reach the skip below.
#
# Both checks run from PLUGIN_ROOT, which is where the steps invoke $PYTHON: a
# relative path has to be judged against the directory it will actually resolve
# in, and a check that resolves it somewhere else answers a question about a
# different interpreter than the one that will run. The probe was left in the
# caller's cwd once, and a relative PYTHON with the engine reported "pyrewire
# not installed" and exited 0 from any cwd but the repo root — a green run that
# measured nothing, the same confusion this block exists to prevent, inverted.
if ! (cd "$PLUGIN_ROOT" && command -v "$PYTHON" >/dev/null 2>&1); then
  echo "FATAL: PYTHON is not an executable interpreter: $PYTHON" >&2
  exit 1
fi

# Skip cleanly if pyrewire is absent. `setup` on an interpreter without the
# engine cannot reach the "already satisfied" path this harness exists to check,
# so the run says nothing about `setup` — reporting it as 9 failures names the
# wrong subject.
if ! (cd "$PLUGIN_ROOT" && "$PYTHON" -c "import pyrewire") >/dev/null 2>&1; then
  echo "SKIP: pyrewire not installed; tests/setup.sh requires the engine"
  exit 0
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
