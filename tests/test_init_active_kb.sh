#!/usr/bin/env bash
# tests/test_init_active_kb.sh — init must not hijack the active KB (#210)
#
# `factlog init` used to write the active-KB config unconditionally. Scaffolding a
# scratch KB anywhere — another shell, a test harness, an agent — therefore
# retargeted the user's accept/reject/amend/sync at it, silently. The failure
# observed in a real KB was every `accept` returning "no fact matches" because the
# commands were pointed at someone else's scratch KB.
#
# Pins:
#   (a) first init (no active KB configured) adopts the target — the first-run
#       convenience is kept
#   (b) a SECOND init elsewhere leaves the active KB alone and says so
#   (c) the KB is still scaffolded either way (init's actual job)
#   (d) re-init of the ALREADY-active KB keeps it active and says so
#   (e) `factlog use` still switches deliberately
#   (f) --activate is the explicit opt-in for scripts that DO want the new KB
#   (g) a config pointing at a DELETED KB does not trap the user
#
# `setup` also touches the active KB, but it is NOT driven here: it installs
# dependencies before reaching the KB block, so running it in CI's dependency-free
# shell job would trigger `pip install` and reach the network. Its decision
# function is pinned in tests/unit/test_active_kb_adoption.py instead.
#
# Usage: bash tests/test_init_active_kb.sh

set -euo pipefail

# pwd -P: the CLI resolve()s paths, and on macOS mktemp hands back /var/... while
# resolve() yields /private/var/... — compare like with like.
TMP_ROOT="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$TMP_ROOT"' EXIT

export XDG_CONFIG_HOME="$TMP_ROOT/cfg"  # isolate the active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

MINE="$TMP_ROOT/my-kb"
SCRATCH="$TMP_ROOT/scratch-kb"

active() { "$PYTHON" -m factlog where | sed -n '1s/^active KB: //p'; }

# ------------------------------------------------------------------------ (a)
"$PYTHON" -m factlog init --target "$MINE" >/dev/null
if [ "$(active)" = "$MINE" ]; then
  ok "(a) first init adopts the target as the active KB"
else
  bad "(a) first init did not set the active KB (got '$(active)')"
fi

# ------------------------------------------------------------------- (b) + (c)
out="$("$PYTHON" -m factlog init --target "$SCRATCH")"

if [ "$(active)" = "$MINE" ]; then
  ok "(b) a second init elsewhere leaves the active KB alone"
else
  bad "(b) init hijacked the active KB: $MINE -> $(active)"
fi

if grep -q "left unchanged" <<<"$out" && grep -q "factlog use $SCRATCH" <<<"$out"; then
  ok "(b) init says the active KB was left alone and how to switch"
else
  bad "(b) init was silent about not adopting the new KB: $out"
fi

if [ -d "$SCRATCH/sources" ] && [ -d "$SCRATCH/facts" ]; then
  ok "(c) the new KB is still scaffolded"
else
  bad "(c) init did not scaffold $SCRATCH"
fi

# ------------------------------------------------------------------------ (d)
out="$("$PYTHON" -m factlog init --target "$MINE")"
if [ "$(active)" = "$MINE" ] && grep -q "set active KB to $MINE" <<<"$out" && ! grep -q "CHANGED" <<<"$out"; then
  ok "(d) re-init of the already-active KB keeps it active, with no scary CHANGED notice"
else
  bad "(d) re-init of the active KB misbehaved (active='$(active)'): $out"
fi

# ------------------------------------------------------------------------ (e)
"$PYTHON" -m factlog use "$SCRATCH" >/dev/null
if [ "$(active)" = "$SCRATCH" ]; then
  ok "(e) 'factlog use' still switches deliberately"
else
  bad "(e) 'factlog use' failed to switch (got '$(active)')"
fi

# ------------------------------------------------------------------------ (f)
# The opt-in: a script that really does want the new KB says so explicitly,
# instead of relying on the old silent retarget.
"$PYTHON" -m factlog use "$MINE" >/dev/null
out="$("$PYTHON" -m factlog init --target "$SCRATCH" --activate 2>"$TMP_ROOT/err")"
if [ "$(active)" = "$SCRATCH" ]; then
  ok "(f) --activate adopts the new KB on request"
else
  bad "(f) --activate did not adopt the new KB (got '$(active)')"
fi
# Opting in is not a licence to be silent: say what was displaced, like setup does.
if grep -q "CHANGED active KB" <<<"$out" && grep -q "CHANGED active KB" "$TMP_ROOT/err"; then
  ok "(f) --activate names the KB it displaced (stdout and stderr)"
else
  bad "(f) --activate replaced the active KB without naming what it displaced"
fi

# ------------------------------------------------------------------------ (g)
# A config left pointing at a deleted KB must not make init refuse forever.
GONE="$TMP_ROOT/gone-kb"
"$PYTHON" -m factlog init --target "$GONE" --activate >/dev/null
rm -rf "$GONE"
FRESH="$TMP_ROOT/fresh-kb"
"$PYTHON" -m factlog init --target "$FRESH" >/dev/null
if [ "$(active)" = "$FRESH" ]; then
  ok "(g) a deleted active KB does not trap the user"
else
  bad "(g) init refused to adopt after the active KB was deleted (got '$(active)')"
fi

echo "---"
echo "passed: $pass, failed: $fail"
[ "$fail" -eq 0 ]
