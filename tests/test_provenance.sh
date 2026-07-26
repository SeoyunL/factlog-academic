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
printf 'relation("X", "rel", "Y").\nrelation("Acme API", "uses", "FastAPI").\n' > "$KB/facts/accepted.dl"

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

# #273: preserve the existing stderr/rc=1 contract and add only accepted-
# vocabulary hints. Candidate rows must never become spelling candidates.
set +e; typo_out="$("$PYTHON" -m factlog provenance 'Acme AP' --target "$KB" 2>&1)"; rc=$?; set -e
[ "$rc" -eq 1 ] && printf '%s' "$typo_out" | grep -qF "factlog provenance: no fact matches" \
  && ok "typo keeps provenance no-match stderr/rc contract" || bad "typo changed provenance no-match contract"
printf '%s' "$typo_out" | grep -qF "note: no accepted entity 'Acme AP'. did you mean: Acme API?" \
  && ok "zero-result provenance suggests close accepted entity" || bad "provenance entity typo hint missing: $typo_out"
set +e; rel_typo="$("$PYTHON" -m factlog provenance X rell --target "$KB" 2>&1)"; rc=$?; set -e
[ "$rc" -eq 1 ] && printf '%s' "$rel_typo" | grep -qF "did you mean: rel?" \
  && ok "zero-result provenance suggests close accepted relation" || bad "provenance relation typo hint missing: $rel_typo"
printf '%s\n' 'Ghost Typo,unaccepted_relation,Elsewhere,sources/a.md,candidate,0.4,draft only' >> "$KB/facts/candidates.csv"
set +e; candidate_only="$("$PYTHON" -m factlog provenance 'Ghost Typ' --target "$KB" 2>&1)"; rc=$?; set -e
[ "$rc" -eq 1 ] && ! printf '%s' "$candidate_only" | grep -qF 'Ghost Typo' \
  && ok "candidate-only vocabulary never leaks into provenance hint" || bad "candidate-only name leaked into provenance hint: $candidate_only"

# --- all-wildcard (no constraint) -> rc 2 ------------------------------------
set +e; "$PYTHON" -m factlog provenance - - - --target "$KB" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 2 ] && ok "all-wildcard query errors rc 2" || bad "all-wildcard rc wrong ($rc)"

# --- more than 3 terms is an error (likely an unquoted multi-word value) -------
set +e; "$PYTHON" -m factlog provenance X rel Y extra --target "$KB" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -eq 2 ] && ok ">3 terms errors rc 2 (not silently truncated)" || bad ">3 terms rc wrong ($rc)"

# --- confidence is normalized to .2f (consistent with /factlog ask) + blank src
NKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$NKB" >/dev/null
printf 'a\n' > "$NKB/sources/a.md"
printf '%s\n%s\n%s\n%s\n' "subject,relation,object,source,status,confidence,note" \
  'P,rel,Q,sources/a.md,confirmed,.9,raw nine' \
  'P,rel,R,sources/a.md,confirmed,1,raw one' \
  'P,rel,S,,confirmed,0.5,no source cell' > "$NKB/facts/candidates.csv"
nout="$("$PYTHON" -m factlog provenance P --target "$NKB" 2>&1)"
printf '%s' "$nout" | grep -qF "conf 0.90" && ok "confidence '.9' normalized to 0.90" || bad "confidence not normalized: $nout"
printf '%s' "$nout" | grep -qF "conf 1.00" && ok "confidence '1' normalized to 1.00" || bad "confidence '1' not normalized"
printf '%s' "$nout" | grep -qF "← (no source)" && ok "blank source cell shown as (no source)" || bad "blank source not handled"

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
