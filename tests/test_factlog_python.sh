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

# Resolution caching is memoisation, not selection. Switch it off so every case
# measures the decision itself, and turn it back on only where the cache is the
# subject — otherwise a case could be answered by the previous case's entry.
export XDG_CACHE_HOME="$TMP/cache"
export FACTLOG_PYTHON_CACHE=0

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

# Everything the launcher itself said, with the payload's own stdout discarded.
launcher_stderr() {
  bash "$LAUNCHER" -c 'raise SystemExit(0)' 2>&1 >/dev/null || true
}

launcher_status() {
  local rc=0
  bash "$LAUNCHER" -c 'raise SystemExit(0)' >/dev/null 2>&1 || rc=$?
  printf '%s' "$rc"
}

# How many times did a shimmed interpreter run? One line per invocation, so a
# cold resolution reads 2 (one probe + the exec) and a cache hit reads 1.
count_calls() {
  : > "$TMP/calls"
  export FACTLOG_TEST_CALLS="$TMP/calls"
  "$@" >/dev/null 2>&1 || true
  wc -l < "$TMP/calls" | tr -d ' '
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
# CASE 4: a candidate carrying the engine outranks an earlier one without it
# ---------------------------------------------------------------------------
ENGINE="$TMP/engine"
make_venv "$ENGINE" "engine" "1.0.3"

RANK_BIN="$TMP/rank-bin"
make_shim "$RANK_BIN" "python3" "$(venv_python "$BARE")"
make_shim "$RANK_BIN" "python" "$(venv_python "$ENGINE")"

export PATH="$RANK_BIN:/usr/bin:/bin"
report "later candidate with pyrewire beats earlier one without" "engine" "$(launcher_pick)"

# ---------------------------------------------------------------------------
# CASE 5: the ranking honours the version floor, not mere importability
#
# pyrewire 0.9 imports fine but is below MIN_PYREWIRE, and finalize downgrades to
# "Logic check SKIPPED" on it — silently lowering the verification tier. So an
# under-floor engine must NOT outrank a candidate that has the real floor.
# ---------------------------------------------------------------------------
OLD_ENGINE="$TMP/old-engine"
make_venv "$OLD_ENGINE" "old-engine" "0.9.0"

FLOOR_BIN="$TMP/floor-bin"
make_shim "$FLOOR_BIN" "python3" "$(venv_python "$OLD_ENGINE")"
make_shim "$FLOOR_BIN" "python" "$(venv_python "$ENGINE")"

export PATH="$FLOOR_BIN:/usr/bin:/bin"
report "pyrewire below the floor does not outrank the floor" "engine" "$(launcher_pick)"

# ---------------------------------------------------------------------------
# CASE 6: with the engine everywhere, the documented candidate order still wins
# ---------------------------------------------------------------------------
ENGINE2="$TMP/engine2"
make_venv "$ENGINE2" "engine2" "1.0.3"

ORDER_BIN="$TMP/order-bin"
make_shim "$ORDER_BIN" "python3" "$(venv_python "$ENGINE")"
make_shim "$ORDER_BIN" "python" "$(venv_python "$ENGINE2")"

export PATH="$ORDER_BIN:/usr/bin:/bin"
report "equally equipped candidates keep python3 first" "engine" "$(launcher_pick)"

# ---------------------------------------------------------------------------
# CASE 7: the bootstrap fallback also keeps the documented order
# ---------------------------------------------------------------------------
BARE2="$TMP/bare2"
make_venv "$BARE2" "bare2"

FB_BIN="$TMP/fallback-bin"
make_shim "$FB_BIN" "python3" "$(venv_python "$BARE")"
make_shim "$FB_BIN" "python" "$(venv_python "$BARE2")"

export PATH="$FB_BIN:/usr/bin:/bin"
report "engine-less fallback keeps python3 first" "bare" "$(launcher_pick)"

# ---------------------------------------------------------------------------
# CASE 8: the reported bug — PATH has no engine, ~/.factlog-venv does
#
# SKILL.md's PEP 668 fallback tells the user to create exactly this venv, so a
# user who followed the documentation must not be stuck on the engine-less
# python3 that happens to come first on PATH.
# ---------------------------------------------------------------------------
DOC_VENV="$HOME/.factlog-venv"
make_venv "$DOC_VENV" "documented" "1.0.3"

DOC_BIN="$TMP/doc-bin"
make_shim "$DOC_BIN" "python3" "$(venv_python "$BARE")"

export PATH="$DOC_BIN:/usr/bin:/bin"
report "engine-less PATH falls through to ~/.factlog-venv" "documented" "$(launcher_pick)"

# The choice leaves PATH, so it must not be silent.
notice="$(launcher_stderr)"
case "$notice" in
  *"$DOC_VENV"*) report "off-PATH choice names the interpreter on stderr" "named" "named" ;;
  *) report "off-PATH choice names the interpreter on stderr" "named" "[$notice]" ;;
esac

# ---------------------------------------------------------------------------
# CASE 9: a PATH candidate that DOES carry the engine outranks ~/.factlog-venv
#
# ~/.factlog-venv is a fallback for a PATH that cannot serve, not an override.
# ---------------------------------------------------------------------------
PATH_ENGINE_BIN="$TMP/path-engine-bin"
make_shim "$PATH_ENGINE_BIN" "python3" "$(venv_python "$ENGINE")"

export PATH="$PATH_ENGINE_BIN:/usr/bin:/bin"
report "PATH candidate with the engine outranks ~/.factlog-venv" "engine" "$(launcher_pick)"

# ---------------------------------------------------------------------------
# CASE 10: ~/.factlog-venv is consulted before the engine-less PATH fallback
#
# Neither has the engine here. The documented venv still wins, because it is the
# one `setup` can pip-install into — the PEP 668 interpreter on PATH is not.
# ---------------------------------------------------------------------------
rm -rf "$DOC_VENV"
make_venv "$DOC_VENV" "documented-bare"

export PATH="$DOC_BIN:/usr/bin:/bin"
report "engine-less ~/.factlog-venv still precedes the PATH fallback" "documented-bare" "$(launcher_pick)"

# ---------------------------------------------------------------------------
# CASE 11: an activated virtualenv outranks everything except FACTLOG_PYTHON
#
# Activating a venv is an explicit signal, so it wins even without the engine —
# otherwise `setup` could not install pyrewire into the venv the user activated.
# ---------------------------------------------------------------------------
ACTIVE="$TMP/active"
make_venv "$ACTIVE" "active"

export PATH="$PATH_ENGINE_BIN:/usr/bin:/bin"
export VIRTUAL_ENV="$ACTIVE"
report "activated virtualenv outranks a PATH candidate with the engine" "active" "$(launcher_pick)"

# ---------------------------------------------------------------------------
# CASE 12: a stale VIRTUAL_ENV pointing nowhere falls through instead of failing
# ---------------------------------------------------------------------------
export VIRTUAL_ENV="$TMP/deleted-venv"
report "stale VIRTUAL_ENV falls through to the normal order" "engine" "$(launcher_pick)"
unset VIRTUAL_ENV

# ---------------------------------------------------------------------------
# CASE 13: no venv discovery — a venv that is not one of the fixed paths is
# invisible, even when it is the only interpreter carrying the engine.
# ---------------------------------------------------------------------------
rm -rf "$DOC_VENV"
mkdir -p "$HOME/.virtualenvs"
ELSEWHERE="$HOME/.virtualenvs/factlog"
make_venv "$ELSEWHERE" "elsewhere" "1.0.3"

export PATH="$DOC_BIN:/usr/bin:/bin"
report "a venv outside the fixed paths is never discovered" "bare" "$(launcher_pick)"
rm -rf "$HOME/.virtualenvs"

# ---------------------------------------------------------------------------
# CASE 14: the resolution is cached across processes
#
# hooks/gate_check.sh execs this launcher up to four times per gate evaluation and
# each exec is a fresh process, so the memo has to survive on disk or it saves
# nothing. Cold: one probe plus the exec. Warm: the exec alone.
# ---------------------------------------------------------------------------
CACHE_BIN="$TMP/cache-bin"
make_shim "$CACHE_BIN" "python3" "$(venv_python "$ENGINE")"

export PATH="$CACHE_BIN:/usr/bin:/bin"
export FACTLOG_PYTHON_CACHE=1
rm -rf "$XDG_CACHE_HOME"

report "cold resolution probes the candidate" "2" "$(count_calls launcher_pick)"
report "warm resolution spawns no probe" "1" "$(count_calls launcher_pick)"
report "the cached choice is the same interpreter" "engine" "$(launcher_pick)"

# ---------------------------------------------------------------------------
# CASE 15: the cache is switchable off, and a zero TTL means never trust it
# ---------------------------------------------------------------------------
export FACTLOG_PYTHON_CACHE=0
report "FACTLOG_PYTHON_CACHE=0 re-resolves" "2" "$(count_calls launcher_pick)"

export FACTLOG_PYTHON_CACHE=1
export FACTLOG_PYTHON_CACHE_TTL=0
report "a zero TTL re-resolves" "2" "$(count_calls launcher_pick)"
unset FACTLOG_PYTHON_CACHE_TTL

# ---------------------------------------------------------------------------
# CASE 16: PATH is part of the cache key
# ---------------------------------------------------------------------------
CACHE_BIN2="$TMP/cache-bin2"
make_shim "$CACHE_BIN2" "python3" "$(venv_python "$ENGINE2")"

export PATH="$CACHE_BIN2:/usr/bin:/bin"
report "a different PATH is not answered from the old entry" "engine2" "$(launcher_pick)"

# ---------------------------------------------------------------------------
# CASE 17: the engine-less fallback is never cached
#
# It is the one outcome the user is expected to leave immediately — `setup`
# installs pyrewire into it — and a cached fallback would keep serving the
# engine-less interpreter for a TTL after that install succeeded.
# ---------------------------------------------------------------------------
FB_CACHE_BIN="$TMP/fb-cache-bin"
make_shim "$FB_CACHE_BIN" "python3" "$(venv_python "$BARE")"

export PATH="$FB_CACHE_BIN:/usr/bin:/bin"
rm -rf "$XDG_CACHE_HOME"
report "engine-less fallback, first run" "2" "$(count_calls launcher_pick)"
report "engine-less fallback is re-resolved, not remembered" "2" "$(count_calls launcher_pick)"

# ---------------------------------------------------------------------------
# CASE 18: a cache hit still discloses an off-PATH choice
#
# Announcing only on the cold run would make every call after the first exactly
# the silent selection the note exists to prevent.
# ---------------------------------------------------------------------------
make_venv "$DOC_VENV" "documented" "1.0.3"

export PATH="$DOC_BIN:/usr/bin:/bin"
rm -rf "$XDG_CACHE_HOME"
report "off-PATH choice survives caching" "documented" "$(launcher_pick)"

# Prove the next call is a hit, not a second cold resolution.
if ls "$XDG_CACHE_HOME"/factlog/python-* >/dev/null 2>&1; then
  report "the off-PATH choice was written to the cache" "cached" "cached"
else
  report "the off-PATH choice was written to the cache" "cached" "missing"
fi

notice="$(launcher_stderr)"
case "$notice" in
  *"$DOC_VENV"*) report "cached off-PATH choice is still announced" "named" "named" ;;
  *) report "cached off-PATH choice is still announced" "named" "[$notice]" ;;
esac

# ---------------------------------------------------------------------------
echo "----"
echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ]
