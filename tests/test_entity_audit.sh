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

# --- amount shape vs unit resolution (#394) -----------------------------------
# With NO typed-relations.md, the unit table is unknowable — but `amount(abc,"억")`
# and `amount(,"억")` fail on the NUMBER part, before any unit lookup, so no
# declaration could make them parse. Unit-independent, hence judged either way.
# `amount(5,"달러")` is the contrast: it may be legal once a table is declared, so
# it must stay silent. Neither assertion depends on a parser version.
KB3="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB3" >/dev/null 2>&1
printf 'x\n' > "$KB3/sources/a.md"
printf '%s\n%s\n%s\n%s\n' "$H" \
  'P1,예산,"amount(abc,""억"")",sources/a.md,accepted,0.9,' \
  'P2,예산,"amount(,""억"")",sources/a.md,accepted,0.9,' \
  'P3,예산,"amount(5,""달러"")",sources/a.md,accepted,0.9,' > "$KB3/facts/candidates.csv"
set +e; out="$("$PYTHON" "$AUDIT" --wiki "$KB3" 2>&1)"; rc=$?; set -e

[ "$rc" -eq 0 ] && ok "undeclared-amount KB exits 0" || bad "undeclared-amount KB exit $rc"
printf '%s' "$out" | grep -qF '• '"'"'amount(abc,"억")'"'" && ok "amount with an unparsable number is malformed without a declaration" || bad "amount(abc,...) missed with no declaration"
printf '%s' "$out" | grep -qF '• '"'"'amount(,"억")'"'" && ok "amount with an empty number is malformed without a declaration" || bad "amount(,...) missed with no declaration"
printf '%s' "$out" | grep -qF '• '"'"'amount(5,"달러")'"'" && bad "unresolved unit accused without a unit table" || ok "unresolved unit stays unjudged without a declaration"

# --- conflicting typed declarations (#393) ------------------------------------
# Canonical and alias declared on separate lines with DIFFERENT unit tables. The
# later line used to overwrite the canonical's, so a value written under the
# canonical was judged against the alias's table and falsely accused.
KB4="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB4" >/dev/null 2>&1
printf 'x\n' > "$KB4/sources/a.md"
printf -- '- `published_year`\n- `게재연도`\n' > "$KB4/policy/attribute-relations.md"
printf -- '- `게재연도` -> `published_year`\n' > "$KB4/policy/relation-aliases.md"
printf -- '`published_year` : amount as a1 (파운드=1700)\n`게재연도` : amount as a2 (달러=1300)\n' \
  > "$KB4/policy/typed-relations.md"
printf '%s\n%s\n%s\n' "$H" \
  'P1,published_year,"amount(9,""파운드"")",sources/a.md,accepted,0.9,' \
  'P2,게재연도,"amount(5,""파운드"")",sources/a.md,accepted,0.9,' > "$KB4/facts/candidates.csv"
set +e; out="$("$PYTHON" "$AUDIT" --wiki "$KB4" 2>&1)"; rc=$?; set -e

[ "$rc" -eq 0 ] && ok "conflicting-declaration KB exits 0" || bad "conflicting-declaration KB exit $rc"
printf '%s' "$out" | grep -qF '• '"'"'amount(9,"파운드")'"'" && bad "canonical-relation amount falsely accused (#393)" || ok "no false accusation under the canonical relation"
# Match the SECTION header, not the bare phrase: the summary line carries the same
# words, so a loose grep passed even with conflict detection removed entirely.
printf '%s' "$out" | grep -qF "conflicting typed declaration (one relation form, two declarations):" && ok "conflicting-declaration section printed" || bad "conflicting-declaration section missing"
printf '%s' "$out" | grep -qF "'published_year' is declared by" && ok "contested form named with its claimants" || bad "contested form not named"
# Both expanded forms are contested here, so the count is exact — an assertion of
# "some number" would hold at zero.
printf '%s' "$out" | grep -qE "2 conflicting typed declaration\(s\)" && ok "summary counts conflicting typed declarations" || bad "summary omits conflicting declaration count"
# Report ORDER is part of the contract (a diffable, re-runnable report). Both
# contested forms are listed, canonical first — reversing the sort must fail here.
# `|| true`: under `set -e` a non-matching grep inside a command substitution aborts
# the whole script, which turns "this assertion failed" into "the gate stopped early
# and reported nothing". Fail as a FAIL line, not as a silent exit.
order="$(printf '%s' "$out" | { grep -oE "'(published_year|게재연도)' is declared by" || true; } | tr '\n' '|')"
[ "$order" = "'published_year' is declared by|'게재연도' is declared by|" ] \
  && ok "conflicting forms reported in a fixed order" || bad "conflict report order not fixed (got: $order)"

# --- agreeing canonical/alias declarations are NOT a conflict (#393) ----------
# The same unit table on both lines, differing only in the engine-side alias — which
# every real pair must, since common.py rejects a duplicate alias. Comparing whole
# specs made this pair "self-contradictory" and dropped its table: a NEW false
# positive, and a coverage regression against main's last-writer-wins (which at
# least landed on an identical table). Pinned through REAL policy files, because
# the property only holds for spec objects the parser actually builds.
KB5="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB5" >/dev/null 2>&1
printf 'x\n' > "$KB5/sources/a.md"
printf -- '- `published_year`\n- `게재연도`\n' > "$KB5/policy/attribute-relations.md"
printf -- '- `게재연도` -> `published_year`\n' > "$KB5/policy/relation-aliases.md"
printf -- '`published_year` : amount as a1 (파운드=1700)\n`게재연도` : amount as a2 (파운드=1700)\n' \
  > "$KB5/policy/typed-relations.md"
printf '%s\n%s\n%s\n%s\n' "$H" \
  'P1,published_year,"amount(9,""파운드"")",sources/a.md,accepted,0.9,' \
  'P2,게재연도,"amount(5,""파운드"")",sources/a.md,accepted,0.9,' \
  'P3,published_year,"amount(5,""달러"")",sources/a.md,accepted,0.9,' > "$KB5/facts/candidates.csv"
set +e; out="$("$PYTHON" "$AUDIT" --wiki "$KB5" 2>&1)"; rc=$?; set -e

[ "$rc" -eq 0 ] && ok "agreeing-declaration KB exits 0" || bad "agreeing-declaration KB exit $rc"
printf '%s' "$out" | grep -qE "0 conflicting typed declaration\(s\)" && ok "agreeing declarations are not a conflict" || bad "agreeing declarations wrongly reported as conflicting"
printf '%s' "$out" | grep -qF "conflicting typed declaration (one relation form, two declarations):" && bad "conflict section printed for agreeing declarations" || ok "no conflict section for agreeing declarations"
printf '%s' "$out" | grep -qF '• '"'"'amount(9,"파운드")'"'" && bad "agreed unit table not applied (value falsely accused)" || ok "the agreed unit table still applies"
# ...and the table being kept must still COST something: an out-of-table unit is
# reported, so "no conflict" cannot be faked by dropping judgement altogether.
printf '%s' "$out" | grep -qF '• '"'"'amount(5,"달러")'"'" && ok "out-of-table unit still reported under agreeing declarations" || bad "kept table stopped judging unknown units"

# --- a contested form names EVERY claimant (#393) ------------------------------
# Three lines on one canonical where the first two AGREE and the third differs.
# Reporting only the disagreeing pair drops an agreeing line and sends the author
# to edit the wrong ones, so all three names must appear on the conflict line.
KB6="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB6" >/dev/null 2>&1
printf 'x\n' > "$KB6/sources/a.md"
printf -- '- `published_year`\n- `게재연도`\n- `출판연도`\n' > "$KB6/policy/attribute-relations.md"
printf -- '- `게재연도` -> `published_year`\n- `출판연도` -> `published_year`\n' > "$KB6/policy/relation-aliases.md"
printf -- '`published_year` : amount as a1 (파운드=1700)\n`게재연도` : amount as a2 (파운드=1700)\n`출판연도` : amount as a3 (달러=1300)\n' \
  > "$KB6/policy/typed-relations.md"
printf '%s\n%s\n' "$H" \
  'P1,published_year,"amount(9,""파운드"")",sources/a.md,accepted,0.9,' > "$KB6/facts/candidates.csv"
set +e; out="$("$PYTHON" "$AUDIT" --wiki "$KB6" 2>&1)"; rc=$?; set -e

[ "$rc" -eq 0 ] && ok "three-claimant KB exits 0" || bad "three-claimant KB exit $rc"
line="$(printf '%s' "$out" | { grep -F "'published_year' is declared by" || true; })"
for n in published_year 게재연도 출판연도; do
  printf '%s' "$line" | grep -qF "'$n'" && ok "conflict names claimant '$n'" || bad "conflict omits claimant '$n'"
done

echo ""
echo "========================================"
echo "test_entity_audit: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
