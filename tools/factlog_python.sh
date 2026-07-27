#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Resolve a usable Python 3.11+ interpreter for factlog's plugin scripts.
#
# Windows can expose Microsoft Store stubs as python3/python: the command exists
# but cannot run Python. Every candidate is executed before being selected.

set -euo pipefail

# pyrewire floor, kept as a string so the probe below can parse it with the same
# rule as factlog. Must stay equal to factlog/cli.py MIN_PYREWIRE and
# factlog/common.py MIN_PYREWIRE_VERSION — tests/unit/test_launcher_pyrewire_floor.py
# pins the three together.
_PYREWIRE_FLOOR='1.0.3'

# Both questions the launcher asks ("is this Python 3.11+?" and "does it carry a
# usable engine?") are answered by ONE python invocation, so the dependency check
# costs no extra process spawn — hooks/gate_check.sh execs this runner up to four
# times per gate evaluation.
#
# Exit-code contract:
#   0     — Python 3.11+ AND pyrewire >= floor
#   10    — Python 3.11+ but pyrewire missing or below the floor
#   other — not a usable Python 3.11+ (too old, Store stub, not executable)
#
# The two states are kept distinct because they have different callers: the
# explicit FACTLOG_PYTHON path accepts 10 (bootstrap must work before pyrewire is
# installed), while candidate ranking prefers 0 over 10.
_probe() {
  local rc=0
  "$@" -c '
import sys
if sys.version_info < (3, 11):
    raise SystemExit(3)
try:
    import pyrewire
except Exception:
    raise SystemExit(10)
import re


def parse(value):
    return tuple(int(part) for part in re.findall(r"\d+", value)[:3])


found = parse(str(getattr(pyrewire, "__version__", "0")))
raise SystemExit(0 if found >= parse(sys.argv[1]) else 10)
' "$_PYREWIRE_FLOOR" >/dev/null 2>&1 || rc=$?
  return "$rc"
}

# Runs Python 3.11+, engine availability not considered.
_version_ok() {
  local rc=0
  _probe "$@" || rc=$?
  [ "$rc" -eq 0 ] || [ "$rc" -eq 10 ]
}

if [ -n "${FACTLOG_PYTHON:-}" ]; then
  # Explicit selection deliberately stays version-only. Applying the dependency
  # predicate here would turn a pyrewire-less FACTLOG_PYTHON into exit 127 and cut
  # the `doctor`/`setup` bootstrap path, which has to run *before* pyrewire exists.
  if _version_ok "$FACTLOG_PYTHON"; then
    exec "$FACTLOG_PYTHON" "$@"
  fi
  echo "[factlog] FACTLOG_PYTHON is set but is not a usable Python 3.11+: $FACTLOG_PYTHON" >&2
  exit 127
fi

# PATH candidates in preference order, one per line: a command name plus at most
# one fixed argument (the Windows `py` launcher picks a version by flag). The `py`
# rows get the same ranking as the POSIX ones — nothing here is platform-special.
_PATH_CANDIDATES='python3
python
py -3.12
py -3.11
py -3
py'

# Selected interpreter, as command + optional argument (never an array: bash 3.2
# on macOS makes empty-array expansion under `set -u` a footgun).
_chosen_cmd=''
_chosen_arg=''
# First candidate that runs Python 3.11+ but has no usable engine. Kept as the
# bootstrap fallback: `doctor` must diagnose and `setup` must install from a state
# where pyrewire exists nowhere, so "no engine" can never be a hard failure.
_fallback_cmd=''
_fallback_arg=''

_select() {
  _chosen_cmd="$1"
  _chosen_arg="${2:-}"
}

# One pass over the candidates, not two: re-probing to find the fallback would
# double the process spawns in exactly the environment that has none to spare.
_scan_path_candidates() {
  local line cmd arg rc
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    cmd="${line%% *}"
    arg=''
    if [ "$line" != "$cmd" ]; then
      arg="${line#* }"
    fi
    command -v "$cmd" >/dev/null 2>&1 || continue
    rc=0
    if [ -n "$arg" ]; then
      _probe "$cmd" "$arg" || rc=$?
    else
      _probe "$cmd" || rc=$?
    fi
    if [ "$rc" -eq 0 ]; then
      _select "$cmd" "$arg"
      return 0
    fi
    if [ "$rc" -eq 10 ] && [ -z "$_fallback_cmd" ]; then
      _fallback_cmd="$cmd"
      _fallback_arg="$arg"
    fi
  done <<CANDIDATES
$_PATH_CANDIDATES
CANDIDATES
  return 1
}

if ! _scan_path_candidates; then
  if [ -n "$_fallback_cmd" ]; then
    _select "$_fallback_cmd" "$_fallback_arg"
  fi
fi

if [ -z "$_chosen_cmd" ]; then
  echo "[factlog] no usable Python 3.11+ found. Set FACTLOG_PYTHON to a venv/system python." >&2
  exit 127
fi

if [ -n "$_chosen_arg" ]; then
  exec "$_chosen_cmd" "$_chosen_arg" "$@"
fi
exec "$_chosen_cmd" "$@"
