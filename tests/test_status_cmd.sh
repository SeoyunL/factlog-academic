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
"$PYTHON" -m factlog init --target "$KB" >/dev/null   # records active KB
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

# --- not a KB -----------------------------------------------------------------
set +e; "$PYTHON" -m factlog status --target "$(mktemp -d)" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -ne 0 ] && ok "status on a non-KB path errors" || bad "non-KB path should error"

echo ""
echo "========================================"
echo "test_status_cmd: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
