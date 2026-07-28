#!/usr/bin/env bash
# tests/test_ask_router.sh — deterministic /factlog ask routing core
#
# Proves the reason-class routing and relation evaluation of tools/ask_router.py:
#   - matching relation            -> route=engine, negative=false
#   - accepted vocab, fact absent  -> route=engine, negative=TRUE (verified
#                                     negative — NEVER wiki). RELATION only: since
#                                     #303 a path query's reachability is the
#                                     engine's call, so the gate never flags a path
#                                     negative — a path verified-negative is the
#                                     engine's empty render, not a classify flag.
#   - unknown entity/predicate/no '?' -> route=wiki
#   - review_required predicate    -> route=wiki
#   - works with NO compiled policy (fresh KB), i.e. no hard exit
#   - evaluate returns matching rows / 0 rows
#   - render emits the greppable VERIFIED — engine marker (positive & negative)
#   - ask_router never writes facts/query.dl or mutates facts/accepted.dl
#
# Runs from the working tree via PYTHONPATH (no install / no pyrewire needed for
# the relation path).
#
# Usage: bash tests/test_ask_router.sh
#   Returns 0 if all checks pass, 1 if any fail.

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine
# Several checks below pin RANKED ORDER (#31 relevance, #572 directory grade, #573
# path damping), and those guarantees are scoped to the bundled lexical path:
# _semantic_rerank reorders the whole result list whenever FACTLOG_EMBED_MODULE names
# an importable backend, so the var inherited from a developer's shell turns them into
# false alarms. Measured on b0618a6 — this file WITHOUT the unset below, run with
# FACTLOG_EMBED_MODULE naming a stub whose `rank` returns ascending scores (an exact
# reversal): 241 passed, 7 failed, and all seven failures are order pins. Same
# reasoning as tests/test_ask_wiki_search.sh, which unsets it for its own pins (#589).
# The cases that exercise the backend-ON path set the var as a single-command prefix
# (`FACTLOG_EMBED_MODULE=..._stub "$PYTHON" "$ROUTER" ...`), which applies to that one
# command and nothing after it — unsetting here does not disable them.
unset FACTLOG_EMBED_MODULE

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
ROUTER="$PLUGIN_ROOT/tools/ask_router.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# A minimal KB with two accepted relation facts and NO compiled policy
# (policy/logic-policy.dl intentionally absent — ask must tolerate it).
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf '// test\nrelation("Acme API", "uses", "FastAPI").\nrelation("Acme API", "depends_on", "Postgres").\n' \
  > "$KB/facts/accepted.dl"
ACCEPTED_BEFORE="$(cat "$KB/facts/accepted.dl")"

router() { "$PYTHON" "$ROUTER" "$@" --target "$KB"; }

# field <json> <key> : print a top-level JSON value
field() { "$PYTHON" -c "import json,sys; print(json.load(sys.stdin).get(sys.argv[1]))" "$1"; }

check_field() {  # check_field <desc> <subcmd> <draft> <key> <expected>
  local desc="$1" sub="$2" draft="$3" key="$4" expected="$5"
  local got; got="$(router "$sub" "$draft" | field "$key")"
  if [ "$got" = "$expected" ]; then ok "$desc ($key=$got)"; else bad "$desc — expected $key=$expected, got $got"; fi
}

# --- routing classification ---
check_field "matching relation routes engine" validate 'relation("Acme API", "uses", V)?' route engine
check_field "matching relation not negative"  validate 'relation("Acme API", "uses", V)?' negative False
check_field "absent fact = verified negative (engine, not wiki)" validate 'relation("Acme API", "uses", "Postgres")?' route engine
check_field "absent fact flagged negative"    validate 'relation("Acme API", "uses", "Postgres")?' negative True
check_field "unknown entity routes wiki"      validate 'relation("Nope", "uses", V)?' route wiki
check_field "unknown predicate routes wiki"   validate 'bogus("Acme API")?' route wiki
check_field "missing question mark routes wiki" validate 'relation("Acme API", "uses", V)' route wiki
check_field "review_required routes wiki"     validate 'review_required("why does it matter?")?' route wiki

# --- tolerance of missing compiled policy ---
if router validate 'relation("Acme API", "uses", V)?' >/dev/null 2>&1; then
  ok "validate works with no policy/logic-policy.dl (no hard exit)"
else
  bad "validate hard-failed on a KB without compiled policy"
fi

# --- evaluation ---
check_field "evaluate matching returns 1 row" evaluate 'relation("Acme API", "uses", V)?' count 1
check_field "evaluate non-matching returns 0 rows" evaluate 'relation("Acme API", "uses", "Nope")?' count 0

# --- render markers ---
if router render 'relation("Acme API", "uses", V)?' | grep -qF "VERIFIED — engine"; then ok "render positive carries VERIFIED — engine marker"; else bad "render positive missing VERIFIED marker"; fi
if router render 'relation("Acme API", "uses", V)?' | grep -qF "Acme API, uses, FastAPI"; then ok "render positive shows the matched row"; else bad "render positive missing matched row"; fi
neg="$(router render 'relation("Acme API", "uses", "Postgres")?')"
if printf '%s' "$neg" | grep -qF "VERIFIED — engine" && printf '%s' "$neg" | grep -qF "verified negative"; then ok "render verified-negative is engine-marked"; else bad "render verified-negative not engine-marked"; fi

# #273: accepted-vocabulary spelling hints decorate only the stable wiki miss;
# they neither change its route nor rewrite/retry the draft.
entity_directive="$(router render 'relation("Acme AP", "uses", V)?')"
if printf '%s' "$entity_directive" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d['route']=='wiki' and d['did_you_mean']==[{'kind':'entity','term':'Acme AP','suggestions':['Acme API']}] else 1)"; then ok "entity typo keeps wiki route and carries deterministic hint"; else bad "entity typo directive/hint wrong: $entity_directive"; fi
entity_wiki="$(router wiki 'What does Acme AP use?' --reason 'entity not accepted' --draft 'relation("Acme AP", "uses", V)?')"
if printf '%s' "$entity_wiki" | grep -qF "note: no accepted entity 'Acme AP'. did you mean: Acme API?"; then ok "wiki answer appends entity did-you-mean without correction"; else bad "wiki entity hint missing"; fi
relation_directive="$(router render 'relation("Acme API", "use", V)?')"
if printf '%s' "$relation_directive" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d['did_you_mean']==[{'kind':'relation','term':'use','suggestions':['uses']}] else 1)"; then ok "relation typo gets accepted-relation hint"; else bad "relation typo hint wrong: $relation_directive"; fi
if printf '%s' "$neg" | grep -qF 'did_you_mean\|did you mean'; then bad "verified negative must not get typo hint"; else ok "verified negative stays hint-free"; fi
distant="$(router render 'relation("Completely Distant", "uses", V)?')"
if printf '%s' "$distant" | "$PYTHON" -c "import json,sys; raise SystemExit(0 if not json.load(sys.stdin)['did_you_mean'] else 1)"; then ok "distant entity stays hint-free"; else bad "distant entity produced false-positive hint"; fi
case_only="$(router render 'relation("acme api", "uses", V)?')"
if printf '%s' "$case_only" | "$PYTHON" -c "import json,sys; raise SystemExit(0 if not json.load(sys.stdin)['did_you_mean'] else 1)"; then ok "case-only entity variant stays hint-free"; else bad "case-only entity variant produced hint"; fi
LKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$LKB" >/dev/null
printf 'relation("Acme API", "year", "2024").\n' > "$LKB/facts/accepted.dl"
printf '%s\n' '- year' > "$LKB/policy/attribute-relations.md"
literal_miss="$("$PYTHON" "$ROUTER" render 'relation("Acme API", "year", "202")?' --target "$LKB")"
if printf '%s' "$literal_miss" | "$PYTHON" -c "import json,sys; raise SystemExit(0 if not json.load(sys.stdin)['did_you_mean'] else 1)"; then ok "attribute literal never becomes an entity spelling hint"; else bad "attribute literal leaked into entity hint"; fi

# --- path routing & verified-negative (renderable for any predicate) ---
check_field "reachable path routes engine" validate 'path("Acme API", "FastAPI")?' route engine
check_field "unreachable path = verified negative (engine)" validate 'path("Postgres", "FastAPI")?' route engine
# #303: the gate no longer re-derives path reachability (it has no engine pairs and
# its python graph is a mirror the engine's fixpoint may outrun), so a path query is
# not flagged negative at classify time -- classify.negative is False. The verified
# NEGATIVE is proven at RENDER time by the engine's own empty result (see the render
# assertion below, which is unchanged). relation queries still flag negative via the
# gate's FACT_ABSENT (that path keeps a match-count check), so L58 above is untouched.
check_field "unreachable path not flagged negative by the gate (#303: engine decides)" validate 'path("Postgres", "FastAPI")?' negative False
# The path verified-negative RENDER assertion moved into the pyrewire guard below:
# since #303 the gate no longer approximates path reachability, so a path negative is
# proven by RUNNING the engine (evaluate -> run_wirelog), not by a gate shortcut. The
# old code emitted "VERIFIED — engine" here WITHOUT the engine, which was itself
# dishonest; with no pyrewire the answer now degrades to a wiki directive (the correct
# "cannot verify" outcome), so the assertion belongs where the engine is available.

# --- #193: uncompiled-but-authored policy warns (no silent ignore) ---
# ask mirrors /factlog check's detection: logic-policy.dl absent + logic-policy.md
# defines compilable rules => policy is IGNORED, so warn (a hint, not a hard fail).
# Baseline: the fixture's logic-policy.md is prose only (no rules) and has no
# compiled logic-policy.dl -> the benign no-policy case must stay quiet.
check_field "benign no-policy KB not flagged uncompiled" validate 'relation("Acme API", "uses", V)?' policy_uncompiled False
if router render 'relation("Acme API", "uses", V)?' | grep -qF "policy is uncompiled"; then
  bad "benign no-policy KB should not emit the uncompiled-policy warning"
else
  ok "benign no-policy KB stays quiet (legitimate empty policy tolerated)"
fi

# Author writes a compilable rule in logic-policy.md but never compiles it
# (logic-policy.dl still absent). ask must now warn instead of silently ignoring.
POLICY_MD_BAK="$(cat "$KB/policy/logic-policy.md")"
printf '# policy\n## Rules\n- [usage_chain] 어떤 항목이 `uses` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB/policy/logic-policy.md"

check_field "validate flags uncompiled authored policy" validate 'relation("Acme API", "uses", V)?' policy_uncompiled True
if router render 'relation("Acme API", "uses", V)?' | grep -qF "policy is uncompiled"; then ok "engine render warns on uncompiled authored policy"; else bad "engine render did not warn on uncompiled policy"; fi
if router render 'relation("Acme API", "uses", "Postgres")?' | grep -qF "policy is uncompiled"; then ok "verified-negative render warns on uncompiled policy"; else bad "verified-negative render did not warn"; fi
# the warning augments — it must NOT suppress the engine answer itself
if router render 'relation("Acme API", "uses", V)?' | grep -qF "VERIFIED — engine"; then ok "engine answer still rendered alongside the warning"; else bad "warning suppressed the engine answer"; fi
# wiki path: render directive carries the structured flag; wiki answer surfaces the warning
if [ "$(router render 'relation("Nope", "uses", V)?' | field policy_uncompiled)" = "True" ]; then ok "wiki route directive carries policy_uncompiled=true"; else bad "wiki directive missing policy_uncompiled flag"; fi
if router wiki 'Nope 관련 자료가 있나' | grep -qF "policy is uncompiled"; then ok "wiki answer warns on uncompiled policy"; else bad "wiki answer did not warn"; fi

# restore the prose template so later assertions see the benign no-policy KB again
printf '%s\n' "$POLICY_MD_BAK" > "$KB/policy/logic-policy.md"
if router render 'relation("Acme API", "uses", V)?' | grep -qF "policy is uncompiled"; then bad "warning persisted after restoring prose-only policy"; else ok "warning clears once authored rules are removed"; fi

# #209 (A): .dl PRESENT + md rules present => NOT uncompiled. This pins the
# `if LOGIC_POLICY_DL.is_file(): return False` short-circuit in _policy_uncompiled:
# once the policy is compiled, authored md rules no longer trigger the warning even
# though logic-policy.md still contains them. (A compiled .dl means policy IS applied.)
# Control pair: this reuses the SAME md rule proven detectable at the True assertion
# above (~L105, .dl absent => uncompiled=True); the only variable here is .dl presence.
# If that True assertion is removed, this contrast weakens — keep the pair together.
printf '# policy\n## Rules\n- [usage_chain] 어떤 항목이 `uses` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB/policy/logic-policy.md"
: > "$KB/policy/logic-policy.dl"   # present (empty is enough: detection keys on existence, not content)
check_field "compiled policy (.dl present) not flagged uncompiled despite md rules" validate 'relation("Acme API", "uses", V)?' policy_uncompiled False
if router render 'relation("Acme API", "uses", V)?' | grep -qF "policy is uncompiled"; then bad "compiled policy still warned uncompiled (.dl-present short-circuit broken)"; else ok "compiled policy (.dl present) suppresses the uncompiled warning"; fi
rm -f "$KB/policy/logic-policy.dl"
printf '%s\n' "$POLICY_MD_BAK" > "$KB/policy/logic-policy.md"   # restore benign prose-only policy

# #198 (PARITY FIX): rules living ONLY in logic-policy.extra.dl (with logic-policy.dl
# ABSENT) are now LOADED and evaluated by ask, matching /factlog check — they are no
# longer silently ignored. _policy_program_optional reuses common.load_logic_policy()
# (the same loader check uses, which merges extra.dl onto an empty base when the .dl
# is absent, #190). CORE REGRESSION GUARD: routing keys on policy_predicates() over
# _policy_program_optional(), which is pure regex over the assembled program (no
# pyrewire), so the route=engine assertion runs everywhere and fails on pre-#198 code
# (which short-circuited to '' when the .dl was absent -> unknown_predicate -> wiki).
# The extra.dl-only case still does NOT trip the uncompiled-md warning: _policy_uncompiled
# inspects logic-policy.dl + logic-policy.md only (extra.dl is a real, applied policy,
# not an uncompiled-md defect) — so #193's benign no-md-rules tolerance is preserved.
printf '.decl uses_fastapi(entity: symbol, reason: symbol)\nuses_fastapi(S, "uses_fastapi") :- relation(S, "uses", "FastAPI").\n' > "$KB/policy/logic-policy.extra.dl"
check_field "extra.dl-only predicate routes engine, not ignored (#198 fix, pre-fix -> wiki)" validate 'uses_fastapi(E, R)?' route engine
check_field "extra.dl-only predicate code=ok (#198)" validate 'uses_fastapi(E, R)?' code ok
check_field "extra.dl-only rules do not flag uncompiled-md (#193 preserved)" validate 'relation("Acme API", "uses", V)?' policy_uncompiled False
if router render 'relation("Acme API", "uses", V)?' | grep -qF "policy is uncompiled"; then bad "extra.dl-only KB warned uncompiled — extra.dl is an applied policy, not an uncompiled-md defect"; else ok "extra.dl-only KB emits no uncompiled-md warning (#193 preserved)"; fi
rm -f "$KB/policy/logic-policy.extra.dl"

# #198 graceful: a MALFORMED extra.dl that makes the shared loader raise
# (_load_logic_policy_from fails loud on a canonical/3 head in the policy text —
# the exact fail-loud check is a NEEDS for /factlog check) must NOT hard-fail ask.
# _policy_program_optional catches FactlogError and degrades to no policy (''), so
# the predicate is unknown -> honest wiki route, rc 0 — ask never raises (#193
# contract). check stays loud on the same input; only ask degrades.
printf 'canonical("x", "y", "z").\n.decl bogusp(e: symbol)\nbogusp(S) :- relation(S, "uses", "FastAPI").\n' > "$KB/policy/logic-policy.extra.dl"
if router validate 'relation("Acme API", "uses", V)?' >/dev/null 2>&1; then ok "malformed extra.dl (canonical head) — ask validate stays graceful (rc 0)"; else bad "malformed extra.dl hard-failed ask validate (must degrade, not raise — #193)"; fi
if router render 'bogusp(S)?' >/dev/null 2>&1; then ok "malformed extra.dl — ask render stays graceful (rc 0), predicate degrades to wiki"; else bad "malformed extra.dl hard-failed ask render"; fi
check_field "malformed extra.dl — predicate degrades to wiki (no policy applied)" validate 'bogusp(S)?' route wiki
rm -f "$KB/policy/logic-policy.extra.dl"

# --- regression: an unaccepted relation name containing the fact-absence
# phrase must route to wiki, NOT masquerade as a verified negative (exact-match) ---
check_field "marker-collision relation name routes wiki" validate 'relation("Acme API", "does not match accepted facts", "X")?' route wiki
check_field "marker-collision not flagged negative" validate 'relation("Acme API", "does not match accepted facts", "X")?' negative False

# --- structured classification codes (routing is by code, not reason text) ---
check_field "matching relation code=ok" validate 'relation("Acme API", "uses", V)?' code ok
check_field "absent fact code=fact_absent" validate 'relation("Acme API", "uses", "Postgres")?' code fact_absent
check_field "unknown predicate code=unknown_predicate" validate 'bogus("Acme API")?' code unknown_predicate
# marker-collision: an unaccepted relation NAME containing the fact-absence
# phrase classifies as relation_not_accepted — structurally NOT fact_absent —
# so it can never masquerade as a verified negative regardless of its text.
check_field "marker-collision code=relation_not_accepted (not fact_absent)" validate 'relation("Acme API", "does not match accepted facts", "X")?' code relation_not_accepted

# A relation present ONLY among candidates (candidates.csv) but NOT accepted must
# route to wiki — proving validation is against load_accepted_facts(), never
# load_facts(). Without this, candidate vocabulary would leak into the engine.
printf 'subject,relation,object,source,status,confidence,note\nAcme API,may_use,Datadog,sources/x.md,candidate,0.40,draft\n' > "$KB/facts/candidates.csv"
check_field "candidate-only relation routes wiki (accepted-only, no candidate leak)" validate 'relation("Acme API", "may_use", "Datadog")?' route wiki
check_field "candidate-only relation code=relation_not_accepted" validate 'relation("Acme API", "may_use", "Datadog")?' code relation_not_accepted
rm -f "$KB/facts/candidates.csv"

# --- Path B: wiki exploration (sources/ + runs/sources/ only; pages/ excluded) ---
printf '# Acme\n\nAcme API uses FastAPI for routing.\n' > "$KB/sources/acme.md"
mkdir -p "$KB/runs/sources"
printf '<!-- ingested -->\n\nThe WidgetX platform integrates ToolA.\n' > "$KB/runs/sources/widgetx.md"
# A pages/ file encoding an UNACCEPTED candidate triple — must NEVER surface in B.
printf '<!-- generated-by-factlog -->\n# Acme API\n- may_use -> [[Datadog]] (sources/x.md, confidence=0.40)\n' > "$KB/pages/acme-api.md"

if router search "what uses FastAPI" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(r['dir']=='sources' for r in d['results']) else 1)"; then ok "search finds excerpts in sources/"; else bad "search missed sources/"; fi
if router search "WidgetX ToolA" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if any(r['dir']=='runs/sources' for r in d['results']) else 1)"; then ok "search finds excerpts in runs/sources/"; else bad "search missed runs/sources/"; fi
# pages/ candidate content must never appear in search citations
if router search "Datadog may_use" | grep -qE 'pages/|may_use|confidence=0\.40'; then bad "pages/ candidate content leaked into search results"; else ok "pages/ excluded from search (no candidate leak)"; fi

# Sync-ignored primary sources are deliberately excluded from wiki evidence;
# non-ignored sources and supplementary decisions retain their normal behavior.
mkdir -p "$KB/sources/drafts" "$KB/runs/sources/drafts"
printf 'syncignoretoken appears only in an ignored original.\n' > "$KB/sources/drafts/ignored.md"
printf 'syncignoretoken appears only in an ignored conversion.\n' > "$KB/runs/sources/drafts/ignored.md"
printf 'syncignoretoken appears in the retained source.\n' > "$KB/sources/retained.md"
printf -- '- drafts/**\n' >> "$KB/policy/sync-ignore.md"
ignored_search="$(router search "syncignoretoken")"
if printf '%s' "$ignored_search" | grep -qE 'sources/drafts/ignored\.md|runs/sources/drafts/ignored\.md'; then bad "sync-ignored sources leaked into search results"; else ok "sync-ignored sources excluded from search results"; fi
if printf '%s' "$ignored_search" | grep -qF 'sources/retained.md'; then ok "non-ignored source remains in search results"; else bad "non-ignored source missing from search results"; fi
if router wiki "syncignoretoken" | grep -qE 'sources/drafts/ignored\.md|runs/sources/drafts/ignored\.md'; then bad "sync-ignored sources leaked into rendered wiki evidence"; else ok "sync-ignored sources excluded from rendered wiki evidence"; fi
rm -rf "$KB/sources/drafts" "$KB/runs/sources/drafts" "$KB/sources/retained.md"
# Keep later search cases independent from this filtering fixture.
sed -i.bak '/^- drafts\/\*\*$/d' "$KB/policy/sync-ignore.md" && rm -f "$KB/policy/sync-ignore.md.bak"

wiki_out="$(router wiki "what uses FastAPI" --reason "unknown entity")"
if printf '%s' "$wiki_out" | grep -qF "UNVERIFIED — wiki exploration"; then ok "wiki answer carries UNVERIFIED marker"; else bad "wiki answer missing UNVERIFIED marker"; fi
if printf '%s' "$wiki_out" | grep -qF "sources/acme.md:"; then ok "wiki answer cites a source path:line"; else bad "wiki answer missing citation"; fi
if printf '%s' "$wiki_out" | grep -qF "accepted.dl"; then bad "wiki answer cites accepted.dl (must not)"; else ok "wiki answer never cites accepted.dl"; fi
# pages/ candidate content must never appear in a rendered wiki answer (ignore the echoed question line)
if router wiki "does Acme use Datadog" | grep -vE '^question:' | grep -qE 'pages/|may_use|confidence=0\.40'; then bad "pages/ candidate content leaked into wiki answer"; else ok "wiki answer free of pages/ candidate content"; fi

# --- note sink: a non-engine-input file, never facts/query.dl ---
router note "an unanswered question for later" >/dev/null
if [ -f "$KB/decisions/ask-open-questions.md" ]; then ok "note writes the open-questions sink"; else bad "note did not create the sink file"; fi
if grep -qF "an unanswered question for later" "$KB/decisions/ask-open-questions.md"; then ok "note records the question verbatim"; else bad "note did not record the question"; fi

# --- Path B robustness ---
# valid-UTF-8-with-NUL (binary-ish / malformed conversion) must be skipped, never emitted
printf 'FastAPI \x00\x00 control \x07 bytes here\n' > "$KB/sources/weird.txt"
if router search "control bytes" | grep -qF "weird.txt"; then bad "binary/control-byte file leaked into search"; else ok "NUL/control file skipped by search"; fi
rm -f "$KB/sources/weird.txt"
# word-boundary matching: 'api' must not match 'therapist'/'rapid'
printf 'The therapist gave rapid feedback.\n' > "$KB/sources/wb.md"
if router search "api" | grep -qF "wb.md"; then bad "substring keyword matched (therapist/rapid)"; else ok "word-boundary keyword matching (no substring false positive)"; fi
rm -f "$KB/sources/wb.md"
# overlapping windows collapse: two adjacent matching lines -> a single excerpt
printf 'pad\npad\nmatchword here\nmatchword again\npad\npad\n' > "$KB/sources/dup.md"
ndup="$(router search "matchword" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(sum(1 for r in d['results'] if r['file']=='sources/dup.md'))")"
if [ "$ndup" = "1" ]; then ok "overlapping windows collapse to one excerpt"; else bad "overlapping windows not collapsed (got $ndup excerpts)"; fi
rm -f "$KB/sources/dup.md"
# empty/whitespace note is not recorded as a blank bullet
router note "   " >/dev/null
if grep -qE '^- *$' "$KB/decisions/ask-open-questions.md" 2>/dev/null; then bad "blank note recorded"; else ok "blank note not recorded"; fi

# --- bilingual keywords: 2-char Korean terms search; particle/josa tolerance ---
printf '# 갑봇\n\n검색 관련 문서 자료는 충분하다.\n' > "$KB/sources/ko.md"
if router search "문서 자료" | grep -qF 'sources/ko.md'; then ok "2-char Korean keywords search (문서/자료) match"; else bad "2-char Korean keywords found nothing"; fi
# substring match tolerates the attached particle: '자료' matches '자료는'
if router search "자료" | grep -qF '자료는'; then ok "CJK substring tolerates a particle (자료 -> 자료는)"; else bad "CJK keyword did not match across a particle"; fi
rm -f "$KB/sources/ko.md"

# --- #571: question function words are not content keywords ------------------
# The defect: every CJK token of len>=2 became a keyword, so '논문은' — pure question
# grammar — matched a topically unrelated retraction notice and cited it as evidence.
#
# Every case below pins the KEYWORD SET, not just "did any result come back": a
# question keeps returning results when a single content word survives, so a
# result-existence check passes even while the filter is silently broken. Each case
# is its own ok/bad and prints the actual patterns, so a failure names itself.
printf '# 철회 공지\n\n이 논문은 저자 요청으로 철회되었다.\n' > "$KB/sources/571-retraction.md"
printf '# 신경기호 추론\n\n신경기호 추론의 근거를 역추적하는 절차.\n' > "$KB/sources/571-topic.md"

# kw_is <desc> <question> <expected repr of the pattern list>
kw_is() {
  local got
  got="$("$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
print([p.pattern for p in a._keyword_patterns(sys.argv[1])])
" "$2")"
  if [ "$got" = "$3" ]; then ok "$1"; else bad "$1 — expected $3, got $got"; fi
}

kw_is "question grammar drops out of the keyword set (#571)" \
  '이 논문은 신경기호 추론의 근거를 어떻게 제시하는가' \
  "['신경기호', '추론의', '근거를', '제시하는가']"
ko_q_out="$(router search '이 논문은 신경기호 추론의 근거를 어떻게 제시하는가')"
if printf '%s' "$ko_q_out" | grep -qF 'sources/571-retraction.md'; then
  bad "function word '논문은' pulled a topically unrelated document into the results"
else
  ok "function word '논문은' does not pull an unrelated document (#571)"
fi
if printf '%s' "$ko_q_out" | grep -qF 'sources/571-topic.md'; then ok "content keywords still reach the on-topic document"; else bad "stop-word filter also removed content keywords"; fi
# Bare stems are NOT stop words (#571 기준 4): the list holds surface forms only.
kw_is "bare stems '논문'/'방법' stay content keywords" '논문 방법' "['논문', '방법']"
# The corpus probe pairs '논문' with a keyword the target document does NOT contain,
# so the match can only come from '논문' itself — a query whose every token is
# filtered would reach the restoring stage and match anyway, hiding the regression.
if router search "논문 신경기호" | grep -qF 'sources/571-retraction.md'; then ok "bare stem '논문' still reaches a document (corpus level)"; else bad "bare stem '논문' was treated as a stop word"; fi
# Copular endings appear ATTACHED; standing alone they are content nouns —
# 인지(cognition), 인가(認可 / 전압 인가). Listing them would over-filter the very
# literature this KB holds.
kw_is "'인지'(cognition) is a content word, not a stop word" '인지 편향' "['인지', '편향']"
kw_is "'인가'(認可) is a content word, not a stop word" '전압 인가 방식' "['전압', '인가', '방식']"
# Whole-token match: a compound merely ENDING in a stop word is content.
kw_is "a compound ending in a stop word is not filtered" '반박논문은 어디에' "['반박논문은']"
# ASCII is untouched by this filter (it is a list of Korean 어절).
kw_is "ASCII questions are untouched by the stop-word list" 'which paper claims this' \
  "['(?<!\\\\w)which(?!\\\\w)', '(?<!\\\\w)paper(?!\\\\w)', '(?<!\\\\w)claims(?!\\\\w)', '(?<!\\\\w)this(?!\\\\w)']"

# A SHORT ASCII content word survives a question whose every other token is a
# function word — never by giving the function words back, which would re-cite the
# retraction notice, the exact defect #571 fixes.
kw_is "short ASCII content word survives an all-function-word frame" \
  'AI 논문은 어디에 있나' "['(?<!\\\\w)ai(?!\\\\w)']"
if router search 'AI 논문은 어디에 있나' | grep -qF 'sources/571-retraction.md'; then
  bad "the short ASCII keyword still cited the unrelated retraction notice"
else
  ok "a short ASCII keyword keeps the unrelated retraction notice out (#571)"
fi
# REVERSED BY #583. #571 pinned the opposite value here ('AI 신경기호' -> ['신경기호']),
# on the reasoning that a 2-char floor would make 'of'/'in' keywords in every question.
# Measured, the floor was never what stopped that — the function-word list is, and it
# still runs. Over the corpus search() actually reads in the reference KB (127 files:
# sources/ + runs/sources/ + decisions/ less the 60 sync-ignored ones), by PROSE reach,
# the 3-char tokens the old floor already admitted beat every unlisted 2-char one:
# and 123, the 120, for 110 vs 10 at 105, 11 at 18, ai at 15. So the initialism is a
# keyword on the DEFAULT path now, and the pin records that direction instead of being
# deleted.
kw_is "a 2-char ASCII content token IS a keyword on the default path (#583)" \
  'AI 신경기호' "['(?<!\\\\w)ai(?!\\\\w)', '신경기호']"
# Each initialism the issue names, on the default path — one case per token with the
# expected set written out, so a floor that regresses for only some of them (a
# hand-written allowlist, say) names which one. The remaining CJK tokens are pinned
# alongside: a keyword set is what is checked here, never "did anything come back".
kw_is "'AI' is a default-path keyword (#583)" 'AI 신경기호 추론' \
  "['(?<!\\\\w)ai(?!\\\\w)', '신경기호', '추론']"
kw_is "'ML' is a default-path keyword (#583)" 'ML 모델 평가' \
  "['(?<!\\\\w)ml(?!\\\\w)', '모델', '평가']"
kw_is "'RL' is a default-path keyword (#583)" 'RL 정책 학습' \
  "['(?<!\\\\w)rl(?!\\\\w)', '정책', '학습']"
kw_is "'QA' is a default-path keyword (#583)" 'QA 데이터셋 구축' \
  "['(?<!\\\\w)qa(?!\\\\w)', '데이터셋', '구축']"
kw_is "'NN' is a default-path keyword (#583)" 'NN 구조 설계' \
  "['(?<!\\\\w)nn(?!\\\\w)', '구조', '설계']"
# The reported defect verbatim: the initialism used to be dropped and the question was
# answered by the 총칭명사 left over from its own frame — #571's class in miniature.
kw_is "the reported case keeps its initialism (#583)" 'QA 방법은 무엇인가' \
  "['(?<!\\\\w)qa(?!\\\\w)', '방법은']"
# A 2-char token is admitted for its LENGTH, not for being a known acronym: digits and
# fragments become keywords too, and that is the measured decision, not an oversight.
# The unlisted 2-char token with the highest prose reach in the reference KB is '10'
# at 105 of the 127 searchable files — below 'and' (123) and 'the' (120), which the old
# 3-char floor already admitted. If a later change adds an acronym allowlist, this case
# fails and says so.
kw_is "a numeric 2-char token is a keyword too — length, not an allowlist (#583)" \
  '19 세기 연구' "['(?<!\\\\w)19(?!\\\\w)', '세기', '연구']"
# ...and the function-word list must not let English GRAMMAR in on that same path.
# 'of' reaches 183 of 187 files in prose — promoting it is the same defect as '논문은',
# in the other language. This check is what makes the list load-bearing rather than
# decorative: before #583 the floor alone would have dropped 'of'.
kw_is "a 2-char English function word is NOT a keyword on the default path" \
  '이것은 무엇인가 of 그것은' "[]"
kw_is "a short content initialism outlives an all-function-word frame" 'QA 논문은 어디에 있나' \
  "['(?<!\\\\w)qa(?!\\\\w)']"
# The keyword set is decided in ONE pass: #571's relaxed-floor recovery stage is gone
# (#583), because re-running the same floor cannot change a result. Checked by
# consequence — no floor is looser than the default one, so no question that comes back
# empty could have been rescued: '이것은 무엇인가 of 그것은' stays [] however often it
# is asked, and the module carries no second floor constant to diverge from _ASCII_MIN.
if "$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
assert a._ASCII_MIN == 2, a._ASCII_MIN
assert not [n for n in vars(a) if n.startswith('_ASCII_MIN') and n != '_ASCII_MIN'], \
    [n for n in vars(a) if n.startswith('_ASCII_MIN')]
# One pass: the tokenizer takes no floor argument, so there is no second stage to pass
# a different one to.
import inspect
params = list(inspect.signature(a._tokenize_keywords).parameters)
assert params == ['question'], params
" 2>/dev/null; then ok "the keyword set is decided in one pass; no relaxed-floor stage remains (#583)"; else bad "a second ASCII floor or tokenizer stage is back: $("$PYTHON" -c "
import sys, os, inspect
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
print(sorted(n for n in vars(a) if n.startswith('_ASCII_MIN')), list(inspect.signature(a._tokenize_keywords).parameters))
")"; fi
# 1-character tokens stay out in BOTH scripts. The floor moved to 2, not to 0, and
# NO_QUERY_TERM_NOTE names 'single-character tokens' as a cause — that claim has to
# stay true or the diagnostic misdiagnoses the question.
kw_is "a 1-char ASCII token is still not a keyword" 'a 신경기호' "['신경기호']"
kw_is "a 1-char CJK token is still not a keyword" '이 신경기호' "['신경기호']"
printf 'This paper is about retrieval of evidence in a corpus.\n' > "$KB/sources/571-english.md"
if router search '이것은 무엇인가 of 그것은' | grep -qF 'sources/571-english.md'; then
  bad "an English function word became the entire query"
else
  ok "English function words do not become the entire query (#571)"
fi
rm -f "$KB/sources/571-english.md"
# A question that is function words and NOTHING else yields NO keyword and NO
# result (#571 기준 2, revised). Restoring the tokens — the behaviour this replaced —
# put the retraction notice back as the sole evidence for '이 논문은?'.
printf '# 메모\n\n이것은 무엇인가 하는 물음.\n' > "$KB/sources/571-allstop.md"
kw_is "all-function-word question yields no keyword" '이것은 무엇인가' "[]"
kw_is "all-function-word question yields no keyword (bare 논문은)" '이 논문은?' "[]"
if router search "이것은 무엇인가" | grep -qF 'sources/571-allstop.md'; then bad "all-function-word question still cited a document"; else ok "all-function-word question cites nothing (#571 기준 2)"; fi
if router search '이 논문은?' | grep -qF 'sources/571-retraction.md'; then bad "'이 논문은?' still cited the retraction notice"; else ok "'이 논문은?' no longer cites the retraction notice (#571)"; fi
# ...and the emptiness is EXPLAINED, distinguishably from "the corpus has nothing".
# Both surfaces: the rendered block and the search JSON.
noterm_answer="$(router wiki '이 논문은?' --reason 'unknown entity')"
nomatch_answer="$(router wiki 'quantumentanglementxyz' --reason 'unknown entity')"
if printf '%s' "$noterm_answer" | grep -qF 'no searchable keyword'; then ok "no-keyword answer explains why it is empty"; else bad "no-keyword answer gave no diagnostic"; fi
if printf '%s' "$noterm_answer" | grep -qF '(no matching source excerpts found)'; then bad "no-keyword answer claims the corpus was searched and empty"; else ok "no-keyword answer is not reported as 'no such source'"; fi
if printf '%s' "$nomatch_answer" | grep -qF '(no matching source excerpts found)'; then ok "a searched-but-unmatched question keeps the 'no such source' wording"; else bad "unmatched question lost its own wording"; fi
if printf '%s' "$nomatch_answer" | grep -qF 'no searchable keyword'; then bad "unmatched question wrongly reported as unsearchable"; else ok "the two empty-result reasons are not conflated (#571)"; fi
# #583 does NOT move any question across that boundary, and that is the point worth
# recording. Before, _keywords returned stage 0 (floor 3), or stage 1 (floor 2) when
# stage 0 came back empty; stage 0's output is a SUBSET of stage 1's, so the result was
# empty exactly when stage 1 was — which is precisely what the single floor-2 pass now
# computes. Measured by running both versions over 32,882 generated questions (every
# 1- and 2-token combination of function words, 1-char tokens, initialisms and content
# words, plus 20,000 random longer ones): 0 questions changed empty/non-empty status,
# 0 lost a keyword, 1,809 gained one. So the no-keyword population is unchanged and the
# diagnostic contract is carried over intact rather than re-derived.
#
# 'zq 무엇인가' pins the searched side of that boundary: its only content token is 2
# chars, so it is a keyword by the FLOOR now instead of by a fallback, and the corpus
# not containing it must read as "searched and found nothing" — not "you asked nothing
# searchable". The two wordings are checked separately below so a regression names which.
kw_is "a question whose only content token is 2 chars is searchable by the floor (#583)" \
  'zq 무엇인가' "['(?<!\\\\w)zq(?!\\\\w)']"
xfer_answer="$(router wiki 'zq 무엇인가' --reason 'unknown entity')"
if printf '%s' "$xfer_answer" | grep -qF '(no matching source excerpts found)'; then
  ok "a newly-searchable 2-char question is reported as searched-and-unmatched (#583)"
else
  bad "newly-searchable 2-char question lost the 'no such source' wording"
fi
if printf '%s' "$xfer_answer" | grep -qF 'no searchable keyword'; then
  bad "a question WITH a 2-char keyword is still reported as unsearchable (#583)"
else
  ok "a question with a 2-char keyword is not reported as unsearchable (#583)"
fi
# NO_QUERY_TERM_NOTE names exactly two causes — function words and single-character
# tokens. With the floor at 2 that claim is checkable EXHAUSTIVELY rather than by
# sampling: over every 1- to 3-token combination of the four token classes, the keyword
# set is empty IF AND ONLY IF every token is a listed function word or a single
# character. A third cause would show up as a question that empties without qualifying,
# and the note would then be misdiagnosing it.
if "$PYTHON" -c "
import sys, os, itertools
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
cjk_fw = sorted(a._CJK_QUESTION_STOPWORDS)[:8]
en_fw = sorted(a._ASCII_FUNCTION_WORDS)[:8]
one = ['a', 'z', '왜', '이']
content = ['ai', '10', 'the', '신경기호', 'c++']
vocab = cjk_fw + en_fw + one + content
def excusable(tok):
    return len(tok) == 1 or tok in a._CJK_QUESTION_STOPWORDS or tok in a._ASCII_FUNCTION_WORDS
bad = []
for n in (1, 2, 3):
    for combo in itertools.product(vocab, repeat=n):
        empty = not a._keywords(' '.join(combo))
        if empty != all(excusable(t) for t in combo):
            bad.append((combo, empty))
assert not bad, bad[:5]
print(len(vocab))
" >/dev/null 2>&1; then ok "the no-keyword diagnostic's two causes are exhaustive (all 1-3 token combinations)"; else bad "a question empties the keyword set for an undocumented reason: $("$PYTHON" -c "
import sys, os, itertools
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
vocab = sorted(a._CJK_QUESTION_STOPWORDS)[:8] + sorted(a._ASCII_FUNCTION_WORDS)[:8] + ['a', 'z', '왜', '이', 'ai', '10', 'the', '신경기호', 'c++']
def excusable(t):
    return len(t) == 1 or t in a._CJK_QUESTION_STOPWORDS or t in a._ASCII_FUNCTION_WORDS
out = [(c, not a._keywords(' '.join(c))) for n in (1, 2, 3) for c in itertools.product(vocab, repeat=n)
       if (not a._keywords(' '.join(c))) != all(excusable(t) for t in c)]
print(out[:5])
")"; fi
# The diagnostic must name a cause that is TRUE of every path to an empty keyword
# set, not just the stop-word path — a user who typed '왜?' used no function word and
# must not be told to stop using them. One case per cause class, plus mixed ones.
#
# #583 changed WHICH questions land here, so the classes are re-measured rather than
# assumed. Lowering the floor to 2 moves questions OUT of this set (any question with a
# 2-char content token now has a keyword) and moves none in — but it makes the English
# function-word class reachable on the default path for the first time, so 'of in to'
# is now a cause class of its own rather than a recovery-stage detail.
for probe in '이것은 무엇인가|all function words (Korean)' \
             'of in to we|all function words (2-char English)' \
             '왜?|single-character CJK only' \
             'a b c|single-character ASCII only' \
             '이 논문은?|mixed: 1-char + function word' \
             'of 이것은 a|mixed: English function word + CJK stop word + 1-char'; do
  q="${probe%%|*}"; cls="${probe#*|}"
  ans="$(router wiki "$q" --reason 'unknown entity')"
  if ! printf '%s' "$ans" | grep -qF 'no searchable keyword'; then
    bad "no-keyword diagnostic missing for [$cls]"
  elif printf '%s' "$ans" | grep -qF '(no matching source excerpts found)'; then
    bad "[$cls] was reported as 'no such source'"
  elif printf '%s' "$ans" | grep -qF 'single-character tokens'; then
    ok "no-keyword diagnostic names a cause true of [$cls]"
  else
    bad "[$cls] diagnostic names only the stop-word cause"
  fi
done
noterm_diag="$(router search '이 논문은?' | "$PYTHON" -c "import json,sys; print(bool(json.load(sys.stdin)['diagnostic']))")"
nomatch_diag="$(router search 'quantumentanglementxyz' | "$PYTHON" -c "import json,sys; print(bool(json.load(sys.stdin)['diagnostic']))")"
if [ "$noterm_diag" = "True" ] && [ "$nomatch_diag" = "False" ]; then ok "search JSON carries the diagnostic only when there was no query term"; else bad "search JSON diagnostic wrong (no-term=$noterm_diag unmatched=$nomatch_diag)"; fi

# Membership is PINNED, not just sampled. A table-driven loop over the constant
# proves every listed form is droppable, but it cannot notice a form that was
# DELETED — the loop simply stops testing it. So the expected set is written out
# here: adding or removing any entry fails with the difference named.
if "$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
want = {
    '거기', '거기서', '그거', '그것', '그것은', '그것이', '그런', '그렇게', '논문은', '논문을',
    '논문이', '논문인가', '누가', '누구', '누구인가', '누구인가요', '맞나', '맞는가', '무엇',
    '무엇에', '무엇을', '무엇이', '무엇인가', '무엇인가요', '무엇인지', '뭐가', '뭐야', '뭔가',
    '어느', '어디', '어디까지', '어디서', '어디에', '어디에서', '어디인가', '어떠한', '어떤',
    '어떻게', '언제', '언제까지', '언제부터', '언제인가', '얼마나', '없나', '없나요', '없는가',
    '여기', '여기서', '이거', '이것', '이것은', '이것이', '이런', '이렇게', '있나', '있나요',
    '있는가', '있는지', '저거', '저것', '저것은', '저것이', '저기', '저기서', '저런', '저렇게',
}
got = set(a._CJK_QUESTION_STOPWORDS)
assert got == want, ('added', sorted(got - want), 'removed', sorted(want - got))
# 이/그/저 계열은 대칭이어야 한다. 한 계열에서만 형태가 빠지면 그 지시어를 쓴 질문에서만
# 조용히 필터가 새고, 목록을 훑어보는 것으로는 알아채기 어렵다.
for suffix in ('', '이', '은'):
    assert {b + suffix for b in ('이것', '그것', '저것')} <= got, suffix
for suffix in ('', '서'):
    assert {b + suffix for b in ('여기', '거기', '저기')} <= got, suffix
" 2>/dev/null; then ok "stop-word list membership pinned (66 forms, 이/그/저 대칭)"; else bad "stop-word list membership moved: $("$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
print(sorted(a._CJK_QUESTION_STOPWORDS))
")"; fi
# ...and every pinned form must actually be dropped, be a CJK 어절 of len>=2 (a
# 1-char entry would be filtered by the length floor anyway, promising a guarantee
# this list does not give), and never be a bare content stem (#571 기준 4).
if "$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
broken = []
for w in sorted(a._CJK_QUESTION_STOPWORDS):
    if len(w) < 2 or not a._is_cjk(w):
        broken.append(('not a CJK 어절 of len>=2', w))
        continue
    got = [p.pattern for p in a._keyword_patterns('신경기호 ' + w)]
    if got != ['신경기호']:
        broken.append(('survived', w, got))
broken += [('bare stem listed', s) for s in ('논문', '방법') if s in a._CJK_QUESTION_STOPWORDS]
assert not broken, broken
" 2>/dev/null; then ok "every listed form is dropped, none is a bare stem (#571)"; else bad "stop-word list contract broken: $("$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
print([(w, [p.pattern for p in a._keyword_patterns('신경기호 ' + w)]) for w in sorted(a._CJK_QUESTION_STOPWORDS)
       if [p.pattern for p in a._keyword_patterns('신경기호 ' + w)] != ['신경기호']])
")"; fi
# The English list is pinned the same way, and every entry must be EXACTLY 2 chars.
# The invariant is unchanged; its MEANING was reversed by #583. Under the old floor of
# 3 the length was what made the list harmless on the default path — nothing 2 chars
# long got that far, so the list only constrained the recovery stage. With the floor at
# 2 the list is LOAD-BEARING on the default path: it is the only thing keeping 'of'
# (prose reach 124 of the 127 searchable files) out of an ordinary question, and the
# drop loop below now exercises that path rather than a fallback.
#
# The invariant still earns its place, for the other reason: at exactly 2 characters
# the list can only remove tokens the floor itself just admitted. A 3-char entry would
# reach past the floor and silently filter a word no measurement here covers.
if "$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
want = {'am', 'an', 'as', 'at', 'be', 'by', 'do', 'he', 'if', 'in', 'is', 'it',
        'me', 'my', 'no', 'of', 'on', 'or', 'so', 'to', 'up', 'us', 'we'}
got = set(a._ASCII_FUNCTION_WORDS)
assert got == want, ('added', sorted(got - want), 'removed', sorted(want - got))
assert all(len(w) == 2 for w in got), sorted(w for w in got if len(w) != 2)
for w in sorted(got):
    kw = [p.pattern for p in a._keyword_patterns('논문은 어디에 ' + w)]
    assert kw == [], (w, kw)
" 2>/dev/null; then ok "2-char English function words pinned (23 forms, all len 2, all dropped)"; else bad "English function-word list moved: $("$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
print(sorted(a._ASCII_FUNCTION_WORDS))
")"; fi

# Ordering contract for #581: a filtered 어절 must leave NOTHING behind — not the
# token, and not a stem derived from it. Checked by consequence rather than by
# inspecting the code: no pattern produced for '논문은 신경기호' may match the
# retraction notice's prose, which contains '논문은' and '논문' but neither content
# word. A stripper added ABOVE the stop-word guard would emit '논문' and match it.
if "$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
pats = a._keyword_patterns('논문은 신경기호')
assert [p.pattern for p in pats] == ['신경기호'], [p.pattern for p in pats]
prose = '이 논문은 저자 요청으로 철회되었다. 논문 3편이 함께 철회됐다.'
assert not [p.pattern for p in pats if p.search(prose)], [p.pattern for p in pats if p.search(prose)]
" 2>/dev/null; then ok "a filtered 어절 leaves no pattern behind, derived or literal (#581 ordering contract)"; else bad "a filtered 어절 still reaches the text: $("$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
print([p.pattern for p in a._keyword_patterns('논문은 신경기호')])
")"; fi
rm -f "$KB/sources/571-retraction.md" "$KB/sources/571-topic.md" "$KB/sources/571-allstop.md"

# --- #575: the wiki block reports keyword match record and low recall ---------
# The defect: an answer whose keywords mostly missed the corpus is shaped exactly
# like an answer to a question the KB genuinely cannot support. The reader concludes
# "no evidence here" from what is really "the search did not cover the question".
#
# Every case below is a WHOLE-LINE check on the rendered block, so a diagnostic that
# regresses into the question echo or into another notice's line fails here.
mkdir -p "$KB/sources/575"
printf '# Neurosymbolic Grounding\n\nThis paper studies neurosymbolic grounding of retrieval evidence.\n' \
  > "$KB/sources/575/topic.md"

# rc_line <question> <line prefix> : the matching rendered line(s), '' if none
rc_line() { router wiki "$1" --reason 'unknown entity' | grep -F "$2" | grep -v '^question:' || true; }
# same <desc> <expected> <got> : whole-value equality, prints the difference on failure
same() { if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 — expected [$2], got [$3]"; fi; }

# 정례 — every keyword reached the corpus: the record is shown, and nothing is
# reported as unmatched or low.
full="$(router wiki 'neurosymbolic grounding' --reason 'unknown entity')"
same "#575 full recall reports the match record" \
  "keywords matched: 2/2 — neurosymbolic, grounding" \
  "$(printf '%s\n' "$full" | grep '^keywords matched:' || true)"
if printf '%s\n' "$full" | grep -q '^keywords unmatched:'; then bad "#575 full recall printed an unmatched line"; else ok "#575 full recall prints no unmatched line"; fi
if printf '%s\n' "$full" | grep -qF 'NOTE: low keyword recall'; then bad "#575 full recall warned about low recall"; else ok "#575 full recall does not warn"; fi

# 반례(경계) — exactly half is NOT low. The threshold is "fewer than half", and a
# question whose one real term did land must not be told its search failed.
half="$(router wiki 'neurosymbolic zzqqxxnotinkb' --reason 'unknown entity')"
same "#575 half recall names the unmatched keyword" \
  "keywords unmatched: zzqqxxnotinkb" \
  "$(printf '%s\n' "$half" | grep '^keywords unmatched:' || true)"
if printf '%s\n' "$half" | grep -qF 'NOTE: low keyword recall'; then bad "#575 exactly-half recall warned (threshold is strictly below half)"; else ok "#575 exactly-half recall does not warn (boundary)"; fi

# 정례 — below half: the warning appears, on its own line, and does not replace or
# absorb the standing warning it sits beside (수용 기준 4).
low="$(router wiki 'neurosymbolic zzqqxxnotinkb yywwvvnotinkb' --reason 'unknown entity')"
same "#575 low recall reports the ratio" "keywords matched: 1/3 — neurosymbolic" \
  "$(printf '%s\n' "$low" | grep '^keywords matched:' || true)"
if printf '%s\n' "$low" | grep -qF 'NOTE: low keyword recall'; then ok "#575 below-half recall warns"; else bad "#575 below-half recall did not warn"; fi
if printf '%s\n' "$low" | grep -qxF 'WARNING: unverified candidates — do not treat as confirmed facts.'; then ok "#575 the unverified WARNING keeps its own whole line"; else bad "#575 the low-recall note displaced the unverified WARNING"; fi
# `|| true`: `grep -c` exits 1 on a count of 0, and with `set -e` a failing pipeline
# inside an ASSIGNMENT kills the whole harness — every check after this line would
# vanish instead of reporting. Measured: without it, a mutant that removes the
# warning aborted the run at this line and silenced 90+ later checks.
nline="$(printf '%s\n' "$low" | grep -cF 'NOTE: low keyword recall' || true)"
if [ "$nline" = "1" ]; then ok "#575 the low-recall warning is exactly one line"; else bad "#575 low-recall warning spans $nline lines"; fi
# ...and it must not read as an evidence claim: the wording says the SEARCH missed.
if printf '%s\n' "$low" | grep -qF "This is NOT 'no such source'"; then ok "#575 the warning distinguishes itself from 'no evidence'"; else bad "#575 the warning can be read as 'no evidence'"; fi

# 반례 — zero matches: the block structure survives (수용 기준 5) and both the
# 0-count record and the existing empty-corpus wording are present.
zero="$(router wiki 'zzqqxxnotinkb yywwvvnotinkb' --reason 'unknown entity')"
same "#575 zero matches still reports a record" "keywords matched: 0/2" \
  "$(printf '%s\n' "$zero" | grep '^keywords matched:' || true)"
if printf '%s\n' "$zero" | grep -qF '(no matching source excerpts found)'; then ok "#575 zero matches keeps the empty-corpus wording"; else bad "#575 zero matches lost the empty-corpus wording"; fi
if printf '%s\n' "$zero" | head -1 | grep -qF 'UNVERIFIED — wiki exploration'; then ok "#575 zero matches keeps the block marker first"; else bad "#575 zero matches broke the block structure"; fi

# EXCLUSIVITY with #571. Three classes, measured one by one: a question with no
# keyword at all, a question with keywords that mostly miss, and a normal one. The
# two diagnostics answer different questions and must never stand in for each other
# — a ratio over an empty keyword set ('0/0') would tell the reader a search ran.
for probe in 'no-keyword|이 논문은?|noterm' \
             'low-recall|neurosymbolic zzqqxxnotinkb yywwvvnotinkb|lowrecall' \
             'normal|neurosymbolic grounding|neither'; do
  cls="${probe%%|*}"; rest="${probe#*|}"; q="${rest%|*}"; want="${rest##*|}"
  ans="$(router wiki "$q" --reason 'unknown entity')"
  has_noterm=no; has_low=no; has_record=no
  printf '%s\n' "$ans" | grep -qF 'no searchable keyword' && has_noterm=yes
  printf '%s\n' "$ans" | grep -qF 'NOTE: low keyword recall' && has_low=yes
  printf '%s\n' "$ans" | grep -q '^keywords matched:' && has_record=yes
  case "$want" in
    noterm)    [ "$has_noterm" = yes ] && [ "$has_low" = no ] && [ "$has_record" = no ] && r=ok || r=no ;;
    lowrecall) [ "$has_noterm" = no ] && [ "$has_low" = yes ] && [ "$has_record" = yes ] && r=ok || r=no ;;
    neither)   [ "$has_noterm" = no ] && [ "$has_low" = no ] && [ "$has_record" = yes ] && r=ok || r=no ;;
  esac
  if [ "$r" = ok ]; then
    ok "#575/#571 [$cls] gets exactly its own diagnostic"
  else
    bad "#575/#571 [$cls] diagnostics overlap (noterm=$has_noterm low=$has_low record=$has_record)"
  fi
done

# The tally's VANTAGE POINT — this is the whole correctness of the feature, and the
# two ways of getting it wrong are both silent, so both are pinned by consequence.
#
# (a) counted after the render cap. A keyword whose only excerpt ranks below the cap
#     is present in the corpus but absent from the shown rows; counting there invents
#     a low-recall warning for a search that did reach the corpus — the exact false
#     alarm this diagnostic exists to prevent. Measured on the reference KB, the
#     issue's own question is 1/8 counted after the cap against 2/8 in the corpus.
for i in 1 2 3 4 5 6 7 8 9 10 11 12; do
  printf 'capalpha capalpha capalpha capalpha capalpha filler %s\n' "$i" > "$KB/sources/575/filler-$i.md"
done
printf 'capbeta appears once, in a weakly ranked file.\n' > "$KB/sources/575/beta.md"
capped="$(router wiki 'capalpha capbeta' --reason 'unknown entity')"
if printf '%s\n' "$capped" | grep -qF 'sources/575/beta.md'; then
  bad "#575 the cap fixture is vacuous — beta.md is inside the rendered cap"
else
  ok "#575 cap fixture: the capbeta excerpt ranks below the render cap"
fi
same "#575 the tally is taken before the render cap, not after" \
  "keywords matched: 2/2 — capalpha, capbeta" \
  "$(printf '%s\n' "$capped" | grep '^keywords matched:' || true)"
rm -f "$KB"/sources/575/filler-*.md "$KB/sources/575/beta.md"

# (b) counted from the returned excerpts even uncapped. search() collapses a window
#     that overlaps the previously emitted one, so a keyword confined to a suppressed
#     line rides in NO excerpt at all — reported unmatched while the corpus holds it.
#     Fixture: line 3 anchors an excerpt covering lines 1-6; line 7's window starts at
#     line 4 and is therefore collapsed away.
printf 'pad\npad\nanchorterm here\npad\npad\npad\nsuppressedterm here\npad\n' > "$KB/sources/575/window.md"
if router search 'anchorterm suppressedterm' --all | grep -qF 'suppressedterm'; then
  bad "#575 window fixture is vacuous — suppressedterm does ride in an excerpt"
else
  ok "#575 window fixture: suppressedterm rides in no excerpt (last_end collapse)"
fi
same "#575 a keyword only on a collapsed line is still reported matched" \
  "keywords matched: 2/2 — anchorterm, suppressedterm" \
  "$(rc_line 'anchorterm suppressedterm' 'keywords matched:')"
rm -f "$KB/sources/575/window.md"

# (c) the DENOMINATOR is the searchable corpus, not the directory tree. A term that
#     occurs only in a sync-ignored source is reported unmatched: search() skips
#     those files, so no excerpt can ever cite them, and calling the term matched
#     would promise evidence that cannot be produced — and would suppress the
#     low-recall warning that is the honest answer. Not hypothetical: on the
#     reference KB 'augmented' occurs in 5 files, every one of them sync-ignored.
mkdir -p "$KB/sources/575/hidden"
printf 'ignoredonlyterm appears only in a sync-ignored source.\n' > "$KB/sources/575/hidden/x.md"
printf -- '- 575/hidden/**\n' >> "$KB/policy/sync-ignore.md"
same "#575 a term only in a sync-ignored source is reported unmatched" \
  "keywords unmatched: ignoredonlyterm" \
  "$(rc_line 'neurosymbolic ignoredonlyterm' 'keywords unmatched:')"
# ...and the SAME term is matched once the ignore is lifted, so the check above
# pins the sync-ignore rule rather than a misspelled fixture.
sed -i.bak '/^- 575\/hidden\/\*\*$/d' "$KB/policy/sync-ignore.md" && rm -f "$KB/policy/sync-ignore.md.bak"
same "#575 the same term is matched once the sync-ignore is lifted" \
  "keywords matched: 2/2 — neurosymbolic, ignoredonlyterm" \
  "$(rc_line 'neurosymbolic ignoredonlyterm' 'keywords matched:')"
rm -rf "$KB/sources/575/hidden"

# PURITY (수용 기준 6): the tally is a side report. Same rows, same order, with and
# without it — and the `search` JSON contract gains no field (its shape is pinned by
# the #279/#571 cases above; this states the intent that #575 stays out of it).
for i in 1 2 3 4 5; do
  printf 'purealpha and purebeta appear here, file %s\n' "$i" > "$KB/sources/575/pure-$i.md"
done
if "$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
from pathlib import Path
import ask_router as a
root = Path('$KB')
q = 'purealpha purebeta'
plain = a.search(q, root, limit=None)
tallied = a.search(q, root, limit=None, recall={})
# The comparison is only meaningful over several rows: with one row it cannot tell
# a truncation from an identity, and a mutant that truncated the result to [:1]
# passed this check while the fixture yielded a single row. Assert the fixture's
# own size first, so it can never go vacuous again without saying so.
assert len(plain) >= 5, ('fixture yields too few rows to compare', len(plain))
assert plain == tallied, (len(plain), len(tallied))
# ...and the same for the capped path, where a truncation is likeliest to hide.
assert a.search(q, root) == a.search(q, root, recall={}), 'capped rows differ'
" 2>/dev/null; then ok "#575 collecting the tally does not change the returned rows"; else bad "#575 the tally changed search() results"; fi
rm -f "$KB"/sources/575/pure-*.md
if router search 'neurosymbolic grounding' | "$PYTHON" -c "
import json, sys
sys.exit(0 if sorted(json.load(sys.stdin)) == ['diagnostic', 'results', 'total', 'truncated'] else 1)
"; then ok "#575 the search JSON contract is unchanged (no recall field)"; else bad "#575 the search JSON gained a field"; fi
# --all renders the same record: the diagnostic describes the question, not the cap.
same "#575 --all reports the same tally as the capped render" \
  "keywords matched: 2/2 — neurosymbolic, grounding" \
  "$(router wiki 'neurosymbolic grounding' --reason 'unknown entity' --all | grep '^keywords matched:' || true)"
rm -rf "$KB/sources/575"

# --- Phase 2: path (positive render / variable) + policy + decisions ---
# Path render/evaluate AND policy-predicate evaluation all go through run_wirelog,
# which needs pyrewire. Guard the whole block so the no-dependency CI shell-harness
# job skips these rather than failing: without the engine a path query returns
# nothing, which is a SKIP here, not a bug. (The two path assertions used to sit
# outside this guard, so that CI job — which installs no pyrewire — failed on
# "path positive not rendered" and "path ... enumerates reachable pairs".)
if "$PYTHON" -c "import pyrewire; raise SystemExit(0 if tuple(int(x) for x in pyrewire.__version__.split('.')[:3]) >= (1,0,1) else 1)" >/dev/null 2>&1; then
  ppos="$(router render 'path("Acme API", "FastAPI")?')"
  if printf '%s' "$ppos" | grep -qF "VERIFIED — engine" && printf '%s' "$ppos" | grep -qF "Acme API, FastAPI"; then ok "path positive renders the dependency path as an engine answer"; else bad "path positive not rendered"; fi
  # #303: a path verified-negative is proven by the engine's own empty result, so its
  # render needs pyrewire (evaluate -> run_wirelog). With the engine present it renders
  # the VERIFIED — engine verified-negative block; without it, cmd_render degrades to a
  # wiki directive, which is why this assertion lives inside the guard.
  pneg="$(router render 'path("Postgres", "FastAPI")?')"
  if printf '%s' "$pneg" | grep -qF "VERIFIED — engine" && printf '%s' "$pneg" | grep -qF "verified negative"; then ok "path verified-negative renders as an engine answer (not deferred/wiki)"; else bad "path verified-negative not rendered as engine answer"; fi
  check_field "path with a variable enumerates reachable pairs" evaluate 'path("Acme API", T)?' count 2

  # policy-predicate evaluation needs pyrewire (run_wirelog). Compile a tiny policy first.
  printf '# policy\n## Rules\n- [usage_chain] 어떤 항목이 `uses` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB/policy/logic-policy.md"
  ( cd "$PLUGIN_ROOT" && FACTLOG_ROOT="$KB" "$PYTHON" tools/generate_logic_policy.py >/dev/null 2>&1 )
  check_field "policy predicate routes engine" validate 'requires_review(E, R)?' route engine
  pc="$(router evaluate 'requires_review(E, R)?' | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['count'])")"
  if [ "$pc" -ge 1 ]; then ok "policy predicate evaluates to engine rows ($pc)"; else bad "policy predicate returned no rows"; fi

  # #152 regression: a user-authored predicate in logic-policy.extra.dl must be
  # askable. ask_router previously read only the generated logic-policy.dl and
  # ignored extra.dl, so such predicates wrongly classified unknown_predicate->wiki.
  printf '.decl uses_fastapi(entity: symbol, reason: symbol)\nuses_fastapi(S, "uses_fastapi") :- relation(S, "uses", "FastAPI").\n' > "$KB/policy/logic-policy.extra.dl"
  check_field "extra.dl predicate routes engine (#152)" validate 'uses_fastapi(E, R)?' route engine
  ec="$(router evaluate 'uses_fastapi(E, R)?' | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['count'])")"
  if [ "$ec" -ge 1 ]; then ok "extra.dl predicate evaluates to engine rows ($ec)"; else bad "extra.dl predicate returned no rows"; fi
  rm -f "$KB/policy/logic-policy.extra.dl"

  rm -f "$KB/policy/logic-policy.dl" "$KB/policy/logic-policy.md"

  # #198 engine-eval guard: a hand-authored TYPED-COMPARISON predicate (after2030,
  # #120/#152 pattern) living ONLY in logic-policy.extra.dl — with logic-policy.dl
  # ABSENT — must be evaluated by the engine (run_wirelog) via ask, matching check.
  # Self-contained KB with a launch_date typed relation projected from candidates,
  # NO compiled logic-policy.dl, and the comparison rule in extra.dl. Pre-#198,
  # _policy_program_optional short-circuited to '' when the .dl was absent, so this
  # predicate was unknown -> wiki and never evaluated (the silent-ignore bug).
  XKB="$(mktemp -d)/wiki"
  "$PYTHON" -m factlog init --target "$XKB" >/dev/null
  printf 'x\n' > "$XKB/sources/a.md"
  printf '%s\n%s\n' \
    'subject,relation,object,source,status,confidence,note' \
    '을서비스,정식_운영,"date(2030,1)",sources/a.md,accepted,0.9,' > "$XKB/facts/candidates.csv"
  printf -- '- `정식_운영` : date as launch_date\n' > "$XKB/policy/typed-relations.md"
  ( cd "$PLUGIN_ROOT" && FACTLOG_ROOT="$XKB" "$PYTHON" tools/compile_facts.py >/dev/null 2>&1 )
  printf '%s\n%s\n' \
    '.decl after2030(entity: symbol, reason: symbol)' \
    'after2030(S, "launch_after_2030") :- launch_date(S, D), D >= 20300101.' \
    > "$XKB/policy/logic-policy.extra.dl"
  # logic-policy.dl intentionally absent here — this is the #198 case.
  [ -f "$XKB/policy/logic-policy.dl" ] && bad "#198 guard setup: logic-policy.dl must be absent" || ok "#198 guard: logic-policy.dl absent (extra.dl carries the only policy)"
  xrouter() { "$PYTHON" "$ROUTER" "$@" --target "$XKB"; }
  check_field_x() {  # like check_field but targets XKB
    local desc="$1" sub="$2" draft="$3" key="$4" expected="$5"
    local got; got="$(xrouter "$sub" "$draft" | field "$key")"
    if [ "$got" = "$expected" ]; then ok "$desc ($key=$got)"; else bad "$desc — expected $key=$expected, got $got"; fi
  }
  check_field_x "#198: extra.dl-only comparison predicate routes engine (.dl absent)" validate 'after2030(E, R)?' route engine
  xc="$(xrouter evaluate 'after2030(E, R)?' | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['count'])")"
  if [ "$xc" -ge 1 ]; then ok "#198: extra.dl-only comparison predicate EVALUATES to engine rows with .dl absent ($xc)"; else bad "#198: extra.dl-only comparison predicate returned no rows — silently ignored (parity with check broken)"; fi
  if xrouter render 'after2030(E, R)?' | grep -qF "VERIFIED — engine"; then ok "#198: extra.dl-only comparison predicate renders a VERIFIED — engine answer"; else bad "#198: extra.dl-only comparison predicate not rendered as engine answer"; fi

  # #198 GRACEFUL (engine-eval stage): a PRESENT-but-broken extra.dl carries the
  # only policy (.dl absent) and now routes to engine (validate rc=0) — but engine
  # evaluation (run_wirelog: re-loads policy + runs pyrewire) can fail loud in ways
  # the routing-time loader guard never sees. ask must NOT hard-fail or crash
  # (#193): render/evaluate must exit rc=0, emit no traceback, and surface a
  # "policy is unevaluable" warning instead of faking a verified negative. Both
  # cases below crash/rc!=0 on code that only guards _policy_program_optional.

  # (b) TYPE VIOLATION: a `number` alias compared against an UNSCALED float
  # (V >= 2.0). run_wirelog's _assert_no_unscaled_number_threshold raises a
  # FactlogError before the engine runs (#125 scaled-×1000 contract).
  BKB="$(mktemp -d)/wiki"
  "$PYTHON" -m factlog init --target "$BKB" >/dev/null
  printf 'x\n' > "$BKB/sources/a.md"
  printf '%s\n%s\n' 'subject,relation,object,source,status,confidence,note' '앱,버전,"number(1.5)",sources/a.md,accepted,0.9,' > "$BKB/facts/candidates.csv"
  printf -- '- `버전` : number as version_num\n' > "$BKB/policy/typed-relations.md"
  ( cd "$PLUGIN_ROOT" && FACTLOG_ROOT="$BKB" "$PYTHON" tools/compile_facts.py >/dev/null 2>&1 )
  printf '%s\n%s\n' '.decl after2(entity: symbol, reason: symbol)' 'after2(S, "ge2") :- version_num(S, V), V >= 2.0.' > "$BKB/policy/logic-policy.extra.dl"
  brouter() { "$PYTHON" "$ROUTER" "$@" --target "$BKB"; }
  if brouter validate 'after2(E, R)?' >/dev/null 2>&1; then ok "#198 (b) type-violation extra.dl — validate stays rc=0"; else bad "#198 (b) validate hard-failed on a type-violating extra.dl"; fi
  if brouter render 'after2(E, R)?' >/dev/null 2>&1; then ok "#198 (b) type-violation extra.dl — render stays rc=0 (no crash)"; else bad "#198 (b) render hard-failed/crashed on a type-violating extra.dl (engine-eval guard missing)"; fi
  if brouter render 'after2(E, R)?' 2>/dev/null | grep -qF "policy is unevaluable"; then ok "#198 (b) render surfaces the 'policy is unevaluable' warning"; else bad "#198 (b) render did not warn that the policy was unevaluable"; fi
  if brouter render 'after2(E, R)?' 2>/dev/null | grep -qF "verified negative"; then bad "#198 (b) render faked a verified negative for an unevaluable policy"; else ok "#198 (b) render does not fake a verified negative"; fi
  if brouter evaluate 'after2(E, R)?' >/dev/null 2>&1; then ok "#198 (b) type-violation extra.dl — evaluate stays rc=0"; else bad "#198 (b) evaluate hard-failed on a type-violating extra.dl"; fi

  # (c) BROKEN SYNTAX: a rule body pyrewire cannot parse. run_wirelog's
  # EasySession raises a pyrewire ParseError — NOT a FactlogError, so run_cli
  # would not catch it and ask would crash with an uncaught traceback unless the
  # engine-eval guard catches broad engine exceptions.
  CKB="$(mktemp -d)/wiki"
  "$PYTHON" -m factlog init --target "$CKB" >/dev/null
  printf 'x\n' > "$CKB/sources/a.md"
  printf '// t\nrelation("Acme API", "uses", "FastAPI").\n' > "$CKB/facts/accepted.dl"
  printf '%s\n%s\n' '.decl brokenp(entity: symbol, reason: symbol)' 'brokenp(S, "x") :- relation(S, "uses", "FastAPI") @@@garbage.' > "$CKB/policy/logic-policy.extra.dl"
  ckrouter() { "$PYTHON" "$ROUTER" "$@" --target "$CKB"; }
  if ckrouter validate 'brokenp(E, R)?' >/dev/null 2>&1; then ok "#198 (c) syntax-broken extra.dl — validate stays rc=0"; else bad "#198 (c) validate hard-failed on a syntax-broken extra.dl"; fi
  if ckrouter render 'brokenp(E, R)?' >/dev/null 2>&1; then ok "#198 (c) syntax-broken extra.dl — render stays rc=0 (no uncaught pyrewire traceback)"; else bad "#198 (c) render crashed on a pyrewire ParseError (non-FactlogError not caught)"; fi
  if ckrouter render 'brokenp(E, R)?' 2>/dev/null | grep -qF "policy is unevaluable"; then ok "#198 (c) render surfaces the 'policy is unevaluable' warning"; else bad "#198 (c) render did not warn on the parse error"; fi
  if ckrouter evaluate 'brokenp(E, R)?' >/dev/null 2>&1; then ok "#198 (c) syntax-broken extra.dl — evaluate stays rc=0"; else bad "#198 (c) evaluate crashed on a pyrewire ParseError"; fi
else
  echo "SKIP: pyrewire unavailable — skipping policy-predicate evaluation assertions"
fi

# decisions/ is searched as clearly-labeled SUPPLEMENTARY context
printf '# Open Questions\n\n## review\n- needs_review widgetterm pending\n' > "$KB/decisions/open-questions.md"
dout="$(router search "widgetterm")"
if printf '%s' "$dout" | grep -qF 'decisions (supplementary)'; then ok "decisions/ searched as labeled supplementary"; else bad "decisions/ supplementary not surfaced"; fi
rm -f "$KB/decisions/open-questions.md"

# --- #572: directory grade is the TOP sort key -------------------------------
# The label alone used to be the only place the grade existed, so a decisions/
# excerpt outranked the source text it reviews. These cases pin the ordering.
printf '# notes\n\nwidgetterm widgetterm widgetterm gadgetterm.\n' > "$KB/decisions/open-questions.md"
printf '# paper\n\nwidgetterm appears once.\n' > "$KB/sources/grade-primary.md"
# (a) the load-bearing case: supplementary wins on BOTH score components
#     (coverage 2 vs 1, frequency 4 vs 1) and still ranks below primary. A tie-only
#     case cannot prove this — ties already fell primary-first from corpus order.
g_dirs="$(router search "widgetterm gadgetterm" | "$PYTHON" -c "
import json, sys
print(','.join(r['dir'] for r in json.load(sys.stdin)['results']))")"
if [ "$g_dirs" = "sources,decisions (supplementary)" ]; then
  ok "#572 등급이 최상위 정렬 키다 — 점수가 더 높은 supplementary 가 primary 뒤로 간다"
else
  bad "#572 등급 정렬이 적용되지 않았다 (got [$g_dirs])"
fi
# (b) the acceptance-criterion case as written: at an exact score TIE primary wins.
#     Measured to have NO unique kill — every mutant it catches, (a) catches too.
#     The reason is structural, not fixture luck: once the grade is in the key AT ALL,
#     a primary/supplementary pair is never a full-key tie, so this case cannot tell
#     "grade is the TOP key" from "grade is anywhere in the key", and a mutation that
#     only reorders full-key ties cannot reach this pair either. Kept because the
#     acceptance criterion asks for it literally; (a) carries the contract and (e)
#     below covers the tie-order half.
printf '# paper\n\nwidgetterm appears once.\n' > "$KB/sources/grade-tie.md"
printf '# notes\n\nwidgetterm appears once.\n' > "$KB/decisions/tie-notes.md"
rm -f "$KB/decisions/open-questions.md" "$KB/sources/grade-primary.md"
t_dirs="$(router search "widgetterm" | "$PYTHON" -c "
import json, sys
print(','.join(r['dir'] for r in json.load(sys.stdin)['results']))")"
if [ "$t_dirs" = "sources,decisions (supplementary)" ]; then
  ok "#572 동점일 때 primary 가 supplementary 를 이긴다"
else
  bad "#572 동점 순서가 뒤집혔다 (got [$t_dirs])"
fi
# (c) the grade is DERIVED from WIKI_SUPPLEMENTARY_DIRS, not a second hardcoded
#     list: moving a dir between the two constants moves its grade with it.
if "$PYTHON" -c "
import sys
sys.path.insert(0, '$PLUGIN_ROOT/tools')
import ask_router as a
base = {rel: grade for rel, _label, grade in a._wiki_corpus()}
assert base['sources'] > base['decisions'], base
a.WIKI_SUPPLEMENTARY_DIRS = ('decisions', 'reviews')
moved = {rel: grade for rel, _label, grade in a._wiki_corpus()}
assert moved['reviews'] == moved['decisions'] < moved['sources'], moved
a.WIKI_SOURCE_DIRS = ('sources', 'runs/sources', 'reviews')
a.WIKI_SUPPLEMENTARY_DIRS = ('decisions',)
promoted = {rel: grade for rel, _label, grade in a._wiki_corpus()}
assert promoted['reviews'] == promoted['sources'] > promoted['decisions'], promoted
" 2>/dev/null; then
  ok "#572 등급은 WIKI_SUPPLEMENTARY_DIRS 에서 파생된다 (하드코딩 없음)"
else
  bad "#572 등급이 상수와 따로 논다 — 디렉터리를 옮겨도 등급이 따라오지 않는다"
fi
# (d) the documented rerank interaction: an opt-in neural backend is allowed to
#     override the grade. The stub reverses lexical order, so supplementary — last
#     under the grade key — becomes first. This is the choice recorded in
#     _semantic_rerank's docstring, so the guarantee in (a)/(b) is scoped to the
#     bundled lexical path (FACTLOG_EMBED_MODULE unset).
GEMB="$(mktemp -d)"; printf 'def rank(q, texts):\n    return [float(i) for i in range(len(texts))]\n' > "$GEMB/grade_stub.py"
r_dir="$(FACTLOG_EMBED_MODULE=grade_stub PYTHONPATH="$PLUGIN_ROOT:$GEMB" "$PYTHON" "$ROUTER" search "widgetterm" --target "$KB" | "$PYTHON" -c "
import json, sys
res = json.load(sys.stdin)['results']
print(res[0]['dir'] if res else '')")"
if [ "$r_dir" = "decisions (supplementary)" ]; then
  ok "#572 재랭크는 등급을 보존하지 않는다 — 백엔드가 supplementary 를 1위로 올릴 수 있다 (문서화된 선택)"
else
  bad "#572 재랭크 상호작용이 문서와 다르다 (got [$r_dir]) — _semantic_rerank 주석을 갱신하라"
fi
rm -rf "$GEMB"
rm -f "$KB/sources/grade-tie.md" "$KB/decisions/tie-notes.md"
# (e) the OTHER half of the sort statement #572 rewrote: ties keep corpus/line order
#     (stable sort). This needs a SAME-grade tie — (b)'s primary/supplementary pair is
#     split by the grade key and so is blind to tie reordering. Measured: a mutant that
#     sorts ascending and then reverses the list (flipping full-key ties while keeping
#     the descending key order) passes (a)-(d) and is caught only here and by
#     tests/test_ask_wiki_search.sh's PIN3. PIN3 alone is not enough cover — it is a
#     characterization pin that #574/#576/#581 are each expected to rewrite, and the
#     tie-order guarantee would then lose its last guard silently.
printf '# a\n\nwidgetterm appears once.\n' > "$KB/sources/aaa-tie.md"
printf '# z\n\nwidgetterm appears once.\n' > "$KB/sources/zzz-tie.md"
s_files="$(router search "widgetterm" | "$PYTHON" -c "
import json, sys
print(','.join(r['file'] for r in json.load(sys.stdin)['results']))")"
if [ "$s_files" = "sources/aaa-tie.md,sources/zzz-tie.md" ]; then
  ok "#572 완전 동점은 코퍼스/행 순서를 유지한다 (안정 정렬)"
else
  bad "#572 동점 안정성이 깨졌다 (got [$s_files])"
fi
rm -f "$KB/sources/aaa-tie.md" "$KB/sources/zzz-tie.md"

# --- #31: relevance ranking + optional embedding rerank seam ---
printf '# hi\n\n검색 문서 자료 항목 모두 포함.\n' > "$KB/sources/rank-hi.md"
printf '# lo\n\n검색 만 언급.\n' > "$KB/sources/rank-lo.md"
top="$(router search "검색 문서 자료 항목" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(d['results'][0]['file'] if d['results'] else '')")"
[ "$top" = "sources/rank-hi.md" ] && ok "relevance ranking surfaces highest-coverage excerpt first" || bad "ranking did not rank most-relevant first (got $top)"
# optional embedding backend (graceful degrade is exercised by every other search; here test the ACTIVE path)
# stub scores ascending by position, so the lexical-best (index 0) gets the
# LOWEST score and is pushed to the bottom — an unambiguous reorder (>=2 results).
EMB="$(mktemp -d)"; printf 'def rank(q, texts):\n    return [float(i) for i in range(len(texts))]\n' > "$EMB/embed_stub.py"
act0="$(FACTLOG_EMBED_MODULE=embed_stub PYTHONPATH="$PLUGIN_ROOT:$EMB" "$PYTHON" "$ROUTER" search "검색 문서 자료 항목" --target "$KB" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(d['results'][0]['file'] if d['results'] else '')")"
if [ -n "$act0" ] && [ "$act0" != "$top" ]; then ok "optional embedding backend reorders results (seam invoked, graceful when absent)"; else bad "embedding seam did not reorder (lex=$top act=$act0)"; fi
rm -f "$KB/sources/rank-hi.md" "$KB/sources/rank-lo.md"

# --- #589: with a backend ON, the lexical score still decides MEMBERSHIP --------
# The unset at the top of this file scopes every order pin above to the bundled
# lexical path. The half of the contract that does NOT go away when a backend is on
# is the cap: search() slices to `limit` BEFORE calling _semantic_rerank, so the
# backend only orders excerpts the lexical score already selected. That is reason (a)
# in _semantic_rerank's docstring, and no check held it. Measured against a mutant that
# reranks BEFORE the slice: tests/unit (6224 passed, 1 skipped) and
# tests/test_ask_wiki_search.sh (52 passed) stay green, and this harness reports
# exactly one failure — the membership check below. The mutant hides that well because
# the rerank is a no-op unless a backend is on, and the other cases that turn one on
# read only position 0 of a result set the cap never trimmed.
# 10 files tie at (coverage 1, frequency 2) and fill the default cap of 10; the 11th
# scores strictly lower (frequency 1) and is last in corpus order, so the reversing
# stub would put it FIRST if it were reranked before the slice.
CAPKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$CAPKB" >/dev/null
rm -f "$CAPKB"/sources/* "$CAPKB"/decisions/*
for i in 01 02 03 04 05 06 07 08 09 10; do
  printf '# c%s\n\nwidgetterm widgetterm here.\n' "$i" > "$CAPKB/sources/cap-$i.md"
done
printf '# low\n\nwidgetterm once.\n' > "$CAPKB/sources/cap-zz-low.md"
cap_files() { "$PYTHON" -c "
import json, sys
print(','.join(r['file'] for r in json.load(sys.stdin)['results']))"; }
CEMB="$(mktemp -d)"; printf 'def rank(q, texts):\n    return [float(i) for i in range(len(texts))]\n' > "$CEMB/cap_stub.py"
cap_lex="$("$PYTHON" "$ROUTER" search "widgetterm" --target "$CAPKB" | cap_files)"
cap_on="$(FACTLOG_EMBED_MODULE=cap_stub PYTHONPATH="$PLUGIN_ROOT:$CEMB" "$PYTHON" "$ROUTER" search "widgetterm" --target "$CAPKB" | cap_files)"
rm -rf "$CEMB"
# fixture guard: an empty or short capped set would make the membership check below
# pass for the wrong reason, so name that failure instead of inheriting it.
if [ "$cap_lex" = "sources/cap-01.md,sources/cap-02.md,sources/cap-03.md,sources/cap-04.md,sources/cap-05.md,sources/cap-06.md,sources/cap-07.md,sources/cap-08.md,sources/cap-09.md,sources/cap-10.md" ]; then
  ok "#589 캡 픽스처: 어휘 경로가 상위 10건을 채우고 최하위 발췌는 캡 밖이다"
else
  bad "#589 캡 픽스처가 깨졌다 — 어휘 결과가 예상과 다르다 (got [$cap_lex])"
fi
# and the backend must actually be running, or the membership check is vacuous.
if [ -n "$cap_on" ] && [ "$cap_on" != "$cap_lex" ]; then
  ok "#589 캡 케이스에서 재랭크 백엔드가 실제로 동작한다 (순서가 어휘 순서와 다르다)"
else
  bad "#589 캡 케이스에서 백엔드가 재정렬하지 않았다 (lex=[$cap_lex] on=[$cap_on]) — 아래 멤버십 체크는 무의미하다"
fi
case ",$cap_on," in
  *,sources/cap-zz-low.md,*)
    bad "#589 캡보다 재랭크가 먼저 돌았다 — 어휘 점수가 캡 밖으로 밀어낸 발췌를 백엔드가 끌어올렸다 (on=[$cap_on])" ;;
  *)
    ok "#589 백엔드가 켜져도 캡 멤버십은 어휘 점수가 정한다 (재랭크는 선택된 집합의 순서만 바꾼다)" ;;
esac
rm -rf "$(dirname "$CAPKB")"

# --- #573: cited file paths must not feed the keyword frequency score ---
# primary vs primary ON PURPOSE: both files live in sources/, so a directory-grade
# sort (#572) cannot separate them — only the path damping can. The real KB has no
# such case, so the fixture is synthetic.
printf '# notes\n\n- sources/graphdb-tuning-widgetterm.md\n- sources/graphdb-tuning-widgetterm.md (2절)\n- runs/sources/graphdb-tuning-widgetterm.txt\n' > "$KB/sources/pathcite-notes.md"
printf '# tuning\n\nThis paper measures widgetterm latency in a graph database.\n' > "$KB/sources/graphdb-tuning-widgetterm.md"
pc_top="$(router search "widgetterm" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); print(d['results'][0]['file'] if d['results'] else '')")"
if [ "$pc_top" = "sources/graphdb-tuning-widgetterm.md" ]; then ok "#573: prose about the keyword outranks a note that only cites its path"; else bad "#573: path citations still win the ranking (top=$pc_top)"; fi
# collection is untouched: the path-only note is demoted, not filtered out. (Under a
# result cap a (0,0) excerpt can still fall outside the cap — that is ranking, not a filter.)
if router search "widgetterm" | grep -qF 'sources/pathcite-notes.md'; then ok "#573: path-only note is still collected (score damped, not filtered out)"; else bad "#573: path-only note disappeared from results (damping must not filter)"; fi
# a path mentioned mid-sentence loses only its own token — searched, not just scored
printf '# mixed\n\nsources/graphdb-tuning-widgetterm.md 는 widgetterm 을 다룬다.\n' > "$KB/sources/pathcite-mixed.md"
pc_order="$(router search "widgetterm" | "$PYTHON" -c "
import json, sys
files = [r['file'] for r in json.load(sys.stdin)['results']]
mixed, notes = 'sources/pathcite-mixed.md', 'sources/pathcite-notes.md'
print('ok' if mixed in files and notes in files and files.index(mixed) < files.index(notes) else files)
")"
if [ "$pc_order" = "ok" ]; then ok "#573: prose keyword survives beside a path citation in the same sentence (ranks above the path-only note)"; else bad "#573: mid-sentence path masking swallowed the prose keyword (order=$pc_order)"; fi
# the damping boundaries, at score level: what must NOT be masked. Each line below
# fails if one of the guards is dropped — the lookbehind (a dir name ending another
# word), the extension requirement (a directory reference, no file), the root-relative
# restriction, and the prose baseline.
pc_edges="$("$PYTHON" -c "
import sys
sys.path.insert(0, '$PLUGIN_ROOT/tools')
import ask_router as a
pats = a._keyword_patterns('widgetterm')
score = lambda text: a._excerpt_score(text, pats)
runon = a._keyword_patterns('runs policy')
print(score('sources/graphdb-tuning-widgetterm.md 는 widgetterm 을 다룬다.'),  # mid-sentence path
      score('widgetterm widgetterm'),                                          # plain prose
      score('resources/x-widgetterm.md'),                                      # 'sources' ends another word
      score('sources/widgetterm/ 디렉터리'),                                    # a dir, not a file
      score('./sources/x-widgetterm.md'),                                      # not root-relative
      # scaffold dirs are ordinary English nouns: a missing space after a full stop
      # must not turn prose into a path (common in PDF->text under runs/sources/)
      a._excerpt_score('we performed 3 runs/day.The results were stable', runon),
      a._excerpt_score('the policy/value.Networks are trained jointly', runon),
      # a Korean particle glued to a citation belongs to the citation, not to prose
      a._excerpt_score('sources/kim-widgetterm.md에서', a._keyword_patterns('에서 widgetterm')),
      # scaffold dirs outside the searched corpus are damped too: decisions/open-questions.md
      # in the reference KB cites 154 pages/*.md paths (measured), and those are locators
      score('- stale_source: pages/widgetterm.md references a removed source'))
")"
if [ "$pc_edges" = "(1, 1) (1, 2) (1, 1) (1, 1) (1, 1) (1, 1) (1, 1) (0, 0) (0, 0)" ]; then ok "#573: damping is bounded — only a root-relative KB path with a source-text extension is masked"; else bad "#573: damping boundaries moved (got $pc_edges)"; fi
# Every axis of the damping, swept rather than sampled: dropping one directory or one
# extension from the constants must fail here. The extension list is DERIVED from
# ingest.INGEST_CONVERTERS + the KB's own file-type constants, so this sweep is also
# what catches an ingest format joining the repo without joining the damping.
if "$PYTHON" -c "
import sys
sys.path.insert(0, '$PLUGIN_ROOT/tools')
import ask_router as a
pats = a._keyword_patterns('widgetterm')
for rel in a._CITED_KB_DIRS:                      # every scaffold root is damped
    text = f'{rel}/x-widgetterm.md 를 봤다'
    assert a._excerpt_score(text, pats) == (0, 0), (rel, a._excerpt_score(text, pats))
for ext in a._CITATION_EXTS:                      # every citable extension is damped
    text = f'sources/x-widgetterm.{ext} 를 봤다'
    assert a._excerpt_score(text, pats) == (0, 0), (ext, a._excerpt_score(text, pats))
for ext in ('py', 'exe', 'png', 'zip'):           # non-source extensions are NOT damped
    text = f'sources/x-widgetterm.{ext} 를 봤다'
    assert a._excerpt_score(text, pats) == (1, 1), (ext, a._excerpt_score(text, pats))
# the two axes the reference KB cannot exercise: a decisions/ citation (none in the KB)
# and a runs/ data file (all real citations are .md/.csv)
assert a._excerpt_score('decisions/open-questions-widgetterm.md 참고', pats) == (0, 0)
assert a._excerpt_score('runs/2024-widgetterm.json 로그', pats) == (0, 0)
# The sweeps above iterate the constants themselves, so they cannot see an entry being
# DELETED. These membership checks can. INGEST_CONVERTERS is compared live, so a format
# added to ingest without joining the damping fails here rather than silently regressing
# a Korean-original KB (.hwp/.hwpx/.pptx/.odt were missed exactly that way).
from factlog import ingest
assert {ext.lstrip('.') for ext in ingest.INGEST_CONVERTERS} <= set(a._CITATION_EXTS), (
    sorted({ext.lstrip('.') for ext in ingest.INGEST_CONVERTERS} - set(a._CITATION_EXTS)))
assert {'csv', 'dl', 'json', 'md', 'txt', 'yaml', 'yml'} <= set(a._CITATION_EXTS), a._CITATION_EXTS
assert {'sources', 'runs/sources', 'decisions', 'pages', 'facts', 'policy', 'templates',
        'runs'} <= set(a._CITED_KB_DIRS), a._CITED_KB_DIRS
" 2>/dev/null; then ok "#573: damping covers every scaffold dir and every citable extension (derived from INGEST_CONVERTERS)"; else bad "#573: a directory or extension axis of the damping is uncovered — run the sweep by hand"; fi
# The compiled pattern must be byte-identical across processes: the dir/ext sets are
# built from set unions, whose iteration order follows PYTHONHASHSEED. Sorting is what
# makes it stable, and no functional assertion can catch its removal.
pc_pat_a="$(PYTHONHASHSEED=1 "$PYTHON" -c "
import sys; sys.path.insert(0, '$PLUGIN_ROOT/tools')
import ask_router as a; print(a._PATH_CITATION_RE.pattern)")"
pc_pat_b="$(PYTHONHASHSEED=987654321 "$PYTHON" -c "
import sys; sys.path.insert(0, '$PLUGIN_ROOT/tools')
import ask_router as a; print(a._PATH_CITATION_RE.pattern)")"
if [ -n "$pc_pat_a" ] && [ "$pc_pat_a" = "$pc_pat_b" ]; then ok "#573: the compiled path pattern is deterministic across PYTHONHASHSEED"; else bad "#573: path pattern varies by hash seed — a set is being iterated unsorted"; fi
rm -f "$KB/sources/pathcite-notes.md" "$KB/sources/graphdb-tuning-widgetterm.md" "$KB/sources/pathcite-mixed.md"

# --- #32: grounded answers (verified facts about mentioned entities) ---
gw="$(router wiki "tell me about Acme API")"
printf '%s' "$gw" | grep -qF "VERIFIED — engine (grounding" && ok "wiki answer includes a VERIFIED grounding block" || bad "no grounding block"
printf '%s' "$gw" | grep -qF "Acme API, uses, FastAPI" && ok "grounding lists accepted facts about the mentioned entity" || bad "grounding missing the accepted fact"
# grounding draws ONLY from accepted.dl: a candidate-only relation must not appear
printf 'subject,relation,object,source,status,confidence,note\nAcme API,may_use,Datadog,sources/x.md,candidate,0.4,\n' > "$KB/facts/candidates.csv"
if printf '%s' "$(router wiki "tell me about Acme API")" | grep -qF "may_use"; then bad "candidate-only relation leaked into grounding"; else ok "grounding excludes candidate-only relations (accepted.dl only)"; fi
rm -f "$KB/facts/candidates.csv"
# no accepted entity mentioned -> no grounding block
if printf '%s' "$(router wiki "completely unrelated xyzzy topic")" | grep -qF "grounding"; then bad "grounding shown without a mentioned entity"; else ok "no grounding block when no accepted entity is mentioned"; fi

# --- #33/#34: engine answers annotated with sources, confidence, staleness ---
printf '# a\n' > "$KB/sources/a.md"; printf '# b\n' > "$KB/sources/b.md"
printf 'subject,relation,object,source,status,confidence,note\nAcme API,uses,FastAPI,sources/a.md,confirmed,0.90,\nAcme API,uses,FastAPI,sources/b.md,confirmed,0.95,\n' > "$KB/facts/candidates.csv"
ann="$(router render 'relation("Acme API", "uses", V)?')"
printf '%s' "$ann" | grep -qF "sources: 2" && ok "engine answer annotated with distinct-source count" || bad "no source count annotation"
printf '%s' "$ann" | grep -qF "extraction conf: 0.95" && ok "annotation shows max extraction confidence (relabeled)" || bad "extraction conf annotation missing/wrong"
printf '%s' "$ann" | grep -qE "[^ ]confidence: 0.95|[(]confidence:" && bad "bare 'confidence:' must be relabeled 'extraction conf:'" || ok "no bare 'confidence:' label leaks into the verified block"
printf '%s' "$ann" | grep -qF "stale" && bad "non-stale fact wrongly flagged stale" || ok "present sources are not flagged stale"
# staleness: backing source file missing -> flagged
printf 'subject,relation,object,source,status,confidence,note\nAcme API,uses,FastAPI,sources/gone.md,confirmed,0.90,\n' > "$KB/facts/candidates.csv"
if router render 'relation("Acme API", "uses", V)?' | grep -qF "[stale: source missing]"; then ok "fact with a vanished source is flagged stale"; else bad "stale source not flagged"; fi
rm -f "$KB/facts/candidates.csv" "$KB/sources/a.md" "$KB/sources/b.md"
# no candidates.csv -> no annotation, still renders
if router render 'relation("Acme API", "uses", V)?' | grep -qF "VERIFIED — engine"; then ok "engine answer renders without candidates.csv (no annotation)"; else bad "engine render broke without candidates.csv"; fi

# --- #35: count aggregation query (engine-verified) ---
check_field "count routes engine" validate 'count("Acme API", "uses")?' route engine
check_field "count valid -> code ok" validate 'count("Acme API", "uses")?' code ok
if router render 'count("Acme API", "uses")?' | grep -qE '^  - 1$'; then ok "count returns the verified aggregate (1)"; else bad "count value wrong"; fi
check_field "count unknown entity -> wiki" validate 'count("Nope", "uses")?' route wiki
# valid vocabulary, zero objects -> verified zero (engine), NOT wiki/fact_absent
check_field "count of zero stays engine" validate 'count("FastAPI", "uses")?' route engine
if router render 'count("FastAPI", "uses")?' | grep -qE '^  - 0$'; then ok "count returns verified zero (not a fallback)"; else bad "count zero not rendered as 0"; fi

# --- #41: punctuation-edge tokens (C++/.NET/node.js) + single-CJK floor ---
if "$PYTHON" -c "
import sys, os
sys.path.insert(0, '$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT'] = '$KB'
import ask_router as a
assert any(p.search('we use c++ here') for p in a._keyword_patterns('C++ tooling')), 'c++ keyword'
assert any(p.search('built on node.js') for p in a._keyword_patterns('node.js runtime')), 'node.js keyword'
assert not any(p.search('the therapist') for p in a._keyword_patterns('api docs')), 'api must not match therapist'
assert a._entity_mentioned('C++', 'migrating to c++ now'), 'C++ entity'
assert a._entity_mentioned('.NET', 'uses .net here'), '.NET entity'
assert not a._entity_mentioned('물', '물고기 이야기'), 'single CJK char must not match a compound'
assert a._entity_mentioned('갑봇', '갑봇 질문'), 'multi-char CJK entity matches'
" 2>/dev/null; then ok "matcher: C++/.NET/node.js tokens + single-CJK floor (no api/therapist regression)"; else bad "matcher boundary/tokenizer test failed"; fi

# --- read-only invariant (engine inputs untouched by any subcommand) ---
if [ -f "$KB/facts/query.dl" ]; then bad "ask_router wrote facts/query.dl (must be read-only)"; else ok "facts/query.dl never written"; fi
if [ "$(cat "$KB/facts/accepted.dl")" = "$ACCEPTED_BEFORE" ]; then ok "facts/accepted.dl unchanged"; else bad "facts/accepted.dl was mutated"; fi

# --- engine answer lists the backing source path(s) (#81) --------------------
# a candidates-backed KB: one relation fact with TWO sources, both on disk.
SKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$SKB" >/dev/null
printf 'a\n' > "$SKB/sources/a.md"; printf 'b\n' > "$SKB/sources/b.md"
printf '// test\nrelation("Acme API", "uses", "FastAPI").\n' > "$SKB/facts/accepted.dl"
printf 'subject,relation,object,source,status,confidence,note\n%s\n%s\n' \
  'Acme API,uses,FastAPI,sources/a.md,confirmed,0.90,' \
  'Acme API,uses,FastAPI,sources/b.md,confirmed,0.95,' > "$SKB/facts/candidates.csv"
sout="$("$PYTHON" "$ROUTER" render 'relation("Acme API", "uses", V)?' --target "$SKB")"
printf '%s' "$sout" | grep -qF "(sources: 2, extraction conf: 0.95)" && ok "engine answer keeps the sources/extraction-conf signal" || bad "signal line wrong: $sout"
printf '%s' "$sout" | grep -qF "← sources/a.md" && printf '%s' "$sout" | grep -qF "← sources/b.md" \
  && ok "engine answer lists both backing source paths" || bad "source paths not listed: $sout"
# a missing backing source is flagged stale on the main line
printf 'subject,relation,object,source,status,confidence,note\n%s\n' \
  'Acme API,uses,FastAPI,sources/gone.md,confirmed,0.90,' > "$SKB/facts/candidates.csv"
gout="$("$PYTHON" "$ROUTER" render 'relation("Acme API", "uses", V)?' --target "$SKB")"
printf '%s' "$gout" | grep -qF "[stale: source missing]" && printf '%s' "$gout" | grep -qF "← sources/gone.md" \
  && ok "engine answer lists a stale source and flags it" || bad "stale source path handling wrong: $gout"

# --- engine-DERIVED relation row carries no extraction confidence ------------
# A relation result with no extracted backing (no signal entry) is rule-inferred,
# not extracted, so it must be marked derived rather than shown with a confidence.
# Drive render_engine_answer directly: one backed row + one unbacked (derived) row.
if "$PYTHON" -c "
import sys
sys.path.insert(0, '$PLUGIN_ROOT/tools')
from ask_router import render_engine_answer
rows = [['Acme API', 'uses', 'FastAPI'], ['Acme API', 'reaches', 'Datadog']]
signals = {('Acme API', 'uses', 'FastAPI'): {'sources': 1, 'source_paths': ['sources/a.md'], 'confidence': '0.90', 'stale': False}}
out = render_engine_answer('relation(\"Acme API\", R, O)?', rows, signals)
assert 'uses, FastAPI (sources: 1, extraction conf: 0.90)' in out, out
assert 'reaches, Datadog [no extraction backing]' in out, out
# the unbacked row must NOT carry any extraction-conf annotation
assert 'reaches, Datadog (' not in out, out
" 2>/dev/null; then ok "unbacked relation row marked '[no extraction backing]', backed row keeps extraction conf"; else bad "backed/unbacked relation row distinction wrong in render_engine_answer"; fi

# integration: a relation in accepted.dl with NO candidates.csv backing (a desync)
# renders the '[no extraction backing]' marker through the full render command.
DKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$DKB" >/dev/null
printf '// test\nrelation(\"Acme API\", \"uses\", \"FastAPI\").\n' > "$DKB/facts/accepted.dl"
printf 'subject,relation,object,source,status,confidence,note\n' > "$DKB/facts/candidates.csv"  # empty: backs nothing
dout="$("$PYTHON" "$ROUTER" render 'relation("Acme API", "uses", V)?' --target "$DKB")"
printf '%s' "$dout" | grep -qF "[no extraction backing]" && ok "desynced relation (accepted.dl without candidates backing) marked via full render" || bad "desync marker missing via render: $dout"
printf '%s' "$dout" | grep -qF "extraction conf:" && bad "unbacked row must not show an extraction conf" || ok "desynced relation carries no extraction conf"

# non-relation predicates (signals=None) never get a derived marker (computed rows)
if "$PYTHON" -c "
import sys
sys.path.insert(0, '$PLUGIN_ROOT/tools')
from ask_router import render_engine_answer
out = render_engine_answer('count(\"Acme API\", \"uses\")?', [['1']], None)
assert 'derived — no extraction confidence' not in out, out
assert 'extraction conf' not in out, out
" 2>/dev/null; then ok "non-relation (path/count/policy) rows render plain, no derived/conf annotation"; else bad "non-relation row wrongly annotated"; fi

# --- #227 SLICE 1: canonical relation name query expansion ---
# A canonical name (one that appears as a target in relation-aliases.md) must:
#   1. validate as route=engine (not relation_not_accepted -> wiki)
#   2. evaluate/render to ALL surface-variant rows (real stored triples, real provenance)
#   3. Without an alias file: every behavior byte-identical to today (opt-in no-op)
AKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$AKB" >/dev/null
# Two facts stored under different surface variants of the same canonical.
printf '// test\nrelation("논문A", "게재연도", "2005").\nrelation("논문B", "publication_year", "2007").\n' \
  > "$AKB/facts/accepted.dl"
# candidates.csv backs both facts (so no [no extraction backing] appears).
printf 'subject,relation,object,source,status,confidence,note\n%s\n%s\n' \
  '논문A,게재연도,2005,sources/paper-a.md,confirmed,0.90,' \
  '논문B,publication_year,2007,sources/paper-b.md,confirmed,0.85,' \
  > "$AKB/facts/candidates.csv"
mkdir -p "$AKB/sources"
printf '# paper A\n' > "$AKB/sources/paper-a.md"
printf '# paper B\n' > "$AKB/sources/paper-b.md"
# Alias file: both surface variants map to the canonical published_year.
printf '# Relation aliases\n- `게재연도` -> `published_year`\n- `publication_year` -> `published_year`\n' \
  > "$AKB/policy/relation-aliases.md"

arouter() { "$PYTHON" "$ROUTER" "$@" --target "$AKB"; }

# 1. validate: canonical name -> route=engine (not wiki/relation_not_accepted)
check_field_router() {  # like check_field but uses arouter
  local desc="$1" sub="$2" draft="$3" key="$4" expected="$5"
  local got; got="$(arouter "$sub" "$draft" | field "$key")"
  if [ "$got" = "$expected" ]; then ok "$desc ($key=$got)"; else bad "$desc — expected $key=$expected, got $got"; fi
}
check_field_router "#227: canonical name routes engine (not wiki)" validate 'relation("논문A", "published_year", X)?' route engine
check_field_router "#227: canonical name code=ok (positive, not fact_absent)" validate 'relation(S, "published_year", O)?' code ok
check_field_router "#227: canonical query not flagged negative" validate 'relation(S, "published_year", O)?' negative False
alias_typo="$(arouter render 'relation("논문A", "publshed_year", O)?')"
if printf '%s' "$alias_typo" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); raise SystemExit(0 if d['did_you_mean']==[{'kind':'relation','term':'publshed_year','suggestions':['published_year']}] else 1)"; then ok "#273: typo suggests declared canonical relation alias"; else bad "#273 alias suggestion missing/wrong: $alias_typo"; fi

# 2. evaluate: canonical query returns BOTH surface-variant rows
aeval_count="$(arouter evaluate 'relation(S, "published_year", O)?' | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['count'])")"
if [ "$aeval_count" = "2" ]; then ok "#227: canonical evaluate returns 2 surface-variant rows"; else bad "#227: canonical evaluate count wrong (expected 2, got $aeval_count)"; fi

# 3. render: canonical query shows both real stored rows, no [no extraction backing]
arender="$(arouter render 'relation(S, "published_year", O)?')"
if printf '%s' "$arender" | grep -qF "VERIFIED — engine"; then ok "#227: canonical render carries VERIFIED marker"; else bad "#227: canonical render missing VERIFIED marker"; fi
if printf '%s' "$arender" | grep -qF "논문A, 게재연도, 2005"; then ok "#227: canonical render shows 논문A/게재연도 row"; else bad "#227: canonical render missing 논문A row"; fi
if printf '%s' "$arender" | grep -qF "논문B, publication_year, 2007"; then ok "#227: canonical render shows 논문B/publication_year row"; else bad "#227: canonical render missing 논문B row"; fi
if printf '%s' "$arender" | grep -qF "[no extraction backing]"; then bad "#227: canonical render wrongly shows [no extraction backing]"; else ok "#227: canonical render: real stored rows carry real provenance (no [no extraction backing])"; fi

# 4. Without alias file: normal query unchanged (opt-in no-op)
NKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$NKB" >/dev/null
printf '// test\nrelation("Acme API", "uses", "FastAPI").\n' > "$NKB/facts/accepted.dl"
nrouter() { "$PYTHON" "$ROUTER" "$@" --target "$NKB"; }
ncheck_field() {
  local desc="$1" sub="$2" draft="$3" key="$4" expected="$5"
  local got; got="$(nrouter "$sub" "$draft" | field "$key")"
  if [ "$got" = "$expected" ]; then ok "$desc ($key=$got)"; else bad "$desc — expected $key=$expected, got $got"; fi
}
ncheck_field "#227: no alias file — existing relation still routes engine" validate 'relation("Acme API", "uses", V)?' route engine
ncheck_field "#227: no alias file — unknown relation still routes wiki" validate 'relation("Acme API", "published_year", V)?' route wiki
ncheck_field "#227: no alias file — unknown relation code=relation_not_accepted" validate 'relation("Acme API", "published_year", V)?' code relation_not_accepted

# --- #227 SLICE 1 commit 3: count() canonical symmetry ---
# AKB is still set from above (same alias KB: 게재연도/publication_year -> published_year,
# two facts with distinct objects 2005 and 2007).

# 5. count(S, canonical)? -> validates as engine (symmetry with relation branch)
check_field_router "#227 count: canonical name routes engine" validate 'count("논문A", "published_year")?' route engine
check_field_router "#227 count: canonical name code=ok" validate 'count("논문A", "published_year")?' code ok

# 6. count evaluates to the correct distinct-object count across variants
# 논문A has 1 object (2005 via 게재연도); canonical query must find it.
acount_val="$(arouter evaluate 'count("논문A", "published_year")?' | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['rows'][0][0])")"
if [ "$acount_val" = "1" ]; then ok "#227 count: canonical count(논문A, published_year) = 1"; else bad "#227 count: expected 1, got $acount_val"; fi

# Full-KB count: 2 subjects, each 1 distinct object -> total 2 distinct objects.
acount_all="$(arouter evaluate 'count(S, "published_year")?' | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['rows'][0][0])")"
if [ "$acount_all" = "2" ]; then ok "#227 count: canonical count(S, published_year) = 2 distinct objects"; else bad "#227 count: expected 2 distinct objects, got $acount_all"; fi

# 7. Collision: add a row stored under the canonical name itself (published_year).
# relation(S, "published_year", O)? must return 3 rows (no double-count).
CKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$CKB" >/dev/null
printf '// test\nrelation("논문A", "published_year", "2003").\nrelation("논문B", "게재연도", "2005").\nrelation("논문C", "publication_year", "2007").\n' \
  > "$CKB/facts/accepted.dl"
printf 'subject,relation,object,source,status,confidence,note\n%s\n%s\n%s\n' \
  '논문A,published_year,2003,sources/a.md,confirmed,0.90,' \
  '논문B,게재연도,2005,sources/b.md,confirmed,0.90,' \
  '논문C,publication_year,2007,sources/c.md,confirmed,0.90,' \
  > "$CKB/facts/candidates.csv"
printf '# Relation aliases\n- `게재연도` -> `published_year`\n- `publication_year` -> `published_year`\n' \
  > "$CKB/policy/relation-aliases.md"
crouter() { "$PYTHON" "$ROUTER" "$@" --target "$CKB"; }
collision_count="$(crouter evaluate 'relation(S, "published_year", O)?' | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['count'])")"
if [ "$collision_count" = "3" ]; then ok "#227 collision: relation canonical+variants returns exactly 3 rows (no double-count)"; else bad "#227 collision: expected 3 rows, got $collision_count"; fi

# 8. count on collision KB: 3 distinct objects -> count = 3
collision_cnt_val="$(crouter evaluate 'count(S, "published_year")?' | "$PYTHON" -c "import json,sys; print(json.load(sys.stdin)['rows'][0][0])")"
if [ "$collision_cnt_val" = "3" ]; then ok "#227 collision count: count(S, published_year) = 3 distinct objects (no double-count)"; else bad "#227 collision count: expected 3, got $collision_cnt_val"; fi

# --- #189: coverage hint on verified-negative relation (predicate mismatch) ---
# A verified-negative relation query whose SUBJECT is an accepted entity carrying
# fact(s) under OTHER relations must surface an informational hint distinguishing a
# predicate mismatch from an honest absence — WITHOUT changing the verdict.
HKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$HKB" >/dev/null
# 갑기업: 3 facts under 설립연도/대표/본사, none under 게재연도.
# 게재연도 IS an accepted relation (used by 논문A) -> the query is a verified
# negative (accepted vocabulary, fact absent), not a wiki route.
# 고아엔티티: an accepted entity ONLY as the object of 인용 -> 0 subject facts.
printf '// t\n%s\n%s\n%s\n%s\n%s\n' \
  'relation("갑기업", "설립연도", "1998").' \
  'relation("갑기업", "대표", "김철수").' \
  'relation("갑기업", "본사", "서울").' \
  'relation("논문A", "게재연도", "2005").' \
  'relation("논문A", "인용", "고아엔티티").' \
  > "$HKB/facts/accepted.dl"
hrouter() { "$PYTHON" "$ROUTER" "$@" --target "$HKB"; }
HACCEPTED_BEFORE="$(cat "$HKB/facts/accepted.dl")"

# routing/verdict unchanged: still a verified negative, engine route.
check_field_h() {
  local desc="$1" sub="$2" draft="$3" key="$4" expected="$5"
  local got; got="$(hrouter "$sub" "$draft" | field "$key")"
  if [ "$got" = "$expected" ]; then ok "$desc ($key=$got)"; else bad "$desc — expected $key=$expected, got $got"; fi
}
check_field_h "#189: predicate-mismatch query stays a verified negative (engine)" validate 'relation("갑기업", "게재연도", O)?' negative True
check_field_h "#189: predicate-mismatch query code=fact_absent" validate 'relation("갑기업", "게재연도", O)?' code fact_absent

hpos="$(hrouter render 'relation("갑기업", "게재연도", O)?')"
# verdict block untouched: still VERIFIED — engine + verified negative.
if printf '%s' "$hpos" | grep -qF "VERIFIED — engine" && printf '%s' "$hpos" | grep -qF "verified negative"; then ok "#189: verified-negative verdict block preserved alongside the hint"; else bad "#189: hint disturbed the verified-negative verdict block"; fi
# the hint line: predicate-mismatch note naming the OTHER relations, sorted.
if printf '%s' "$hpos" | grep -qF "possible predicate mismatch"; then ok "#189: predicate-mismatch hint appears"; else bad "#189: predicate-mismatch hint missing"; fi
if printf '%s' "$hpos" | grep -qF "3 fact(s) under other relations"; then ok "#189: hint reports the correct other-relation fact count (3)"; else bad "#189: hint fact count wrong"; fi
if printf '%s' "$hpos" | grep -qF "대표, 본사, 설립연도"; then ok "#189: hint lists the OTHER relations sorted deterministically"; else bad "#189: hint relation listing wrong/unsorted"; fi
# the hint must NOT name the queried relation as an 'other' relation.
if printf '%s' "$hpos" | grep -qE "other relations[^:]*게재연도"; then bad "#189: queried relation leaked into the 'other relations' listing"; else ok "#189: queried relation excluded from the 'other relations' listing"; fi
# evaluate JSON carries the optional coverage_hint field (rows/count unchanged).
check_field_h "#189: evaluate keeps count=0 (field is additive)" evaluate 'relation("갑기업", "게재연도", O)?' count 0
if hrouter evaluate 'relation("갑기업", "게재연도", O)?' | grep -qF "coverage_hint"; then ok "#189: evaluate JSON exposes the optional coverage_hint field"; else bad "#189: evaluate JSON missing coverage_hint"; fi

# EVALUATE SCOPE PIN: the hint is defined ONLY for a verified negative. An accepted
# subject with an UNACCEPTED object routes to wiki (negative=False) — evaluate must
# NOT leak coverage_hint there (machine output honors the same gate as render).
# Mutation guard: removing the verified-negative gate re-leaks the hint here.
check_field_h "#189 scope: accepted subject + unaccepted object routes wiki" validate 'relation("갑기업", "게재연도", "새로운값999")?' route wiki
check_field_h "#189 scope: wiki-routed query is not a verified negative" validate 'relation("갑기업", "게재연도", "새로운값999")?' negative False
if hrouter evaluate 'relation("갑기업", "게재연도", "새로운값999")?' | grep -qF "coverage_hint"; then bad "#189 scope: evaluate leaked coverage_hint on a wiki-routed (non-verified-negative) query"; else ok "#189 scope: evaluate omits coverage_hint on a wiki-routed query (verified-negative gate honored)"; fi

# NO FALSE POSITIVE: an accepted entity (object-only) with 0 subject facts -> no hint.
hneg="$(hrouter render 'relation("고아엔티티", "게재연도", O)?')"
if printf '%s' "$hneg" | grep -qF "VERIFIED — engine" && printf '%s' "$hneg" | grep -qF "verified negative"; then ok "#189: honest-absence subject still a verified negative"; else bad "#189: honest-absence verdict wrong"; fi
if printf '%s' "$hneg" | grep -qF "possible predicate mismatch"; then bad "#189: false-positive hint on a subject with zero facts (honest absence violated)"; else ok "#189: no hint for a subject with zero facts (honest absence preserved)"; fi
if hrouter evaluate 'relation("고아엔티티", "게재연도", O)?' | grep -qF "coverage_hint"; then bad "#189: evaluate emitted coverage_hint for an honest absence"; else ok "#189: evaluate omits coverage_hint for an honest absence"; fi

# OBJECT MISMATCH (not predicate mismatch): subject HAS the queried relation, just
# not this object -> no predicate-mismatch hint (the relation is present).
# Object "2005" is an accepted value (게재연도 of 논문A) so the query is a verified
# negative (not an unaccepted-object wiki route); 갑기업 HAS 설립연도 (=1998).
hobj="$(hrouter render 'relation("갑기업", "설립연도", "2005")?')"
if printf '%s' "$hobj" | grep -qF "verified negative"; then ok "#189: object-mismatch query is a verified negative"; else bad "#189: object-mismatch not a verified negative"; fi
if printf '%s' "$hobj" | grep -qF "possible predicate mismatch"; then bad "#189: predicate-mismatch hint fired on an OBJECT mismatch (subject has the relation)"; else ok "#189: no predicate-mismatch hint when the subject has the queried relation (object mismatch)"; fi

# UNKNOWN ENTITY: a non-accepted subject routes to wiki and carries no hint.
check_field_h "#189: unknown subject still routes wiki (unchanged)" validate 'relation("존재안함", "게재연도", O)?' route wiki
if hrouter render 'relation("존재안함", "게재연도", O)?' | grep -qF "possible predicate mismatch"; then bad "#189: hint appeared for an unknown (wiki-routed) subject"; else ok "#189: no hint for an unknown, wiki-routed subject"; fi

# read-only invariant preserved by the hint path.
if [ -f "$HKB/facts/query.dl" ]; then bad "#189: coverage-hint path wrote facts/query.dl"; else ok "#189: coverage-hint path never writes facts/query.dl"; fi
if [ "$(cat "$HKB/facts/accepted.dl")" = "$HACCEPTED_BEFORE" ]; then ok "#189: coverage-hint path leaves accepted.dl unchanged"; else bad "#189: coverage-hint path mutated accepted.dl"; fi

# --- #279: renderer row caps are explicit and escapable ---------------------
# Test below / at / above the same small cap directly.  This keeps the boundary
# deterministic without making the fixture depend on the production default.
if "$PYTHON" -c "
import sys
sys.path.insert(0, '$PLUGIN_ROOT/tools')
from ask_router import render_engine_answer, render_wiki_answer

rows = [['S1', 'rel', 'O1'], ['S2', 'rel', 'O2'], ['S3', 'rel', 'O3']]
for count in (1, 2):
    out = render_engine_answer('relation(S, rel, O)?', rows[:count], limit=2)
    assert f'rows: {count}' in out, out
    assert 'more rows' not in out, out
out = render_engine_answer('relation(S, rel, O)?', rows, limit=2)
assert 'rows: 3' in out, out
assert 'S1, rel, O1' in out and 'S2, rel, O2' in out and 'S3, rel, O3' not in out, out
assert '… 1 more rows (full output: --all)' in out, out
all_out = render_engine_answer('relation(S, rel, O)?', rows, limit=None)
assert all(row[0] in all_out for row in rows), all_out
assert 'more rows' not in all_out, all_out

# One varying column is an indexed, lossless projection: the fixed positions and
# every varying value are printed, so the displayed triples can be reconstructed.
same_tail = [['S1', 'rel', 'O'], ['S2', 'rel', 'O'], ['S3', 'rel', 'O']]
signals = {('S1', 'rel', 'O'): {'sources': 1, 'source_paths': ['sources/a.md'], 'confidence': '0.90', 'stale': False}}
compact = render_engine_answer('relation(S, rel, O)?', same_tail, signals, limit=None)
assert 'rows differ only at column 0; fixed: [1] rel, [2] O' in compact, compact
assert all(f'    - S{i}' in compact for i in range(1, 4)), compact
assert '← sources/a.md' in compact, compact

results = [
    {'file': 'sources/a.md', 'line': 1, 'dir': 'sources', 'excerpt': 'alpha'},
    {'file': 'sources/b.md', 'line': 2, 'dir': 'sources', 'excerpt': 'beta'},
    {'file': 'sources/c.md', 'line': 3, 'dir': 'sources', 'excerpt': 'gamma'},
]
grounding = [
    {'subject': 'S1', 'relation': 'rel', 'object': 'O1'},
    {'subject': 'S2', 'relation': 'rel', 'object': 'O2'},
    {'subject': 'S3', 'relation': 'rel', 'object': 'O3'},
]
wiki = render_wiki_answer('question', 'reason', results, grounding, limit=2, total_results=3)
assert 'UNVERIFIED — wiki exploration' in wiki, wiki
assert 'WARNING: unverified candidates' in wiki, wiki
assert wiki.index('WARNING: unverified candidates') < wiki.index('VERIFIED — engine (grounding'), wiki
assert 'grounding facts: 3' in wiki and 'S3, rel, O3' not in wiki, wiki
assert '[sources/a.md:1] (sources)' in wiki and '[sources/b.md:2] (sources)' in wiki and '[sources/c.md:3]' not in wiki, wiki
assert wiki.count('… 1 more rows (full output: --all)') == 2, wiki
" 2>/dev/null; then ok "#279: engine/wiki caps retain totals, citations, warning, and explicit truncation"; else bad "#279: renderer cap contract failed"; fi

# JSON search keeps its existing `results` array and adds an explicit total and
# truncation flag. --all is the lossless escape hatch for the same corpus.
LKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$LKB" >/dev/null
for n in $(seq 1 11); do printf 'limitprobe item %s\n' "$n" > "$LKB/sources/$n.md"; done
limited_search="$("$PYTHON" "$ROUTER" search limitprobe --target "$LKB")"
all_search="$("$PYTHON" "$ROUTER" search limitprobe --all --target "$LKB")"
if printf '%s' "$limited_search" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); assert len(d['results']) == 10 and d['total'] == 11 and d['truncated'] is True"; then ok "#279: JSON search exposes capped total and truncation"; else bad "#279: JSON search cap metadata missing/wrong"; fi
if printf '%s' "$all_search" | "$PYTHON" -c "import json,sys; d=json.load(sys.stdin); assert len(d['results']) == 11 and d['total'] == 11 and d['truncated'] is False"; then ok "#279: JSON search --all returns every excerpt"; else bad "#279: JSON search --all is not lossless"; fi

# --- #577: a wiki answer's decomposition proposals really do route to the engine ---
# The proposal block (rendered by the wiki path, pinned in tests/test_ask_wiki_search.sh)
# claims every line it prints is engine-answerable. That claim is about ROUTING, which is
# this file's subject: checked only over there, the renderer would be grading its own
# homework — claim and check would both come from one module's view of classify_query.
# Here the PRINTED line is fed back through the `validate` CLI, the same entry point
# every other routing check in this file uses.
DKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$DKB" >/dev/null
printf '// t\n%s\n%s\n%s\n%s\n' \
  'relation("논문1", "가_관계", "알파개념_하나").' \
  'relation("논문2", "가_관계", "알파개념_둘").' \
  'relation("논문3", "하_관계", "베타개념_하나").' \
  'relation("논문4", "하_관계", "베타개념_둘").' \
  > "$DKB/facts/accepted.dl"
drouter() { "$PYTHON" "$ROUTER" "$@" --target "$DKB"; }
DACCEPTED_BEFORE="$(cat "$DKB/facts/accepted.dl")"

check_field_d() {  # check_field_d <desc> <subcmd> <draft> <key> <expected>
  local desc="$1" sub="$2" draft="$3" key="$4" expected="$5"
  local got; got="$(drouter "$sub" "$draft" | field "$key")"
  if [ "$got" = "$expected" ]; then ok "$desc ($key=$got)"; else bad "$desc — expected $key=$expected, got $got"; fi
}
# The premise, measured rather than assumed: the combined condition has no shape in this
# language, so the conjunction a user would write is not a query at all. If this ever
# routes engine, the proposals below are the wrong answer to a solved problem.
check_field_d "#577: a conjunctive draft is not expressible (the defect's premise)" \
  validate 'relation(X, "가_관계", "알파개념_하나"), relation(X, "하_관계", "베타개념_하나")?' route wiki

d_answer="$(drouter wiki '알파개념 이면서 베타개념 인 것은?' --reason 'review_required' || true)"
d_props="$(printf '%s\n' "$d_answer" | sed -n 's/^  \(relation(X, .*)?\)  — .*/\1/p' || true)"
# `|| true` on the count: with `set -e` a grep matching nothing exits 1 inside an
# assignment and kills the run, and every check after this point would go silent.
d_count="$(printf '%s\n' "$d_props" | grep -c '^relation(' || true)"
# The fixture premise first — with zero proposals the loop below inspects nothing and
# reports success.
if [ "$d_count" = "4" ]; then ok "#577: the wiki answer proposed 4 single queries"; else bad "#577: expected 4 proposals, got [$d_count] — block: [$d_answer]"; fi

d_bad=0
while IFS= read -r proposal; do
  [ -n "$proposal" ] || continue
  d_json="$(drouter validate "$proposal")"
  d_route="$(printf '%s' "$d_json" | field route)"
  d_code="$(printf '%s' "$d_json" | field code)"
  if [ "$d_route" != "engine" ] || [ "$d_code" != "ok" ]; then
    d_bad=$((d_bad + 1))
    echo "  ^ $proposal -> route=$d_route code=$d_code" >&2
  fi
done <<EOF
$d_props
EOF
if [ "$d_bad" -eq 0 ]; then ok "#577: every proposed query validates as route=engine code=ok (수용 기준 2)"; else bad "#577: $d_bad proposed query(ies) do not route to the engine"; fi

# Proposed, never executed: the wiki path stays read-only, and no proposal has been
# turned into a query the KB records.
if [ -f "$DKB/facts/query.dl" ]; then bad "#577: the proposal path wrote facts/query.dl"; else ok "#577: the proposal path never writes facts/query.dl"; fi
if [ "$(cat "$DKB/facts/accepted.dl")" = "$DACCEPTED_BEFORE" ]; then ok "#577: the proposal path leaves accepted.dl unchanged"; else bad "#577: the proposal path mutated accepted.dl"; fi

echo ""
echo "========================================"
echo "test_ask_router: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
