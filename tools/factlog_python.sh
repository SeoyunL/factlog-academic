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

if command -v python3 >/dev/null 2>&1 && _version_ok python3; then
  exec python3 "$@"
fi

if command -v python >/dev/null 2>&1 && _version_ok python; then
  exec python "$@"
fi

if command -v py >/dev/null 2>&1; then
  for version in -3.12 -3.11 -3; do
    if _version_ok py "$version"; then
      exec py "$version" "$@"
    fi
  done
  if _version_ok py; then
    exec py "$@"
  fi
fi

echo "[factlog] no usable Python 3.11+ found. Set FACTLOG_PYTHON to a venv/system python." >&2
exit 127
