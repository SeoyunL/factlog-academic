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

# Redirected so CASE 14 can assert the launcher writes nothing here, and so a
# stray write could never land in the developer's real cache directory.
export XDG_CACHE_HOME="$TMP/cache"

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

# How many times did a shimmed interpreter run? One line per invocation, so every
# resolution reads 2: one probe, then the exec. There is no second reading to
# describe — the launcher memoises nothing (CASE 14, which uses this helper), so a
# 1 would mean an interpreter was selected without being executed first.
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
# Belt and braces before an rm -rf: $HOME is this run's sandbox, never the real one.
[ "$HOME" = "$TMP/home" ] || exit 1
rm -rf "$HOME/.virtualenvs"

# ---------------------------------------------------------------------------
# CASE 14: nothing is memoised, and every call re-executes its candidate
#
# A resolution cache would turn the exec decision into a writable artifact: four
# lines of text in a predictably named file were enough to make an earlier
# revision of this launcher exec an arbitrary program. So the launcher must leave
# no readable-back state, and each call must probe the interpreter it execs —
# otherwise a remembered choice could pin an under-floor engine and silently
# downgrade the verification tier.
# ---------------------------------------------------------------------------
NOCACHE_BIN="$TMP/nocache-bin"
make_shim "$NOCACHE_BIN" "python3" "$(venv_python "$ENGINE")"

export PATH="$NOCACHE_BIN:/usr/bin:/bin"
rm -rf "$XDG_CACHE_HOME" "$HOME/.cache"

report "first call probes, then execs" "2" "$(count_calls launcher_pick)"
report "second call probes again — nothing was remembered" "2" "$(count_calls launcher_pick)"
report "third call probes again" "2" "$(count_calls launcher_pick)"

# `|| true`: find exits non-zero when the directories are absent, which is the
# passing case here — pipefail would otherwise abort the harness on success.
leftovers="$({ find "$XDG_CACHE_HOME" "$HOME/.cache" -type f 2>/dev/null || true; } | wc -l | tr -d ' ')"
report "the launcher leaves no state on disk" "0" "$leftovers"

# ---------------------------------------------------------------------------
# CASE 15: an off-PATH choice is announced on EVERY call, not just the first
# ---------------------------------------------------------------------------
make_venv "$DOC_VENV" "documented" "1.0.3"
export PATH="$DOC_BIN:/usr/bin:/bin"

first="$(launcher_stderr)"
second="$(launcher_stderr)"
case "$first" in
  *"$DOC_VENV"*) report "first call announces the off-PATH choice" "named" "named" ;;
  *) report "first call announces the off-PATH choice" "named" "[$first]" ;;
esac
case "$second" in
  *"$DOC_VENV"*) report "second call announces it too" "named" "named" ;;
  *) report "second call announces it too" "named" "[$second]" ;;
esac

# The counter-example, without which the notice has no falsifiable boundary:
# an interpreter that IS on PATH must be selected in silence.
#
# `source .venv/bin/activate` produces exactly this shape — VIRTUAL_ENV set AND
# its bin/ prepended to PATH — so the branch is the common case, not an edge one.
# The selection still carries a reason ("activated virtualenv"), so only the PATH
# guard inside _announce_off_path keeps it quiet. Delete that guard and the two
# assertions above still pass while every activated-venv user gets a line of
# stderr on every plugin command.
ONPATH="$TMP/onpath-venv"
make_venv "$ONPATH" "onpath" "1.0.3"

export PATH="$ONPATH/bin:/usr/bin:/bin"
export VIRTUAL_ENV="$ONPATH"
report "an on-PATH selection is made" "onpath" "$(launcher_pick)"
report "an on-PATH selection says nothing on stderr" "" "$(launcher_stderr)"
unset VIRTUAL_ENV

# ---------------------------------------------------------------------------
# CASE 16: the Windows `py` launcher — the version flag survives probe AND exec
#
# `py` alone may resolve to a Python below the floor, so the flag is not
# decoration: dropping it in either place silently changes which interpreter runs.
# This shim answers only to `-3.12`, so a lost flag shows up as no selection at all.
# ---------------------------------------------------------------------------
PY_BIN="$TMP/py-bin"
mkdir -p "$PY_BIN"
cat > "$PY_BIN/py" <<PYSHIM
#!/bin/sh
printf '%s\n' "py \$1" >> "\${FACTLOG_TEST_CALLS:-/dev/null}"
if [ "\$1" = "-3.12" ]; then
  shift
  exec "$(venv_python "$ENGINE")" "\$@"
fi
exit 1
PYSHIM
chmod +x "$PY_BIN/py"

export PATH="$PY_BIN:/usr/bin:/bin"
report "py -3.12 is probed and exec'd with its flag" "engine" "$(launcher_pick)"

# The same, one row further down the table: only `py -3` answers here.
cat > "$PY_BIN/py" <<PYSHIM
#!/bin/sh
if [ "\$1" = "-3" ]; then
  shift
  exec "$(venv_python "$ENGINE2")" "\$@"
fi
exit 1
PYSHIM
chmod +x "$PY_BIN/py"

report "the py table falls through to -3" "engine2" "$(launcher_pick)"

# And bare `py`, the last row of the table.
cat > "$PY_BIN/py" <<PYSHIM
#!/bin/sh
case "\$1" in
  -3*) exit 1 ;;
esac
exec "$(venv_python "$ENGINE")" "\$@"
PYSHIM
chmod +x "$PY_BIN/py"

report "the py table ends with bare py" "engine" "$(launcher_pick)"

# ---------------------------------------------------------------------------
# CASE 17: the Windows venv layout — `Scripts/python.exe`, not `bin/python`
#
# _venv_interpreter is the only reader of both fixed venv paths, and on Windows
# it is the ONLY row that can match: `python3 -m venv` there writes
# Scripts/python.exe and no bin/. Dropping that row leaves docs/reference/
# windows.md documenting a path the launcher never looks at, and the whole
# Windows story (Git Bash + $FACTLOG_PYTHON on .venv\Scripts\python.exe) rests on
# it. Every other case here builds POSIX venvs, so the row had no coverage at all.
#
# Reproduced on POSIX rather than skipped: the layout is just a directory shape,
# so a root carrying ONLY Scripts/python.exe (no bin/, so the first two rows
# cannot match) forces the third row or nothing.
# ---------------------------------------------------------------------------
WIN_PAYLOAD="$TMP/win-payload"
make_venv "$WIN_PAYLOAD" "windows-layout" "1.0.3"

WIN_ROOT="$TMP/win-venv"
mkdir -p "$WIN_ROOT/Scripts"
cat > "$WIN_ROOT/Scripts/python.exe" <<WINPY
#!/bin/sh
exec "$(venv_python "$WIN_PAYLOAD")" "\$@"
WINPY
chmod +x "$WIN_ROOT/Scripts/python.exe"
# No bin/ here — assert it, or a stray directory would let this case pass on the
# POSIX rows and prove nothing.
[ ! -e "$WIN_ROOT/bin" ] || exit 1

export PATH="$DOC_BIN:/usr/bin:/bin"   # engine-less python3, so PATH cannot win
export VIRTUAL_ENV="$WIN_ROOT"
report "an activated venv is found through Scripts/python.exe" "windows-layout" "$(launcher_pick)"
unset VIRTUAL_ENV

# The same layout at the other fixed path, reached by a different caller.
rm -rf "$DOC_VENV"
mkdir -p "$DOC_VENV/Scripts"
cp "$WIN_ROOT/Scripts/python.exe" "$DOC_VENV/Scripts/python.exe"
chmod +x "$DOC_VENV/Scripts/python.exe"
report "~/.factlog-venv is found through Scripts/python.exe" "windows-layout" "$(launcher_pick)"
rm -rf "$DOC_VENV"

# ---------------------------------------------------------------------------
echo "----"
echo "passed: $pass  failed: $fail"
[ "$fail" -eq 0 ]
