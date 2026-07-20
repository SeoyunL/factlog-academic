#!/usr/bin/env bash
# tests/test_entity_audit.sh — entity fragmentation / literal-leak audit (#51)
#
# Pins:
#   - no candidates.csv -> "no candidate facts", exit 0 (informational)
#   - entity list reports each entity with its fact count
#   - a substring-contained entity pair is surfaced as a fragmentation candidate
#   - an object that looks literal under an UNDECLARED relation is a literal
#     suspect, with a suggestion to declare it
#   - once that relation IS declared in attribute-relations.md, its object moves
#     to "declared literals", drops out of entities, and is no longer a suspect
#   - always exits 0
#
# Deterministic; no pyrewire.  Usage: bash tests/test_entity_audit.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
AUDIT="$PLUGIN_ROOT/tools/entity_audit.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
run() { set +e; out="$("$PYTHON" "$AUDIT" --wiki "$KB" 2>&1)"; rc=$?; set -e; }

# --- empty KB -----------------------------------------------------------------
rm -f "$KB/facts/candidates.csv"
run
[ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qF "no candidate facts" && ok "no candidates -> informational, exit 0" || bad "empty KB not handled (rc=$rc)"

# --- populated KB -------------------------------------------------------------
H="subject,relation,object,source,status,confidence,note"
printf '%s\n%s\n%s\n%s\n' "$H" \
  '갑봇,통합_대상,을서비스,sources/a.md,accepted,0.9,' \
  '갑봇,통합_대상,병서비스,sources/a.md,accepted,0.9,' \
  '을서비스,정식_운영,2030.1,sources/a.md,accepted,0.9,' > "$KB/facts/candidates.csv"
printf 'x\n' > "$KB/sources/a.md"
# add a substring-contained pair for fragmentation (플랫폼가 ⊂ 병지역 플랫폼가)
printf '%s\n' '병지역 플랫폼가,예시,플랫폼가,sources/a.md,accepted,0.9,' >> "$KB/facts/candidates.csv"

run
[ "$rc" -eq 0 ] && ok "populated audit exits 0" || bad "exit $rc"
printf '%s' "$out" | grep -qE "\[ *2\] 갑봇" && ok "entity list reports fact count" || bad "entity fact count missing"
printf '%s' "$out" | grep -qF "'병지역 플랫폼가' ⟷ '플랫폼가'" && ok "substring fragmentation candidate surfaced" || bad "fragmentation candidate missing"
printf '%s' "$out" | grep -qF "relation '정식_운영' has literal-looking object(s): 2030.1" && ok "literal suspect under undeclared relation surfaced" || bad "literal suspect missing"

# the motivating non-date literal forms (ordinal '1호 항목', year '2026년', amount '100억')
printf '%s\n%s\n%s\n' \
  '갑봇,공약_순위,1호 항목,sources/a.md,accepted,0.9,' \
  '을서비스,운영_연도,2026년,sources/a.md,accepted,0.9,' \
  '예산,규모,100억,sources/a.md,accepted,0.9,' >> "$KB/facts/candidates.csv"
run
for v in "1호 항목" "2026년" "100억"; do
  printf '%s' "$out" | grep -qE "literal-looking object\(s\):.*$v" && ok "literal form '$v' detected as suspect" || bad "literal form '$v' missed"
done
printf '%s' "$out" | grep -qF "consider adding '정식_운영' to policy/attribute-relations.md" && ok "suggests declaring the relation" || bad "declare suggestion missing"
# undeclared literal still counts as an entity
printf '%s' "$out" | grep -qF "2030.1  (accepted)" && ok "undeclared literal still listed as entity" || bad "literal not listed as entity"

# --- declare the relation: literal moves out of entities ----------------------
printf -- '- `정식_운영`\n' > "$KB/policy/attribute-relations.md"
run
printf '%s' "$out" | grep -qF "declared literals (attribute-relation objects, not entities):" && ok "declared-literals section appears" || bad "declared-literals section missing"
# 2030.1 should now be under declared literals, not flagged as a suspect
printf '%s' "$out" | grep -qF "relation '정식_운영' has literal-looking" && bad "declared relation still flagged as suspect" || ok "declared relation no longer a literal suspect"

# --- compound-term literals (#386) --------------------------------------------
# The issue's reproduction, end to end: two `date(YYYY)` rows used to pair with
# each other on the wrapper name ("shared token ['date']"), and every further date
# added C(n,2) more of them until the real candidates were buried.
KB2="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB2" >/dev/null 2>&1
printf 'x\n' > "$KB2/sources/a.md"
printf '%s\n%s\n%s\n%s\n%s\n%s\n%s\n' "$H" \
  'P1998,published_year,date(1998),sources/a.md,accepted,0.9,' \
  'P2020,published_year,date(2020),sources/a.md,accepted,0.9,' \
  '병지역 플랫폼가,예시,플랫폼가,sources/a.md,accepted,0.9,' \
  'Date(Time),측정,플랫폼가,sources/a.md,accepted,0.9,' \
  'date(2019),집필,Kim,sources/a.md,accepted,0.9,' \
  'X1,측정_시점,date(abc),sources/a.md,accepted,0.9,' > "$KB2/facts/candidates.csv"
set +e; out="$("$PYTHON" "$AUDIT" --wiki "$KB2" 2>&1)"; rc=$?; set -e

[ "$rc" -eq 0 ] && ok "compound-term KB exits 0" || bad "compound-term KB exit $rc"
printf '%s' "$out" | grep -qF "shared token ['date']" && bad "date(YYYY) pair still a fragmentation candidate" || ok "no wrapper-name shared-token pair"
printf '%s' "$out" | grep -qF "'병지역 플랫폼가' ⟷ '플랫폼가'" && ok "real candidate survives beside compound terms" || bad "real candidate lost"
printf '%s' "$out" | grep -qF "consider adding 'published_year' to policy/attribute-relations.md" && ok "compound term under undeclared relation advises declaring" || bad "declare advice missing for compound term"
# Capitalized look-alike: a column label, not the mandated notation — stays an entity.
printf '%s' "$out" | grep -qF "Date(Time)  (accepted)" && ok "'Date(Time)' still listed as an entity" || bad "'Date(Time)' wrongly treated as a literal"
# A compound term dropped from the entity list must resurface in its own section,
# or removing it from `entities` just moves the blind spot.
printf '%s' "$out" | grep -qF "literal used as subject" && ok "subject-position literal section printed" || bad "subject-position literal section missing"
printf '%s' "$out" | grep -qF "'date(2019)' (1 fact(s))" && ok "subject-position literal named with its fact count" || bad "subject-position literal missing"
# `date(abc)` is malformed under ANY parser version, unlike year-only date(YYYY)
# (#385) — so this assertion does not flip when that lands.
printf '%s' "$out" | grep -qF "malformed typed literal" && ok "malformed-literal section printed" || bad "malformed-literal section missing"
printf '%s' "$out" | grep -qF "• 'date(abc)'" && ok "unparsable compound term named" || bad "unparsable compound term missing"
# Both new findings must be countable from the one-line summary, not only from
# the stderr sections a scanning human or wrapper script never reads.
printf '%s' "$out" | grep -qE "1 literal subject\(s\)" && ok "summary counts literal subjects" || bad "summary omits literal subject count"
printf '%s' "$out" | grep -qE "[0-9]+ malformed literal\(s\)" && ok "summary counts malformed literals" || bad "summary omits malformed literal count"

echo ""
echo "========================================"
echo "test_entity_audit: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
