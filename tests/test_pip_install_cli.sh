#!/usr/bin/env bash
# Verifies a normal (non-editable) pip install can import and run factlog outside
# the source tree. Installs --no-deps so the test stays offline; doctor may
# report missing pyrewire, but it must reach the diagnostic code without a
# ModuleNotFoundError/traceback from package layout.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

PYTHON="${PYTHON:-python3}"
VENV="$TMP/venv"
"$PYTHON" -m venv "$VENV"

VENV_PY="$VENV/bin/python"
VENV_FACTLOG="$VENV/bin/factlog"
if [ ! -x "$VENV_PY" ]; then
  VENV_PY="$VENV/Scripts/python.exe"
  VENV_FACTLOG="$VENV/Scripts/factlog.exe"
fi

"$VENV_PY" -m pip install --no-deps "$PLUGIN_ROOT" >/dev/null

cd "$TMP"

"$VENV_PY" -m factlog --version >/tmp/factlog-pip-version.out
"$VENV_FACTLOG" --version >/tmp/factlog-pip-script-version.out

set +e
"$VENV_PY" -m factlog doctor > /tmp/factlog-pip-doctor.out 2>&1
doctor_rc=$?
"$VENV_FACTLOG" doctor > /tmp/factlog-pip-script-doctor.out 2>&1
script_doctor_rc=$?
set -e

if [ "$doctor_rc" -ne 0 ] && [ "$doctor_rc" -ne 1 ]; then
  echo "FAIL: doctor exited with unexpected status $doctor_rc" >&2
  cat /tmp/factlog-pip-doctor.out >&2
  exit 1
fi

if [ "$script_doctor_rc" -ne 0 ] && [ "$script_doctor_rc" -ne 1 ]; then
  echo "FAIL: console doctor exited with unexpected status $script_doctor_rc" >&2
  cat /tmp/factlog-pip-script-doctor.out >&2
  exit 1
fi

if grep -Eq "Traceback|ModuleNotFoundError|No module named 'factlog_config'|No module named 'common'" \
    /tmp/factlog-pip-doctor.out /tmp/factlog-pip-script-doctor.out; then
  echo "FAIL: installed factlog doctor failed during import" >&2
  cat /tmp/factlog-pip-doctor.out >&2
  cat /tmp/factlog-pip-script-doctor.out >&2
  exit 1
fi

if ! grep -q "Python" /tmp/factlog-pip-doctor.out; then
  echo "FAIL: doctor did not reach Python diagnostic output" >&2
  cat /tmp/factlog-pip-doctor.out >&2
  exit 1
fi

if ! grep -q "Python" /tmp/factlog-pip-script-doctor.out; then
  echo "FAIL: console doctor did not reach Python diagnostic output" >&2
  cat /tmp/factlog-pip-script-doctor.out >&2
  exit 1
fi

echo "PASS: non-editable pip install runs module and console CLI outside repo"
