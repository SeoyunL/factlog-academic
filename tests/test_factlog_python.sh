#!/usr/bin/env bash
# Behavioral matrix for tools/factlog_python.sh (#578).
#
# The launcher decides which interpreter every plugin script and hook runs on, so
# each case here fixes one branch of that decision. Candidates are real venvs
# (`-m venv --without-pip`, hence guaranteed free of pyrewire) into which a stub
# `pyrewire.py` with a chosen __version__ is planted — that way the matrix holds
# whether or not the developer's own python3 has the engine installed.
#
# Usage: bash tests/test_factlog_python.sh
#   Returns 0 if all cases pass, 1 if any fail.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$SCRIPT_DIR/../tools/factlog_python.sh"
PYTHON="$(command -v "${PYTHON:-python3}")"  # absolute: cases below rewrite PATH

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# The launcher's whole input surface is FACTLOG_PYTHON / VIRTUAL_ENV / HOME / PATH.
# Neutralise all four so a developer shell that already exports FACTLOG_PYTHON
# cannot make these cases pass (or fail) for the wrong reason.
unset FACTLOG_PYTHON
unset VIRTUAL_ENV
export HOME="$TMP/home"
mkdir -p "$HOME"

pass=0
fail=0

report() {
  local desc="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "PASS: $desc ($actual)"
    pass=$((pass + 1))
  else
    echo "FAIL: $desc — expected [$expected], got [$actual]"
    fail=$((fail + 1))
  fi
}

# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------
venv_site() {
  local root="$1" candidate
  for candidate in "$root"/lib/python*/site-packages "$root/Lib/site-packages"; do
    if [ -d "$candidate" ]; then
      printf '%s' "$candidate"
      return 0
    fi
  done
  return 1
}

venv_python() {
  local root="$1"
  if [ -x "$root/bin/python" ]; then
    printf '%s' "$root/bin/python"
    return 0
  fi
  if [ -x "$root/Scripts/python.exe" ]; then
    printf '%s' "$root/Scripts/python.exe"
    return 0
  fi
  return 1
}

# make_venv <root> <id> [pyrewire-version]
#   <id> is echoed back by whichever interpreter the launcher ends up exec'ing,
#   so a case asserts on identity rather than on a path (macOS /var vs /private/var).
make_venv() {
  local root="$1" id="$2" version="${3:-}" site
  "$PYTHON" -m venv --without-pip "$root" >/dev/null
  site="$(venv_site "$root")"
  printf 'ID = "%s"\n' "$id" > "$site/factlog_test_id.py"
  if [ -n "$version" ]; then
    printf '__version__ = "%s"\n' "$version" > "$site/pyrewire.py"
  fi
}

# make_shim <bin-dir> <name> <target>
#   A PATH entry named `python3`/`python` that forwards to a venv interpreter and
#   appends one line per invocation to $FACTLOG_TEST_CALLS (used to count probes).
make_shim() {
  local dir="$1" name="$2" target="$3"
  mkdir -p "$dir"
  cat > "$dir/$name" <<SHIM
#!/bin/sh
printf '%s\n' "$name" >> "\${FACTLOG_TEST_CALLS:-/dev/null}"
exec "$target" "\$@"
SHIM
  chmod +x "$dir/$name"
}

# Which interpreter did the launcher pick? Prints the venv id, or nothing.
launcher_pick() {
  bash "$LAUNCHER" -c 'import factlog_test_id, sys; sys.stdout.write(factlog_test_id.ID)' 2>/dev/null
}

launcher_status() {
  local rc=0
  bash "$LAUNCHER" -c 'raise SystemExit(0)' >/dev/null 2>&1 || rc=$?
  printf '%s' "$rc"
}

# ---------------------------------------------------------------------------
# CASE 1: FACTLOG_PYTHON without pyrewire is still honoured (bootstrap path)
#
# `doctor` and `setup` must run *before* the engine exists. If the dependency
# predicate were applied to the explicit selection, this would be exit 127 and
# there would be no way to reach the command that installs pyrewire.
# ---------------------------------------------------------------------------
BARE="$TMP/bare"
make_venv "$BARE" "bare"

export FACTLOG_PYTHON="$(venv_python "$BARE")"
report "FACTLOG_PYTHON without pyrewire is selected" "bare" "$(launcher_pick)"
report "FACTLOG_PYTHON without pyrewire exits 0" "0" "$(launcher_status)"
unset FACTLOG_PYTHON

# ---------------------------------------------------------------------------
# CASE 2: FACTLOG_PYTHON that cannot run Python still exits 127 (unchanged)
# ---------------------------------------------------------------------------
STUB_BIN="$TMP/stub-bin"
mkdir -p "$STUB_BIN"
printf '#!/bin/sh\nexit 1\n' > "$STUB_BIN/python3"
chmod +x "$STUB_BIN/python3"

export FACTLOG_PYTHON="$STUB_BIN/python3"
report "FACTLOG_PYTHON pointing at a Store-stub-like shim exits 127" "127" "$(launcher_status)"
unset FACTLOG_PYTHON

export FACTLOG_PYTHON="$TMP/no-such-python"
report "FACTLOG_PYTHON pointing at a missing file exits 127" "127" "$(launcher_status)"
unset FACTLOG_PYTHON

# ---------------------------------------------------------------------------
# CASE 3: no candidate carries pyrewire — fall back to the version-only choice
#
# This is the bootstrap state doctor/setup must reach; hard-failing here would
# leave the user with no command that can install the engine.
# ---------------------------------------------------------------------------
BOOT_BIN="$TMP/boot-bin"
make_shim "$BOOT_BIN" "python3" "$(venv_python "$BARE")"

export PATH="$BOOT_BIN:/usr/bin:/bin"
report "no pyrewire anywhere still selects a 3.11+ interpreter" "bare" "$(launcher_pick)"
report "no pyrewire anywhere exits 0" "0" "$(launcher_status)"

# ---------------------------------------------------------------------------
echo "----"
echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ]
