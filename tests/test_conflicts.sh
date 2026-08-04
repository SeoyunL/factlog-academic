#!/usr/bin/env bash
# tests/test_conflicts.sh — single-valued contradiction detection + supersession (#30)
#
# Pins:
#   - a single-valued relation with 2 distinct objects for one subject = conflict
#   - marking the outdated row status='superseded' resolves it (non-destructive)
#   - a multi-valued (undeclared) relation with 2 objects is NOT a conflict
#   - no policy/single-valued.md => nothing to check (exit 0)
#   - 'superseded' rows survive merge and are excluded from accepted.dl
#   - validate.py accepts the 'superseded' status
#
# Deterministic; no pyrewire required.  Usage: bash tests/test_conflicts.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
HEADER="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
CONFLICTS="$PLUGIN_ROOT/tools/check_conflicts.py"
csv() { printf '%s\n' "$HEADER" "$@" > "$KB/facts/candidates.csv"; }
run_conflicts() { "$PYTHON" "$CONFLICTS" --wiki "$KB" >/dev/null 2>&1; }

# no single-valued.md yet -> nothing to check
if run_conflicts; then ok "no single-valued.md => exit 0 (nothing to check)"; else bad "should be 0 without single-valued.md"; fi

# declare 주_속성 single-valued
printf '# single-valued relations\n\n- 주_속성\n' > "$KB/policy/single-valued.md"

# multi-valued (undeclared) relation with 2 objects -> NOT a conflict
csv '갑봇,구성_요소,ToolA,sources/x.md,confirmed,0.9,' '갑봇,구성_요소,을서비스,sources/x.md,confirmed,0.9,'
printf '# x\n' > "$KB/sources/x.md"
if run_conflicts; then ok "multi-valued relation with 2 objects is not flagged"; else bad "multi-valued wrongly flagged"; fi

# single-valued relation with 2 distinct objects -> CONFLICT
csv '을서비스,주_속성,값가,sources/x.md,confirmed,0.9,' '을서비스,주_속성,값나,sources/x.md,confirmed,0.9,'
if run_conflicts; then bad "single-valued conflict NOT detected"; else ok "single-valued conflict detected (exit non-zero)"; fi
cout="$("$PYTHON" "$CONFLICTS" --wiki "$KB" 2>&1 || true)"
if printf '%s' "$cout" | grep -qF "CONFLICT"; then ok "conflict reported with CONFLICT line"; else bad "no CONFLICT line"; fi

# resolve by superseding the outdated row -> conflict clears (non-destructively)
csv '을서비스,주_속성,값가,sources/x.md,superseded,0.9,old' '을서비스,주_속성,값나,sources/x.md,confirmed,0.9,current'
if run_conflicts; then ok "superseding the outdated row resolves the conflict"; else bad "superseded row still conflicts"; fi

# superseded survives merge and is excluded from accepted.dl
printf '[{"subject":"을서비스","relation":"주_속성","object":"값가","source":"sources/x.md","status":"superseded","confidence":0.9,"note":"old"},{"subject":"을서비스","relation":"주_속성","object":"값나","source":"sources/x.md","status":"confirmed","confidence":0.9,"note":"current"}]' > "$KB/runs/r.json"
"$PYTHON" "$PLUGIN_ROOT/tools/merge_candidates.py" --wiki "$KB" >/dev/null 2>&1 || true
if grep -q ',superseded,' "$KB/facts/candidates.csv"; then ok "merge preserves superseded status"; else bad "merge dropped/renamed superseded status"; fi
FACTLOG_ROOT="$KB" "$PYTHON" "$PLUGIN_ROOT/tools/compile_facts.py" >/dev/null 2>&1
if grep -q '"값가"' "$KB/facts/accepted.dl"; then bad "superseded fact leaked into accepted.dl"; else ok "superseded fact excluded from accepted.dl"; fi
if grep -q '"값나"' "$KB/facts/accepted.dl"; then ok "current fact compiled to accepted.dl"; else bad "current fact missing from accepted.dl"; fi

# H1: a human-marked superseded row is PRESERVED across a re-merge even when the
# originating run JSON re-asserts it as confirmed (resolution is durable).
csv '을서비스,주_속성,값가,sources/x.md,superseded,0.9,old' '을서비스,주_속성,값나,sources/x.md,confirmed,0.9,current'
printf '[{"subject":"을서비스","relation":"주_속성","object":"값가","source":"sources/x.md","status":"confirmed","confidence":0.9,"note":"re-asserted"}]' > "$KB/runs/r.json"
"$PYTHON" "$PLUGIN_ROOT/tools/merge_candidates.py" --wiki "$KB" >/dev/null 2>&1 || true
if grep -q '을서비스,주_속성,값가,.*,superseded,' "$KB/facts/candidates.csv"; then ok "re-merge preserves human-marked superseded (durable resolution)"; else bad "re-merge reverted the superseded row"; fi
if run_conflicts; then ok "conflict stays resolved after re-merge"; else bad "conflict reappeared after re-merge"; fi
rm -f "$KB/runs/r.json"

# M1: single-valued name parses from a backtick token with a trailing description
printf '# single-valued\n\n- `주_속성` (the base LLM)\n' > "$KB/policy/single-valued.md"
got="$("$PYTHON" -c "import sys; sys.path.insert(0,'$PLUGIN_ROOT/tools'); import os; os.environ['FACTLOG_ROOT']='$KB'; import common; print('주_속성' in common.single_valued_relations())")"
[ "$got" = "True" ] && ok "single-valued name parsed from backtick+description line" || bad "backtick+description line mis-parsed (false negative)"
printf '# single-valued\n\n- 주_속성\n' > "$KB/policy/single-valued.md"

# validate.py accepts the superseded status (no invalid-status error)
if "$PYTHON" "$PLUGIN_ROOT/tools/validate.py" "$KB" 2>&1 | grep -q "invalid status"; then bad "validate rejected superseded status"; else ok "validate accepts superseded status"; fi

# --- #204: typed single-valued amount compares on the canonical scalar, not the
# raw string, so equivalent notations (억 ↔ 조) don't false-positive while real
# value differences still fire and the report preserves the original notation.
printf '# single-valued\n\n- 주_속성\n- 매출\n' > "$KB/policy/single-valued.md"
printf -- '- `매출` : amount as revenue\n' > "$KB/policy/typed-relations.md"

# equivalent notations of the same value -> NOT a conflict (1 value)
csv '갑사,매출,"amount(5400,""억"")",sources/x.md,confirmed,0.9,' '갑사,매출,"amount(0.54,""조"")",sources/x.md,confirmed,0.9,'
if run_conflicts; then ok "typed amount: equivalent notations (5400억 == 0.54조) not flagged"; else bad "typed amount: equivalent notations wrongly flagged as conflict"; fi

# real value difference -> CONFLICT, and the message keeps the ORIGINAL notation
csv '갑사,매출,"amount(5000,""억"")",sources/x.md,confirmed,0.9,' '갑사,매출,"amount(5400,""억"")",sources/x.md,confirmed,0.9,'
if run_conflicts; then bad "typed amount: real value difference (5000억 vs 5400억) NOT detected"; else ok "typed amount: real value difference detected"; fi
cout="$("$PYTHON" "$CONFLICTS" --wiki "$KB" 2>&1 || true)"
if printf '%s' "$cout" | grep -qF 'amount(5000,"억")' && printf '%s' "$cout" | grep -qF 'amount(5400,"억")'; then ok "typed amount: report preserves original notation (provenance)"; else bad "typed amount: report lost original notation"; fi

# unparseable typed object degrades to the raw-string key (still detected)
csv '갑사,매출,"amount(5400,""억"")",sources/x.md,confirmed,0.9,' '갑사,매출,not-a-number,sources/x.md,confirmed,0.9,'
if run_conflicts; then bad "typed amount: parseable-vs-unparseable difference NOT detected"; else ok "typed amount: unparseable object degrades to raw key (detected)"; fi

# --- #210: the relation name written in NFD (macOS decomposed jamo) must still
# reach its NFC-keyed typed spec, so equivalent notations collapse (억 ↔ 조) and
# only real value differences fire — the same asymmetry #57/#64 fixed for names.
NFD_REL="$("$PYTHON" -c 'import unicodedata; print(unicodedata.normalize("NFD","매출"))')"
printf '# single-valued\n\n- 주_속성\n- %s\n' "$NFD_REL" > "$KB/policy/single-valued.md"
printf -- '- `%s` : amount as revenue\n' "$NFD_REL" > "$KB/policy/typed-relations.md"

# NFD relation, equivalent notations -> NOT a conflict (pre-fix: false positive)
csv "갑사,$NFD_REL,\"amount(5400,\"\"억\"\")\",sources/x.md,confirmed,0.9," "갑사,$NFD_REL,\"amount(0.54,\"\"조\"\")\",sources/x.md,confirmed,0.9,"
if run_conflicts; then ok "NFD relation: equivalent notations (5400억 == 0.54조) not flagged"; else bad "NFD relation: equivalent notations wrongly flagged (typed lookup missed NFD name)"; fi

# NFD relation, real value difference -> still a CONFLICT
csv "갑사,$NFD_REL,\"amount(5000,\"\"억\"\")\",sources/x.md,confirmed,0.9," "갑사,$NFD_REL,\"amount(5400,\"\"억\"\")\",sources/x.md,confirmed,0.9,"
if run_conflicts; then bad "NFD relation: real value difference (5000억 vs 5400억) NOT detected"; else ok "NFD relation: real value difference still detected"; fi

# restore the baseline single-valued policy for any later additions
printf '# single-valued\n\n- 주_속성\n' > "$KB/policy/single-valued.md"
rm -f "$KB/policy/typed-relations.md"

# --- #227: canonicalize single-valued conflict detection over alias variants ---
# Set up: published_year is single-valued; 게재연도 and 발행년도 are aliases.
printf '# single-valued\n\n- published_year\n' > "$KB/policy/single-valued.md"
printf '# Relation aliases\n\n- `게재연도` -> `published_year`\n- `발행년도` -> `published_year`\n' > "$KB/policy/relation-aliases.md"
printf '# x\n' > "$KB/sources/x.md"

# cross-variant conflict: 게재연도/2005 + 발행년도/2007 -> ONE conflict on published_year
csv '논문A,게재연도,2005,sources/x.md,confirmed,0.9,' '논문A,발행년도,2007,sources/x.md,confirmed,0.9,'
if run_conflicts; then bad "#227: cross-variant conflict (2005 vs 2007) NOT detected"; else ok "#227: cross-variant conflict detected (exit non-zero)"; fi
cout="$("$PYTHON" "$CONFLICTS" --wiki "$KB" 2>&1 || true)"
if printf '%s' "$cout" | grep -qF "published_year"; then ok "#227: conflict reported under canonical name published_year"; else bad "#227: canonical name missing from conflict report"; fi
if printf '%s' "$cout" | grep -qF "2005" && printf '%s' "$cout" | grep -qF "2007"; then ok "#227: both verbatim objects shown in conflict report"; else bad "#227: verbatim object values missing from conflict report"; fi

# typed-equal across variants: amount(5400,"억") == amount(0.54,"조") -> NOT a conflict
printf '# single-valued\n\n- published_year\n' > "$KB/policy/single-valued.md"
printf -- '- `published_year` : amount as revenue\n' > "$KB/policy/typed-relations.md"
csv '갑사,게재연도,"amount(5400,""억"")",sources/x.md,confirmed,0.9,' '갑사,발행년도,"amount(0.54,""조"")",sources/x.md,confirmed,0.9,'
if run_conflicts; then ok "#227: typed-equal across variants (5400억 == 0.54조) not flagged"; else bad "#227: typed-equal across variants wrongly flagged"; fi

# typed-different across variants: 5000억 vs 5400억 -> conflict, 2 verbatim reps
csv '갑사,게재연도,"amount(5000,""억"")",sources/x.md,confirmed,0.9,' '갑사,발행년도,"amount(5400,""억"")",sources/x.md,confirmed,0.9,'
if run_conflicts; then bad "#227: typed-different across variants (5000억 vs 5400억) NOT detected"; else ok "#227: typed-different across variants detected"; fi
cout="$("$PYTHON" "$CONFLICTS" --wiki "$KB" 2>&1 || true)"
if printf '%s' "$cout" | grep -qF 'amount(5000,"억")' && printf '%s' "$cout" | grep -qF 'amount(5400,"억")'; then ok "#227: typed-different across variants: both verbatim reps preserved"; else bad "#227: typed-different across variants: verbatim reps missing"; fi

# no-alias no-op: without relation-aliases.md two raw variants are independent -> no conflict
rm -f "$KB/policy/typed-relations.md"
rm -f "$KB/policy/relation-aliases.md"
printf '# single-valued\n\n- published_year\n' > "$KB/policy/single-valued.md"
csv '논문A,게재연도,2005,sources/x.md,confirmed,0.9,' '논문A,발행년도,2007,sources/x.md,confirmed,0.9,'
if run_conflicts; then ok "#227: no alias file -> cross-variant rows not flagged (opt-in no-op)"; else bad "#227: no alias file -> cross-variant rows wrongly flagged"; fi

# restore clean state
printf '# single-valued\n\n- 주_속성\n' > "$KB/policy/single-valued.md"
rm -f "$KB/policy/typed-relations.md"
rm -f "$KB/policy/relation-aliases.md"

# --- #218 / #224 A: ordinal is a rank-only contract. parse_ordinal drops the
# ordinal-class unit (호/위/번/...), so cross-unit notations of the same rank
# collapse onto one value — consistent with the engine (rank-only comparison).
# Pin the by-design collapse (a future unit-aware grouping would fail here).
printf '# single-valued\n\n- 순위\n' > "$KB/policy/single-valued.md"
printf -- '- `순위` : ordinal as rank\n' > "$KB/policy/typed-relations.md"
printf '# x\n' > "$KB/sources/x.md"
# 제3호 and 3위 both normalize to rank 3 -> ONE value, not a conflict.
csv '갑,순위,제3호,sources/x.md,confirmed,0.9,' '갑,순위,3위,sources/x.md,confirmed,0.9,'
if run_conflicts; then ok "ordinal rank-only: 제3호 == 3위 (rank 3) not flagged"; else bad "ordinal rank-only: cross-unit same-rank wrongly flagged"; fi
# a genuine rank difference (3 vs 5) must still fire.
csv '갑,순위,제3호,sources/x.md,confirmed,0.9,' '갑,순위,5위,sources/x.md,confirmed,0.9,'
if run_conflicts; then bad "ordinal rank-only: distinct ranks (3 vs 5) NOT detected"; else ok "ordinal rank-only: distinct ranks (3 vs 5) detected"; fi

# --- #224 B1: a CUSTOM unit table declared in typed-relations.md must flow
# end-to-end (typed_relations() -> spec.units -> _group_key) with a REAL KB, so
# equivalent amounts under the custom table collapse (prior tests: hand-built).
printf '# single-valued\n\n- 보상\n' > "$KB/policy/single-valued.md"
printf -- '- `보상` : amount as reward (달러=1300, 센트=13)\n' > "$KB/policy/typed-relations.md"
# amount(2,"달러")=2600 == amount(200,"센트")=2600 -> NOT a conflict.
csv '갑,보상,"amount(2,""달러"")",sources/x.md,confirmed,0.9,' '갑,보상,"amount(200,""센트"")",sources/x.md,confirmed,0.9,'
if run_conflicts; then ok "custom units: amount(2,달러) == amount(200,센트) not flagged"; else bad "custom units: equivalent amounts wrongly flagged (custom table not applied)"; fi
# 2달러 (2600) vs 3달러 (3900) -> a real difference under the custom table.
csv '갑,보상,"amount(2,""달러"")",sources/x.md,confirmed,0.9,' '갑,보상,"amount(3,""달러"")",sources/x.md,confirmed,0.9,'
if run_conflicts; then bad "custom units: distinct amounts (2 vs 3 달러) NOT detected"; else ok "custom units: distinct amounts (2 vs 3 달러) detected"; fi

# --- #224 B2: a bare year `2030` has no month -> parse_date None -> degrades to
# the raw key, which is distinct from a scalar date (2030.1 -> 20300101). So the
# pair fires a real CONFLICT: a bare year is NOT silently a parseable date.
printf '# single-valued\n\n- 출시\n' > "$KB/policy/single-valued.md"
printf -- '- `출시` : date as launch_date\n' > "$KB/policy/typed-relations.md"
csv '기서비스,출시,2030,sources/x.md,confirmed,0.9,' '기서비스,출시,2030.1,sources/x.md,confirmed,0.9,'
if run_conflicts; then bad "year-only date: bare 2030 vs 2030.1 NOT detected (bare year mis-parsed as date?)"; else ok "year-only date: bare 2030 degrades to raw and conflicts with scalar 2030.1"; fi

# --- #331: a conflict value carrying non-ASCII digits gets an extra note.
# The generic advice this command prints ("mark the outdated row superseded")
# is wrong for this class. Measured: following it on the ASCII row takes
# check_conflicts from rc=1 to `0 conflicts` / rc=0 while the unparseable
# full-width row survives as the only engine input — gate green, KB worse.
printf '# single-valued\n\n- 매출\n' > "$KB/policy/single-valued.md"
printf -- '- `매출` : amount as revenue_amt\n' > "$KB/policy/typed-relations.md"
csv '갑사,매출,100억,sources/x.md,confirmed,0.9,' '갑사,매출,１００억,sources/x.md,confirmed,0.9,'
nout="$("$PYTHON" "$CONFLICTS" --wiki "$KB" 2>&1 || true)"
printf '%s' "$nout" | grep -qF "non-ASCII digits" \
  && ok "#331: full-width conflict value gets the extra note" \
  || bad "#331: no non-ASCII note on a full-width conflict"
# Match the ESCAPED codepoint, which the raw glyph cannot satisfy. (The CONFLICT
# line above does print the raw '１００억', so asserting on the glyph would pass
# even if the note escaped nothing.)
printf '%s' "$nout" | grep -qF 'uff11' \
  && ok "#331: the note escapes the offending codepoints" \
  || bad "#331: note does not name the offending characters as escapes"
printf '%s' "$nout" | grep -qF "re-collect" \
  && ok "#331: the note gives the real fix (correct the source)" \
  || bad "#331: note does not point at the source fix"

# Negative control 1 (UNTYPED relation). Every clause of the note is false here:
# _group_key returned raw because there is no spec, not because of digit width —
# the ASCII twin 'GPT-5' keys the same way — 'GPT-４' is a usable relation/3 fact,
# and superseding the outdated row is exactly the right fix. Firing here would
# tell the user NOT to do the one thing that works.
printf '# single-valued\n\n- 모델\n' > "$KB/policy/single-valued.md"
rm -f "$KB/policy/typed-relations.md"
csv '갑사,모델,GPT-４,sources/x.md,confirmed,0.9,old' '갑사,모델,GPT-5,sources/x.md,confirmed,0.9,current'
uout="$("$PYTHON" "$CONFLICTS" --wiki "$KB" 2>&1 || true)"
printf '%s' "$uout" | grep -qF "CONFLICT" \
  && ok "#331: untyped full-width conflict is still reported" \
  || bad "#331: untyped conflict not reported at all"
if printf '%s' "$uout" | grep -qF "non-ASCII digits"; then
  bad "#331: UNTYPED relation wrongly carries the non-ASCII note (guidance is false there)"
else
  ok "#331: untyped relation carries no non-ASCII note"
fi

# Negative control 2 (ASCII-only, typed). Without these the assertions above would
# pass just as well against a note printed for EVERY conflict.
printf '# single-valued\n\n- 주_속성\n' > "$KB/policy/single-valued.md"
rm -f "$KB/policy/typed-relations.md"
csv '을서비스,주_속성,값가,sources/x.md,confirmed,0.9,' '을서비스,주_속성,값나,sources/x.md,confirmed,0.9,'
aout="$("$PYTHON" "$CONFLICTS" --wiki "$KB" 2>&1 || true)"
if printf '%s' "$aout" | grep -qF "non-ASCII digits"; then
  bad "#331: ASCII-only conflict wrongly carries the non-ASCII note"
else
  ok "#331: ASCII-only conflict carries no non-ASCII note"
fi

# restore clean state
printf '# single-valued\n\n- 주_속성\n' > "$KB/policy/single-valued.md"
rm -f "$KB/policy/typed-relations.md"
rm -f "$KB/policy/relation-aliases.md"

echo ""
echo "========================================"
echo "test_conflicts: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
