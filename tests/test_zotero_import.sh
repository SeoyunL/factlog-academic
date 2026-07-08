#!/usr/bin/env bash
# tests/test_zotero_import.sh — CLI surface of `factlog zotero-import` (Zotero phase 1)
#
# Pins the hermetic (no live Zotero) behaviour of the command: argument parsing,
# selector rules, KB/target validation, and graceful error exit codes. The happy
# path (a real fetch -> parse -> write) is covered deterministically by the unit
# tests with a fake client (tests/unit/test_zotero_{client,importer,cli}.py) and
# by a manual live smoke against a running Zotero Local API; it is intentionally
# NOT exercised here so this test never depends on a running Zotero app or on the
# optional pyzotero extra.
#
# All cases below fail BEFORE the Zotero client is constructed (argparse, the
# _require_kb gate, selector normalisation, or KB config loading), so they are
# deterministic in CI.
#
# Usage: PYTHON=~/.factlog-venv/bin/python bash tests/test_zotero_import.sh

set -uo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

fl() { "$PYTHON" -m factlog "$@"; }

# Isolate user-level config discovery so load_config never reads the developer's
# real ~/.config/factlog/zotero.toml (which, if malformed, would derail the
# no-policy cases). Keeps the harness hermetic as its header claims.
export XDG_CONFIG_HOME="$(mktemp -d)/cfg"
export HOME="$(mktemp -d)/home"

# A valid KB (has sources/) and one with a malformed Zotero policy file.
KB="$(mktemp -d)/wiki"; mkdir -p "$KB/sources"
BADCFG="$(mktemp -d)/wiki"; mkdir -p "$BADCFG/sources/" "$BADCFG/policy"
printf 'this = = not toml\n' > "$BADCFG/policy/zotero-config.toml"
NOKB="$(mktemp -d)/plain"; mkdir -p "$NOKB"  # no sources/

# --- 1. --help lists the selectors and options ------------------------------
out="$(fl zotero-import --help 2>&1)"; rc=$?
missing=""
for opt in --collection --tag --items --target --dry-run --porcelain --pdf --annotations; do
  grep -q -- "$opt" <<<"$out" || missing="$missing $opt"
done
if [ "$rc" -eq 0 ] && [ -z "$missing" ]; then
  ok "--help documents options"
else
  bad "--help (rc=$rc, missing:$missing): $out"
fi

# --- 2. missing selector is an argparse error (exit 2) -----------------------
out="$(fl zotero-import --target "$KB" 2>&1)"; rc=$?
if [ "$rc" -eq 2 ]; then ok "missing selector -> exit 2"; else bad "missing selector rc=$rc: $out"; fi

# --- 3. two selectors are mutually exclusive (exit 2) ------------------------
out="$(fl zotero-import --collection A --tag b --target "$KB" 2>&1)"; rc=$?
if [ "$rc" -eq 2 ]; then ok "mutually exclusive selectors -> exit 2"; else bad "two selectors rc=$rc: $out"; fi

# --- 4. target that is not a KB -> exit 1 with guidance ----------------------
out="$(fl zotero-import --tag t --target "$NOKB" 2>&1)"; rc=$?
if [ "$rc" -eq 1 ] && grep -q "not a factlog KB" <<<"$out" && ! grep -q "Traceback" <<<"$out"; then
  ok "non-KB target -> graceful exit 1"
else
  bad "non-KB target rc=$rc: $out"
fi

# --- 5. empty --items -> exit 1 with guidance -------------------------------
out="$(fl zotero-import --items ' , , ' --target "$KB" 2>&1)"; rc=$?
if [ "$rc" -eq 1 ] && grep -q "at least one item key" <<<"$out" && ! grep -q "Traceback" <<<"$out"; then
  ok "empty --items -> graceful exit 1"
else
  bad "empty --items rc=$rc: $out"
fi

# --- 5b. blank --collection -> exit 1 (uniform with --items) -----------------
out="$(fl zotero-import --collection '   ' --target "$KB" 2>&1)"; rc=$?
if [ "$rc" -eq 1 ] && grep -q "non-empty name" <<<"$out" && ! grep -q "Traceback" <<<"$out"; then
  ok "blank --collection -> graceful exit 1"
else
  bad "blank --collection rc=$rc: $out"
fi

# --- 6. malformed KB Zotero config -> exit 1, not a traceback ----------------
out="$(fl zotero-import --tag t --target "$BADCFG" 2>&1)"; rc=$?
if [ "$rc" -eq 1 ] && grep -q "invalid TOML" <<<"$out" && ! grep -q "Traceback" <<<"$out"; then
  ok "malformed KB config -> graceful exit 1"
else
  bad "malformed config rc=$rc: $out"
fi

# --- 7. --pdf is accepted and still gated by the KB check (hermetic) ---------
# --pdf reaches the same _require_kb gate before any Zotero/PDF work, so a
# non-KB target fails gracefully — proving the flag parses without a live client.
out="$(fl zotero-import --collection Foo --pdf --target "$NOKB" 2>&1)"; rc=$?
if [ "$rc" -eq 1 ] && grep -q "not a factlog KB" <<<"$out" && ! grep -q "Traceback" <<<"$out"; then
  ok "--pdf accepted, still gated by KB check"
else
  bad "--pdf gating rc=$rc: $out"
fi

# --- 8. --annotations is accepted and still gated by the KB check ------------
out="$(fl zotero-import --collection Foo --annotations --target "$NOKB" 2>&1)"; rc=$?
if [ "$rc" -eq 1 ] && grep -q "not a factlog KB" <<<"$out" && ! grep -q "Traceback" <<<"$out"; then
  ok "--annotations accepted, still gated by KB check"
else
  bad "--annotations gating rc=$rc: $out"
fi

echo
echo "zotero-import CLI surface: $pass passed, $fail failed"
[ "$fail" -eq 0 ]
