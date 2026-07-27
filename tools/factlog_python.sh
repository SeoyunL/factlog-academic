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
# Why an off-PATH interpreter won, for the stderr note. Empty for PATH candidates.
_chosen_reason=''
# Set when the selection is the engine-less bootstrap fallback; that outcome is
# never cached (see below).
_chosen_is_fallback=0
# First candidate that runs Python 3.11+ but has no usable engine. Kept as the
# bootstrap fallback: `doctor` must diagnose and `setup` must install from a state
# where pyrewire exists nowhere, so "no engine" can never be a hard failure.
_fallback_cmd=''
_fallback_arg=''

_select() {
  _chosen_cmd="$1"
  _chosen_arg="${2:-}"
  _chosen_reason="${3:-}"
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

# Interpreter inside a venv root, if that root has one. Fixed layouts only —
# `bin/` on POSIX, `Scripts/` on Windows. No globbing, no walking upwards.
_venv_interpreter() {
  local root="$1" relative
  for relative in bin/python bin/python3 Scripts/python.exe; do
    if [ -x "$root/$relative" ]; then
      printf '%s' "$root/$relative"
      return 0
    fi
  done
  return 1
}

# Never pick an interpreter from outside PATH silently (#578). The two fixed
# paths below are conventions, not discoveries, but they still shift which engine
# every subsequent command runs on — so say which one won, once, on stderr.
_announce_off_path() {
  local exe="$1" reason="$2" dir="${1%/*}"
  case ":${PATH:-}:" in
    *":$dir:"*) return 0 ;;
  esac
  echo "[factlog] using $exe ($reason)" >&2
}

# The activated virtualenv is an explicit user signal, not a search result, so it
# outranks PATH — and version-only is enough, exactly as for FACTLOG_PYTHON:
# `setup` has to be able to install the engine INTO the venv the user activated.
# ---------------------------------------------------------------------------
# Resolution cache
#
# hooks/gate_check.sh execs this runner up to four times per gate evaluation, and
# every exec is a fresh process — an in-memory memo would help nothing. So the
# resolved choice is written to one small file keyed by the inputs that decide it
# (PATH, VIRTUAL_ENV, HOME, the floor). A hit costs no python spawn at all.
#
# Staleness is bounded three ways: the key changes when the environment does, the
# command must still resolve, and the entry expires after a TTL.
# ---------------------------------------------------------------------------
_CACHE_FORMAT=1  # bump when the file layout below changes
_cache_file=''
_cache_now=''

_cache_ttl() {
  local ttl="${FACTLOG_PYTHON_CACHE_TTL:-600}"
  case "$ttl" in
    ''|*[!0-9]*) ttl=600 ;;
  esac
  printf '%s' "$ttl"
}

_cache_init() {
  [ "${FACTLOG_PYTHON_CACHE:-1}" != "0" ] || return 1
  [ "$(_cache_ttl)" -gt 0 ] || return 1
  command -v cksum >/dev/null 2>&1 || return 1
  _cache_now="$(date +%s 2>/dev/null || true)"
  case "$_cache_now" in
    ''|*[!0-9]*) return 1 ;;
  esac
  local dir key
  if [ -n "${XDG_CACHE_HOME:-}" ]; then
    dir="$XDG_CACHE_HOME/factlog"
  elif [ -n "${HOME:-}" ]; then
    dir="$HOME/.cache/factlog"
  else
    return 1
  fi
  key="$(printf '%s\n%s\n%s\n%s\n%s\n' \
    "$_CACHE_FORMAT" "${PATH:-}" "${VIRTUAL_ENV:-}" "${HOME:-}" "$_PYREWIRE_FLOOR" \
    | cksum | tr -cd '0-9')"
  [ -n "$key" ] || return 1
  _cache_file="$dir/python-$key"
}

_cache_load() {
  _cache_init || return 1
  [ -r "$_cache_file" ] || return 1
  local line stamp='' cmd='' arg='' reason='' n=0 age
  while IFS= read -r line; do
    n=$((n + 1))
    case "$n" in
      1) stamp="$line" ;;
      2) cmd="$line" ;;
      3) arg="$line" ;;
      4) reason="$line" ;;
    esac
  done < "$_cache_file"
  [ "$n" -eq 4 ] || return 1
  case "$stamp" in
    ''|*[!0-9]*) return 1 ;;
  esac
  age=$((_cache_now - stamp))
  # A negative age means the clock moved backwards; distrust the entry.
  [ "$age" -ge 0 ] && [ "$age" -lt "$(_cache_ttl)" ] || return 1
  [ -n "$cmd" ] || return 1
  command -v "$cmd" >/dev/null 2>&1 || return 1
  _select "$cmd" "$arg" "$reason"
}

_cache_store() {
  [ -n "$_cache_file" ] || return 0
  local dir="${_cache_file%/*}" tmp="$_cache_file.$$"
  mkdir -p "$dir" 2>/dev/null || return 0
  if printf '%s\n%s\n%s\n%s\n' \
      "$_cache_now" "$_chosen_cmd" "$_chosen_arg" "$_chosen_reason" > "$tmp" 2>/dev/null; then
    mv -f "$tmp" "$_cache_file" 2>/dev/null || rm -f "$tmp" 2>/dev/null || true
  fi
  return 0
}

_resolve() {
  # The activated virtualenv is an explicit user signal, not a search result, so it
  # outranks PATH — and version-only is enough, exactly as for FACTLOG_PYTHON:
  # `setup` has to be able to install the engine INTO the venv the user activated.
  local virtual_env_interpreter='' documented_venv=''
  if [ -n "${VIRTUAL_ENV:-}" ]; then
    virtual_env_interpreter="$(_venv_interpreter "$VIRTUAL_ENV" || true)"
  fi
  if [ -n "$virtual_env_interpreter" ] && _version_ok "$virtual_env_interpreter"; then
    _select "$virtual_env_interpreter" '' "activated virtualenv"
    return 0
  fi

  _scan_path_candidates && return 0

  # skills/factlog/SKILL.md's PEP 668 fallback tells the user to create exactly
  # this venv, so honouring it is a documented convention rather than a scan. It
  # is consulted only after PATH failed to produce an engine — and, like the rest
  # of the fixed paths, it is one hardcoded location: no globbing, no discovery.
  if [ -n "${HOME:-}" ]; then
    documented_venv="$(_venv_interpreter "$HOME/.factlog-venv" || true)"
  fi
  if [ -n "$documented_venv" ] && _version_ok "$documented_venv"; then
    _select "$documented_venv" '' "PATH has no python with pyrewire >= $_PYREWIRE_FLOOR"
    return 0
  fi

  if [ -n "$_fallback_cmd" ]; then
    _select "$_fallback_cmd" "$_fallback_arg"
    _chosen_is_fallback=1
    return 0
  fi
  return 1
}

if ! _cache_load; then
  _resolve || true
  # The engine-less fallback is never cached. It is the one outcome the user is
  # expected to leave immediately (`setup` installs pyrewire), and pinning it for
  # a TTL would keep serving the engine-less interpreter after the install
  # succeeded. Every other outcome is stable for as long as its key holds.
  if [ -n "$_chosen_cmd" ] && [ "$_chosen_is_fallback" -eq 0 ]; then
    _cache_store
  fi
fi

if [ -z "$_chosen_cmd" ]; then
  echo "[factlog] no usable Python 3.11+ found. Set FACTLOG_PYTHON to a venv/system python." >&2
  exit 127
fi

# Also fires on a cache hit: the disclosure must not disappear once the choice is
# remembered, or the second call onwards would be exactly the silent selection
# this note exists to prevent.
if [ -n "$_chosen_reason" ]; then
  _announce_off_path "$_chosen_cmd" "$_chosen_reason"
fi

if [ -n "$_chosen_arg" ]; then
  exec "$_chosen_cmd" "$_chosen_arg" "$@"
fi
exec "$_chosen_cmd" "$@"
