#!/usr/bin/env bash
# tests/test_provenance.sh — `factlog provenance` (fact → source tracing) (#81)
#
# Pins (XDG-isolated; synthetic data):
#   - exact triple lists every backing row: source path, status, confidence, note
#   - a multi-source fact lists all its distinct sources
#   - partial match: subject-only (prefix) and relation-only (via '-' wildcard)
#   - ALL statuses shown (a superseded backing row is still listed)
#   - [stale] marker when the backing source file is missing on disk
#   - no match -> rc 1; an all-wildcard / no-term query -> rc 2
#   - the `trace` alias works
#
# Usage: bash tests/test_provenance.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
# two real source files (so non-stale rows are non-stale) + facts citing them;
# 'X rel Y' has two sources; 'X attr 2030' has a superseded row; 'G rel H' cites
# a source file that does NOT exist on disk (stale).
printf 'a\n' > "$KB/sources/a.md"
printf 'b\n' > "$KB/sources/b.md"
H="subject,relation,object,source,status,confidence,note"
printf '%s\n%s\n%s\n%s\n%s\n' "$H" \
  'X,rel,Y,sources/a.md,confirmed,0.90,from doc a' \
  'X,rel,Y,sources/b.md,confirmed,0.95,from doc b' \
  'X,attr,2030,sources/a.md,superseded,0.80,retired value' \
  'G,rel,H,sources/gone.md,confirmed,0.70,cites a missing file' > "$KB/facts/candidates.csv"

# --- exact triple: all backing rows with path/status/conf/note ----------------
out="$("$PYTHON" -m factlog provenance X rel Y --target "$KB" 2>&1)"
printf '%s\n' "$out"; echo "---"
printf '%s' "$out" | grep -qF "X / rel / Y" && ok "exact triple: groups under the fact" || bad "fact header missing"
printf '%s' "$out" | grep -qF "← sources/a.md  [confirmed, conf 0.90]" && ok "lists source a with status+confidence" || bad "source a row wrong"
printf '%s' "$out" | grep -qF "← sources/b.md  [confirmed, conf 0.95]" && ok "lists source b (multi-source)" || bad "source b row wrong"
printf '%s' "$out" | grep -qF "note: from doc a" && ok "shows the note (extracted excerpt)" || bad "note missing"
printf '%s' "$out" | grep -qF "2 distinct source(s); 0 stale row(s)" && ok "summary counts distinct sources" || bad "distinct-source summary wrong"

# --- subject-only (positional prefix) -> all facts about X --------------------
out="$("$PYTHON" -m factlog provenance X --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "X / rel / Y" && printf '%s' "$out" | grep -qF "X / attr / 2030" \
  && ok "subject-only lists every fact about the subject" || bad "subject-only incomplete"

# --- a superseded backing row is still shown (all statuses) -------------------
printf '%s' "$out" | grep -qF "[superseded, conf 0.80]" && ok "superseded backing row is shown" || bad "superseded row hidden"

# --- relation-only via '-' wildcard ------------------------------------------
out="$("$PYTHON" -m factlog provenance - rel --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "X / rel / Y" && printf '%s' "$out" | grep -qF "G / rel / H" \
  && ok "relation-only ('- rel') matches across subjects" || bad "relation-only wildcard failed"

# --- object-only via '- -' wildcards -----------------------------------------
out="$("$PYTHON" -m factlog provenance - - Y --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "X / rel / Y" && ok "object-only ('- - Y') matches" || bad "object-only wildcard failed"

# --- stale marker when the source file is missing ----------------------------
out="$("$PYTHON" -m factlog provenance G rel H --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "← sources/gone.md  [confirmed, conf 0.70]  [stale: source missing]" && ok "missing source file marked [stale]" || bad "stale marker missing"
printf '%s' "$out" | grep -qF "1 stale row(s)" && ok "stale row counted in summary" || bad "stale count wrong"

# --- no match -> rc 1 ---------------------------------------------------------
set +e; "$PYTHON" -m factlog provenance nope nope nope --target "$KB" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 1 ] && ok "no match exits rc 1" || bad "no-match rc wrong ($rc)"

# --- all-wildcard (no constraint) -> rc 2 ------------------------------------
set +e; "$PYTHON" -m factlog provenance - - - --target "$KB" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 2 ] && ok "all-wildcard query errors rc 2" || bad "all-wildcard rc wrong ($rc)"

# --- the `trace` alias works --------------------------------------------------
"$PYTHON" -m factlog trace X rel Y --target "$KB" >/dev/null 2>&1 && ok "trace alias works" || bad "trace alias failed"

# --- non-KB path errors -------------------------------------------------------
set +e; "$PYTHON" -m factlog provenance X --target "$(mktemp -d)" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -ne 0 ] && ok "provenance on a non-KB path errors" || bad "non-KB path should error"

echo ""
echo "========================================"
echo "test_provenance: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
