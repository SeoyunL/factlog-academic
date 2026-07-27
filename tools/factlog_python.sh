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
# costs no extra process spawn.
#
# How many times hooks/gate_check.sh execs this runner per gate evaluation is a
# RANGE, not a constant. Three earlier readings of this comment reported three
# different numbers because each measured one point of that range. Measured, and
# pinned by CASE 19 of tests/test_gate_check.sh:
#
#   3 execs — the payload carries no usable target path (no file_path key, or
#             unparseable JSON): the gate exits before canonicalising anything
#   5 execs — the target canonicalises to <KB_ROOT>/facts/accepted.dl, so the
#             engine-input loop matches on its first entry and breaks
#   6 execs — every other target: <KB_ROOT>/facts/query.dl (matches on the second
#             loop entry) or a non-engine path (loop runs both, matches neither)
#
# KB state is NOT a determinant. Whether logic_report.txt exists, whether the
# engine inputs exist, and whether the verdict is allow or deny all change the
# exit code and change nothing here: every _canon call above the verdict runs
# unconditionally. Only the target's identity moves the number.
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

# No resolution cache lives here, and that is deliberate.
#
# An earlier revision of this script memoised the choice to a file under
# ~/.cache/factlog so the repeated execs in hooks/gate_check.sh could skip the
# probe. Two things killed it:
#
#   * It turned the exec decision into a writable artifact. Four lines of text in
#     a predictably named 0644 file were enough to make this script exec an
#     arbitrary program. The fixed paths above are read-only conventions; a cache
#     entry is not, and the argument that justifies the former does not carry.
#   * Once a cache hit is required to re-probe (it must be — otherwise a stale
#     entry can pin an under-floor engine and silently downgrade finalize to
#     "logic check SKIPPED"), the saving mostly evaporates. Measured on one gate
#     evaluation with a facts/accepted.dl target — five execs of this script, the
#     middle row of the table above: 437 ms with no cache versus 478 ms with a
#     re-validating cache when PATH already carries the engine, i.e. the common
#     case got SLOWER, before adding the ownership/symlink/whitelist checks a
#     writable cache would also need.
#
# Every selection below therefore executes the candidate it selects, on every
# call, exactly as the header promises.
#
# What that costs relative to the launcher BEFORE #578 (the number a user
# actually experiences) — same fixture, same five-exec target, engine first on
# PATH, macOS/arm64, mean of 20 evaluations:
#
#   engine on PATH            pre-#578    #578      delta
#   real pyrewire 1.0.3        358 ms    637 ms    +279 ms  (+78%)
#   stub pyrewire (test venv)  382 ms    389 ms      +8 ms   (+2%)
#
# The two rows differ by two orders of magnitude, and the reason is the whole
# point: the added cost is one `import pyrewire` per exec, so it is the ENGINE's
# import time, not this script's logic. Here that import is 55 ms (31 ms bare
# interpreter vs 86 ms with the import), and 5 x 55 accounts for the +279 ms
# almost exactly. tests/test_factlog_python.sh plants a one-line stub pyrewire,
# so any timing taken against the fixtures measures the bottom row and will
# understate the real cost roughly 35-fold. Quote the top row.
#
# +0.28 s per gate evaluation is not free. The lever is not this probe but the
# number of execs it is multiplied by: a perfect zero-cost cache could still only
# remove the probe half, and the remaining execs stay.

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
    return 0
  fi
  return 1
}

_resolve || true

if [ -z "$_chosen_cmd" ]; then
  echo "[factlog] no usable Python 3.11+ found. Set FACTLOG_PYTHON to a venv/system python." >&2
  exit 127
fi

if [ -n "$_chosen_reason" ]; then
  _announce_off_path "$_chosen_cmd" "$_chosen_reason"
fi

if [ -n "$_chosen_arg" ]; then
  exec "$_chosen_cmd" "$_chosen_arg" "$@"
fi
exec "$_chosen_cmd" "$@"
