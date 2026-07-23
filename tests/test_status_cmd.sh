#!/usr/bin/env bash
# tests/test_status_cmd.sh — `factlog status` KB-state summary (#68)
#
# Pins (XDG-isolated; synthetic data; no pyrewire needed — the engine line
# degrades gracefully and the rest is pure):
#   - facts by status + engine-fact count; vocabulary (entities/literals/relations)
#   - source count + how many carry facts (NFC-matched)
#   - conflicts: n/a with no single-valued relations; counted when declared
#   - logic-report freshness (fresh vs STALE when an input is newer) + errors/warnings
#   - uses the active KB with no --target; errors on a non-KB path
#
# Usage: bash tests/test_status_cmd.sh

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
"$PYTHON" -m factlog init --target "$KB" --activate >/dev/null   # records active KB (temp -> --activate, #461)
H="subject,relation,object,source,status,confidence,note"
printf '%s\n%s\n%s\n%s\n' "$H" \
  '갑봇,통합,을서비스,sources/a.md,confirmed,0.9,' \
  '갑봇,운영,2030.1,sources/a.md,confirmed,0.9,' \
  '항목,후보,자료,sources/a.md,needs_review,0.5,' > "$KB/facts/candidates.csv"
printf 'x\n' > "$KB/sources/a.md"

# --- populated KB (active, no --target) --------------------------------------
out="$(cd /tmp && "$PYTHON" -m factlog status 2>&1)"
printf '%s\n' "$out"
echo "---"
printf '%s' "$out" | grep -qF "active KB: $(cd "$KB" && pwd -P)" && ok "shows active KB (no --target)" || bad "active KB line wrong"
printf '%s' "$out" | grep -qE "facts: +3 candidate\(s\) \[confirmed=2, needs_review=1\]; 2 engine fact\(s\)" && ok "facts by status + engine count" || bad "facts line wrong"
printf '%s' "$out" | grep -qE "vocabulary: +[0-9]+ entit" && ok "vocabulary line present" || bad "vocabulary line missing"
printf '%s' "$out" | grep -qE "sources: +1 file\(s\), 1 with facts" && ok "source count + with-facts" || bad "sources line wrong"
printf '%s' "$out" | grep -qF "conflicts:  n/a (no single-valued" && ok "conflicts n/a when none declared" || bad "conflicts n/a line missing"
printf '%s' "$out" | grep -qF "no logic_report.txt yet" && ok "logic: no report yet" || bad "logic no-report line missing"
printf '%s' "$out" | grep -qF "0 literal(s) — none declared" && ok "literal label when no attribute relations declared" || bad "literal-none label missing"

# --- literal count + accepted/superseded breakdown ---------------------------
printf -- '- `운영`\n' > "$KB/policy/attribute-relations.md"
printf '%s\n%s\n%s\n%s\n' "$H" \
  '갑봇,통합,을서비스,sources/a.md,accepted,0.9,' \
  '갑봇,운영,2030.1,sources/a.md,confirmed,0.9,' \
  '값가,대체,값나,sources/a.md,superseded,0.9,' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qE "facts: +3 candidate\(s\) \[confirmed=1, accepted=1, superseded=1\]; 2 engine fact\(s\)" && ok "accepted/superseded in status breakdown" || bad "status breakdown wrong: $(printf '%s' "$out"|grep facts:)"
printf '%s' "$out" | grep -qE "vocabulary: +[0-9]+ entit\(y/ies\), 1 literal\(s\)" && ok "literal counted when attribute relation declared (2030.1)" || bad "literal count wrong: $(printf '%s' "$out"|grep vocab)"

# --- single-valued conflict ---------------------------------------------------
printf '# single-valued\n- 주속성\n' > "$KB/policy/single-valued.md"
printf '%s\n%s\n%s\n' "$H" \
  '을서비스,주속성,값가,sources/a.md,confirmed,0.9,' \
  '을서비스,주속성,값나,sources/a.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qE "conflicts: +1 \(over 1 single-valued" && ok "conflict counted for single-valued relation" || bad "conflict not counted: $(printf '%s' "$out" | grep conflicts)"

# --- logic report freshness (report mtime pinned; each input checked) ---------
printf 'errors: 0\nwarnings: 2\n' > "$KB/facts/logic_report.txt"
printf 'relation("x","r","y").\n' > "$KB/facts/accepted.dl"
printf 'review_required("q")?\n' > "$KB/facts/query.dl"
touch -t 205001010000 "$KB/facts/logic_report.txt"             # report pinned to 2050
touch -t 200001010000 "$KB/facts/accepted.dl" "$KB/facts/query.dl" "$KB/policy/logic-policy.dl"  # all older
out="$("$PYTHON" -m factlog status --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qE "logic: +report fresh; errors=0, warnings=2" && ok "logic report fresh + errors/warnings parsed" || bad "fresh logic line wrong: $(printf '%s' "$out"|grep logic)"
for inp in "facts/accepted.dl" "facts/query.dl" "policy/logic-policy.dl"; do
  touch -t 200001010000 "$KB/facts/accepted.dl" "$KB/facts/query.dl" "$KB/policy/logic-policy.dl"  # reset all old
  touch -t 210001010000 "$KB/$inp"                                                                  # this one newer
  out="$("$PYTHON" -m factlog status --target "$KB" 2>&1)"
  printf '%s' "$out" | grep -qF "report STALE" && ok "STALE when $inp newer than report" || bad "stale not detected for $inp"
done

# --- #330: candidates-vs-accepted engine-count drift axis --------------------
# status prints two engine-fact counts — its own (from candidates.csv) and the
# report's `engine facts:` (from accepted.dl). A truncated accepted.dl (#328/#329)
# makes them disagree; status must SAY so instead of calling the report fresh.
DKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$DKB" >/dev/null
printf 'x\n' > "$DKB/sources/a.md"
printf '%s\n%s\n%s\n' "$H" \
  'A,uses,B,sources/a.md,confirmed,0.9,' \
  'C,uses,D,sources/a.md,confirmed,0.9,' > "$DKB/facts/candidates.csv"
# A report that checked only 1 engine fact while candidates carries 2 (deduped).
printf 'engine facts: 1\nerrors: 0\nwarnings: 0\n' > "$DKB/facts/logic_report.txt"
printf 'relation("A", "uses", "B").\n' > "$DKB/facts/accepted.dl"
touch -t 205001010000 "$DKB/facts/logic_report.txt"                          # report newest
touch -t 200001010000 "$DKB/facts/accepted.dl" "$DKB/policy/logic-policy.dl" # older
touch -t 200001010000 "$DKB/facts/candidates.csv"                            # older -> isolate the count axis from freshness
out="$("$PYTHON" -m factlog status --target "$DKB" 2>&1)"
printf '%s' "$out" | grep -qF "engine-input mismatch: 2 confirmed fact(s) in candidates.csv but the report checked 1" \
  && ok "#330: status flags candidates-vs-accepted engine mismatch" || bad "#330: mismatch not flagged: $(printf '%s' "$out" | grep -A1 'logic:')"

# matching counts must NOT warn (legitimate duplicate triples dedup to the same count)
printf 'engine facts: 2\nerrors: 0\nwarnings: 0\n' > "$DKB/facts/logic_report.txt"
touch -t 205001010000 "$DKB/facts/logic_report.txt"
out="$("$PYTHON" -m factlog status --target "$DKB" 2>&1)"
printf '%s' "$out" | grep -qF "engine-input mismatch" && bad "#330: false mismatch on matching counts" || ok "#330: matching counts do not warn"

# candidates.csv newer than the report -> STALE (candidates.csv was not a freshness input before #330)
touch -t 210001010000 "$DKB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$DKB" 2>&1)"
printf '%s' "$out" | grep -qF "report STALE" && ok "#330: candidates.csv newer than report => STALE" || bad "#330: candidates.csv staleness not detected: $(printf '%s' "$out" | grep 'logic:')"

# --- #355: the on-screen engine count uses the SAME deduped basis as the report ---
# A duplicate triple (same S/R/O from two sources) makes raw len(engine_rows) exceed the
# deduped count the report prints. status used to display the RAW count, so a duplicate-
# triple KB showed "3 engine fact(s)" next to a report reading "engine facts: 2" while the
# dedup-aware mismatch stayed silent (2==2) — the two on-screen numbers disagreed with no
# explanation, the letter-of-#330-AC2 gap this closes.
UKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$UKB" >/dev/null
printf 'x\n' > "$UKB/sources/a.md"
printf 'x\n' > "$UKB/sources/b.md"
printf '%s\n%s\n%s\n%s\n' "$H" \
  'A,uses,B,sources/a.md,confirmed,0.9,' \
  'A,uses,B,sources/b.md,confirmed,0.9,' \
  'C,uses,D,sources/a.md,confirmed,0.9,' > "$UKB/facts/candidates.csv"
# The report checked the deduped 2 engine facts (accepted.dl is deduped).
printf 'engine facts: 2\nerrors: 0\nwarnings: 0\n' > "$UKB/facts/logic_report.txt"
printf 'relation("A", "uses", "B").\nrelation("C", "uses", "D").\n' > "$UKB/facts/accepted.dl"
touch -t 205001010000 "$UKB/facts/logic_report.txt"
touch -t 200001010000 "$UKB/facts/accepted.dl" "$UKB/policy/logic-policy.dl" "$UKB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$UKB" 2>&1)"
# displayed engine count == the report's deduped basis (3 raw engine rows -> 2 deduped)
printf '%s' "$out" | grep -qE "facts: +3 candidate\(s\) \[confirmed=3\]; 2 engine fact\(s\)" \
  && ok "#355: status shows the deduped engine count (matches the report's basis)" \
  || bad "#355: displayed engine count not deduped: $(printf '%s' "$out" | grep 'facts:')"
# and with the two counts agreeing, no false mismatch warning on legitimate duplicates
printf '%s' "$out" | grep -qF "engine-input mismatch" \
  && bad "#355: false mismatch on a legitimate duplicate-triple KB" || ok "#355: no false mismatch when displayed count matches the report"
# truncation (#328/#329) still surfaces: a report that checked FEWER than the deduped count warns
printf 'engine facts: 1\nerrors: 0\nwarnings: 0\n' > "$UKB/facts/logic_report.txt"
printf 'relation("A", "uses", "B").\n' > "$UKB/facts/accepted.dl"
touch -t 205001010000 "$UKB/facts/logic_report.txt"
touch -t 200001010000 "$UKB/facts/accepted.dl" "$UKB/policy/logic-policy.dl" "$UKB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$UKB" 2>&1)"
printf '%s' "$out" | grep -qF "engine-input mismatch: 2 confirmed fact(s) in candidates.csv but the report checked 1" \
  && ok "#355: truncated report still warns in a duplicate-triple KB (detection preserved)" \
  || bad "#355: truncation warning lost: $(printf '%s' "$out" | grep -A1 'logic:')"

# --- binary original counted as covered via its conversion (like coverage) -----
PKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$PKB" >/dev/null
printf '\x00\x01bin\x00' > "$PKB/sources/report.pdf"           # binary original (0 direct facts)
printf 'converted text\n' > "$PKB/runs/sources/report.md"      # its conversion carries the fact
printf '%s\n%s\n' "$H" \
  'A,rel,B,runs/sources/report.md,confirmed,0.9,' > "$PKB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$PKB" 2>&1)"
printf '%s' "$out" | grep -qE "sources: +2 file\(s\), 2 with facts \(1 via conversion\), 0 with none" \
  && ok "binary original counted covered via its conversion" || bad "status pairing wrong: $(printf '%s' "$out" | grep sources:)"

# an UNCONVERTED binary (no conversion) stays 'with none'
printf '\x00\x01bin\x00' > "$PKB/sources/lonely.pdf"
out="$("$PYTHON" -m factlog status --target "$PKB" 2>&1)"
printf '%s' "$out" | grep -qE "sources: +3 file\(s\), 2 with facts \(1 via conversion\), 1 with none" \
  && ok "unconverted binary still counted 'with none'" || bad "unconverted binary miscounted: $(printf '%s' "$out" | grep sources:)"

# a stray BINARY under runs/sources/ (cited) must NOT mask the original's gap
AKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$AKB" >/dev/null
printf '\x00\x01bin\x00' > "$AKB/sources/report.pdf"
printf '\x00\x01bin\x00' > "$AKB/runs/sources/report.bin"   # binary, not a usable conversion
printf '%s\n%s\n' "$H" 'A,rel,B,runs/sources/report.bin,confirmed,0.9,' > "$AKB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$AKB" 2>&1)"
printf '%s' "$out" | grep -qE "sources: +2 file\(s\), 1 with facts, 1 with none" \
  && ok "stray binary in runs/sources does not mask the original's gap (text-only pairing)" || bad "anomaly masked gap: $(printf '%s' "$out" | grep sources:)"

# hidden files are skipped; sync-ignored sources are tallied separately
HKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$HKB" >/dev/null
printf 'x\n' > "$HKB/sources/keep.md"
printf 'x\n' > "$HKB/sources/wip.md"
printf 'x\n' > "$HKB/sources/.DS_Store_note.md"   # hidden-ish name (dot-prefixed)
printf -- '- wip.md\n' >> "$HKB/policy/sync-ignore.md"
printf '%s\n%s\n' "$H" 'A,rel,B,sources/keep.md,confirmed,0.9,' > "$HKB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$HKB" 2>&1)"
printf '%s' "$out" | grep -qE "sources: +1 file\(s\), 1 with facts, 0 with none, 1 sync-ignored" \
  && ok "hidden skipped + sync-ignored tallied separately (not a gap)" || bad "hidden/ignored accounting wrong: $(printf '%s' "$out" | grep sources:)"

# --- not a KB -----------------------------------------------------------------
set +e; "$PYTHON" -m factlog status --target "$(mktemp -d)" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -ne 0 ] && ok "status on a non-KB path errors" || bad "non-KB path should error"

echo ""
echo "========================================"
echo "test_status_cmd: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
