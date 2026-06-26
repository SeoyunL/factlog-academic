#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Resolve a usable Python 3.11+ interpreter for factlog's plugin scripts.
#
# Windows can expose Microsoft Store stubs as python3/python: the command exists
# but cannot run Python. Every candidate is executed before being selected.

set -euo pipefail

_ok_python() {
  "$@" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1
}

if [ -n "${FACTLOG_PYTHON:-}" ]; then
  if _ok_python "$FACTLOG_PYTHON"; then
    exec "$FACTLOG_PYTHON" "$@"
  fi
  echo "[factlog] FACTLOG_PYTHON is set but is not a usable Python 3.11+: $FACTLOG_PYTHON" >&2
  exit 127
fi

if command -v python3 >/dev/null 2>&1 && _ok_python python3; then
  exec python3 "$@"
fi

if command -v python >/dev/null 2>&1 && _ok_python python; then
  exec python "$@"
fi

if command -v py >/dev/null 2>&1; then
  for version in -3.12 -3.11 -3; do
    if _ok_python py "$version"; then
      exec py "$version" "$@"
    fi
  done
  if _ok_python py; then
    exec py "$@"
  fi
fi

echo "[factlog] no usable Python 3.11+ found. Set FACTLOG_PYTHON to a venv/system python." >&2
exit 127
