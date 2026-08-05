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

# --- #331: a conflicting value with non-ASCII digits is named -----------------
# This path counts distinct RAW object strings, so it already saw the pair as two
# values; what it never did was show WHICH one the engine cannot read. repr()
# would not help — '１００억' and '100억' are indistinguishable in most fonts.
printf '# single-valued\n- 매출\n' > "$KB/policy/single-valued.md"
# The relation must be declared TYPED: that declaration is what makes the digits
# (rather than a missing spec) the reason the value degrades to a raw key.
printf -- '- `매출` : amount as revenue_amt\n' > "$KB/policy/typed-relations.md"
printf '%s\n%s\n%s\n' "$H" \
  '갑사,매출,100억,sources/a.md,confirmed,0.9,' \
  '갑사,매출,１００억,sources/a.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "non-ASCII digits" && ok "#331: status flags the non-ASCII digit value" || bad "#331: status does not flag it: $(printf '%s' "$out"|grep conflicts)"
# The ESCAPED codepoint; the raw glyph cannot satisfy this.
printf '%s' "$out" | grep -qF 'uff11' && ok "#331: status escapes the offending codepoints" || bad "#331: status does not escape the offending characters"
# The same claim check_conflicts' note makes: re-collection does not REPLACE
# supersession — for genuinely different values (100억 vs ２００억) correcting the
# source leaves 100억 vs 200억, still a conflict supersession must settle. Both
# surfacing points have to say so or one of them is telling half the truth.
printf '%s' "$out" | grep -qF "if the values still differ" && ok "#331: status names supersession as the follow-up" || bad "#331: status drops the supersede-if-still-different clause"

# One offender shared by TWO conflict groups must be named once, not once per
# group. The values are collected into a set before rendering.
printf '%s\n%s\n%s\n%s\n%s\n' "$H" \
  '갑사,매출,100억,sources/a.md,confirmed,0.9,' \
  '갑사,매출,１００억,sources/a.md,confirmed,0.9,' \
  '을사,매출,200억,sources/a.md,confirmed,0.9,' \
  '을사,매출,１００억,sources/a.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$KB" 2>&1)"
dupes="$(printf '%s' "$out" | grep -o 'uff11' | wc -l | tr -d ' ')"
[ "$dupes" = "1" ] && ok "#331: a shared offender is named once across conflict groups" || bad "#331: offender repeated $dupes times across groups"

# Negative control 1 (UNTYPED relation): the same full-width value under a
# relation with no typed declaration must NOT be flagged. There the raw key comes
# from the missing spec, not the digits, and supersession is the correct fix.
printf '# single-valued\n- 모델\n' > "$KB/policy/single-valued.md"
rm -f "$KB/policy/typed-relations.md"
printf '%s\n%s\n%s\n' "$H" \
  '갑사,모델,GPT-４,sources/a.md,confirmed,0.9,old' \
  '갑사,모델,GPT-5,sources/a.md,confirmed,0.9,current' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qE "conflicts: +1 \(over 1 single-valued" && ok "#331: untyped conflict still counted" || bad "#331: untyped conflict not counted"
if printf '%s' "$out" | grep -qF "non-ASCII digits"; then
  bad "#331: status flags an UNTYPED relation as non-ASCII (guidance is false there)"
else
  ok "#331: status does not flag an untyped relation"
fi

# Negative control 1b: non-ASCII digits in the declared UNIT NAME. The value
# parses to a scalar, so calling it out as unreadable would be false — the flag
# has to ask the normalizer, not just the digit predicate.
printf '# single-valued\n- 매출\n' > "$KB/policy/single-valued.md"
printf -- '- `매출` : amount as revenue_amt (억１=100000000)\n' > "$KB/policy/typed-relations.md"
printf '%s\n%s\n%s\n' "$H" \
  '갑사,매출,"amount(100,""억１"")",sources/a.md,confirmed,0.9,' \
  '갑사,매출,"amount(200,""억１"")",sources/a.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qE "conflicts: +1 \(over 1 single-valued" && ok "#331: unit-name conflict still counted" || bad "#331: unit-name conflict not counted: $(printf '%s' "$out"|grep conflicts)"
if printf '%s' "$out" | grep -qF "non-ASCII digits"; then
  bad "#331: status flags a value that PARSES (the unit name carries the digits)"
else
  ok "#331: status does not flag a value whose non-ASCII digits are in the unit name"
fi
rm -f "$KB/policy/typed-relations.md"

# Negative control 2: restore the ASCII-only conflict, which must NOT be flagged —
# otherwise the two assertions above would pass against an unconditional warning.
printf '# single-valued\n- 주속성\n' > "$KB/policy/single-valued.md"
printf '%s\n%s\n%s\n' "$H" \
  '을서비스,주속성,값가,sources/a.md,confirmed,0.9,' \
  '을서비스,주속성,값나,sources/a.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog status --target "$KB" 2>&1)"
if printf '%s' "$out" | grep -qF "non-ASCII digits"; then
  bad "#331: ASCII-only conflict wrongly flagged as non-ASCII"
else
  ok "#331: ASCII-only conflict carries no non-ASCII note"
fi

# A clean ASCII-only KB must produce NO extra output. Resolving typed relations
# is not free: KbContext.typed_relations warns when a typed relation is missing
# from attribute-relations.md (and re-reads facts + logic policy to compute
# reserved names). Resolving it unconditionally made `factlog status` print a
# warning on a KB with zero conflicts and nothing wrong.
#
# COMPARE rather than grep: the assertions above ask whether a specific string is
# present, which cannot see an unrelated line appearing. Here the whole of stderr
# is compared against empty, and stdout against "no typed-relations line", so any
# new output at all fails.
CLEAN="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$CLEAN" >/dev/null
printf '# single-valued\n- 매출\n' > "$CLEAN/policy/single-valued.md"
# Typed but deliberately NOT declared in attribute-relations.md: the shape that
# makes typed_relations() warn.
printf -- '- `매출` : amount as revenue_amt\n' > "$CLEAN/policy/typed-relations.md"
printf 'x\n' > "$CLEAN/sources/a.md"
printf '%s\n%s\n' "$H" '갑사,매출,100억,sources/a.md,accepted,0.9,' > "$CLEAN/facts/candidates.csv"
clean_err="$("$PYTHON" -m factlog status --target "$CLEAN" 2>&1 >/dev/null)"
clean_out="$("$PYTHON" -m factlog status --target "$CLEAN" 2>/dev/null)"
if [ -z "$clean_err" ]; then
  ok "#331: clean ASCII KB — status writes nothing to stderr"
else
  bad "#331: clean ASCII KB — status wrote to stderr: $clean_err"
fi
typed_lines="$(printf '%s\n' "$clean_out" | grep -c '^typed-relations:' || true)"
[ "$typed_lines" = "0" ] && ok "#331: clean ASCII KB — no typed-relations warning on stdout" || bad "#331: clean ASCII KB — $typed_lines typed-relations line(s) on stdout"
printf '%s' "$clean_out" | grep -qE "conflicts: +0 \(over 1 single-valued" && ok "#331: clean ASCII KB — 0 conflicts reported" || bad "#331: clean ASCII KB — conflicts line wrong: $(printf '%s' "$clean_out"|grep conflicts)"

# A BROKEN typed-relations policy must not abort the report. `status` is the
# command you run to find out what is wrong with a KB, so it has to be total:
# typed_relations() raises FactlogError on a non-ASCII alias (among others), and
# the flagging block above is the first status code path ever to call it. Left
# unguarded that exception costs the `logic:` line and turns rc 0 into 1 —
# a regression against everything documented in docs/reference/active-kb.md.
BROKEN="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$BROKEN" >/dev/null
printf 'x\n' > "$BROKEN/sources/a.md"
printf '# single-valued\n- 매출\n' > "$BROKEN/policy/single-valued.md"
printf -- '- `매출` : amount as 매출액\n' > "$BROKEN/policy/typed-relations.md"   # alias is not ASCII
# The full-width value is what makes `flagged` non-empty and so reaches the call.
printf '%s\n%s\n%s\n' "$H" \
  '갑사,매출,100억,sources/a.md,confirmed,0.9,' \
  '갑사,매출,１００억,sources/a.md,confirmed,0.9,' > "$BROKEN/facts/candidates.csv"
set +e; broken_out="$("$PYTHON" -m factlog status --target "$BROKEN" 2>/dev/null)"; broken_rc=$?; set -e
[ "$broken_rc" = "0" ] && ok "#331: broken typed policy + full-width conflict — status still exits 0" || bad "#331: broken typed policy aborts status (rc=$broken_rc)"
printf '%s' "$broken_out" | grep -qE "logic: +" && ok "#331: broken typed policy — report still reaches the logic line" || bad "#331: broken typed policy truncates the report before logic:"
printf '%s' "$broken_out" | grep -qE "conflicts: +1 \(over 1 single-valued" && ok "#331: broken typed policy — conflicts still counted" || bad "#331: broken typed policy — conflicts line wrong: $(printf '%s' "$broken_out"|grep conflicts)"

# Same claim, a failure that is NOT a FactlogError. typed_relations() reads
# logic-policy.dl to compute reserved names, so a policy file that is not UTF-8
# (cp949 is realistic here — the CLI already forces UTF-8 on cp949 consoles)
# raises UnicodeDecodeError. Nothing caught it: it is not a FactlogError, so
# main()'s friendly handler re-raised and the user got a raw traceback.
#
# The widened catch covers typed-relations.md itself too — this block is the only
# place cmd_status reads that file, so on main a cp949 copy was simply never
# decoded (rc=0, full report) and HEAD now matches. What it cannot cover is a
# cp949 single-valued.md or attribute-relations.md: those abort status on main
# too (measured, rc=1 on both trees) and are read at cli.py:1692-1693, long
# before this block, so they are out of reach here by construction.
"$PYTHON" -c "import pathlib,sys; pathlib.Path(sys.argv[1]).write_bytes('// 정책\n'.encode('cp949'))" "$BROKEN/policy/logic-policy.dl"
set +e; cp949_out="$("$PYTHON" -m factlog status --target "$BROKEN" 2>/dev/null)"; cp949_rc=$?; set -e
[ "$cp949_rc" = "0" ] && ok "#331: non-UTF-8 logic-policy.dl — status still exits 0" || bad "#331: non-UTF-8 logic-policy.dl aborts status (rc=$cp949_rc)"
printf '%s' "$cp949_out" | grep -qE "logic: +" && ok "#331: non-UTF-8 logic-policy.dl — report still reaches the logic line" || bad "#331: non-UTF-8 logic-policy.dl truncates the report before logic:"

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
