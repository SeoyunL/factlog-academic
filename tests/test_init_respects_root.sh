#!/usr/bin/env bash
# `factlog init` (and setup) must scaffold at $FACTLOG_ROOT when no --target is given,
# not at the hardcoded ~/wiki (#247). Ignoring $FACTLOG_ROOT created an unwanted ~/wiki
# while the user believed they were initializing $FACTLOG_ROOT, and every later command
# -- which DOES read $FACTLOG_ROOT -- then pointed at an empty KB.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${FACTLOG_PY:-${PYTHON:-python3}}"
export PYTHONPATH="$PWD"
fails=0
ok() { echo "  ok: $1"; }
bad() { echo "FAIL: $1"; fails=$((fails+1)); }

export XDG_CONFIG_HOME="$(mktemp -d)"  # isolate the active-KB config

# (a) init with $FACTLOG_ROOT set, no --target -> scaffolds there
ENVKB="$(mktemp -d)/envkb"
FACTLOG_ROOT="$ENVKB" "$PY" -m factlog init >/dev/null 2>&1
[ -d "$ENVKB/policy" ] && ok "(a) init scaffolds at \$FACTLOG_ROOT when no --target" \
  || bad "(a) init did not scaffold at \$FACTLOG_ROOT"

# (b) a later command that reads $FACTLOG_ROOT finds a real KB, not an empty one
OUT="$(FACTLOG_ROOT="$ENVKB" "$PY" -m factlog status 2>&1 || true)"
printf '%s' "$OUT" | grep -q "not a factlog KB root" \
  && bad "(b) status found no KB at \$FACTLOG_ROOT after init" \
  || ok "(b) a later \$FACTLOG_ROOT command finds the KB init made"

# (c) --target still wins over $FACTLOG_ROOT, and does not touch $FACTLOG_ROOT
TGT="$(mktemp -d)/tgt"; ENV2="$(mktemp -d)/env2"
FACTLOG_ROOT="$ENV2" "$PY" -m factlog init --target "$TGT" >/dev/null 2>&1
[ -d "$TGT/policy" ] && ok "(c) --target wins over \$FACTLOG_ROOT" || bad "(c) --target ignored"
[ ! -d "$ENV2/policy" ] && ok "(c) \$FACTLOG_ROOT is left untouched when --target is given" \
  || bad "(c) init scaffolded \$FACTLOG_ROOT despite --target"

# (d) setup honours $FACTLOG_ROOT the same way
ENV3="$(mktemp -d)/env3"
FACTLOG_ROOT="$ENV3" "$PY" -m factlog setup --target "$ENV3" >/dev/null 2>&1
[ -d "$ENV3/policy" ] && ok "(d) setup scaffolds where told" || bad "(d) setup did not scaffold"

# (e) _init_target resolution, unit-checked: --target > $FACTLOG_ROOT > ~/wiki, no config
ECASE="$(mktemp -d)/envcase"
RES="$(FACTLOG_ROOT="$ECASE" "$PY" -c "
import sys; sys.path.insert(0, '$PWD')
from pathlib import Path
from factlog.cli import _init_target
import os
print(str(_init_target(None)) == str(Path(os.environ['FACTLOG_ROOT']).expanduser().resolve()))")"
[ "$RES" = "True" ] && ok "(e) _init_target(None) uses \$FACTLOG_ROOT" \
  || bad "(e) _init_target(None) did not use \$FACTLOG_ROOT"
RES2="$(env -u FACTLOG_ROOT "$PY" -c "
import sys, os; sys.path.insert(0, '$PWD')
from pathlib import Path
from factlog.cli import _init_target
print(str(_init_target(None)) == str(Path('~/wiki').expanduser().resolve()))")"
[ "$RES2" = "True" ] && ok "(e) with neither, _init_target falls back to ~/wiki" \
  || bad "(e) fallback is not ~/wiki"

echo
if [ "$fails" -eq 0 ]; then echo "init respects root: all passed"; else echo "init respects root: $fails failed"; exit 1; fi
