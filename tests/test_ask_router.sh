#!/usr/bin/env bash
# tests/test_ask_router.sh — deterministic /factlog ask routing core
#
# Proves the reason-class routing and relation evaluation of tools/ask_router.py:
#   - matching relation            -> route=engine, negative=false
#   - accepted vocab, fact absent  -> route=engine, negative=TRUE (verified
#                                     negative — NEVER wiki)
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

# --- path routing & verified-negative (renderable for any predicate) ---
check_field "reachable path routes engine" validate 'path("Acme API", "FastAPI")?' route engine
check_field "unreachable path = verified negative (engine)" validate 'path("Postgres", "FastAPI")?' route engine
check_field "unreachable path flagged negative" validate 'path("Postgres", "FastAPI")?' negative True
pneg="$(router render 'path("Postgres", "FastAPI")?')"
if printf '%s' "$pneg" | grep -qF "VERIFIED — engine" && printf '%s' "$pneg" | grep -qF "verified negative"; then ok "path verified-negative renders as an engine answer (not deferred/wiki)"; else bad "path verified-negative not rendered as engine answer"; fi

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

# --- Phase 2: path (positive render / variable) + policy + decisions ---
ppos="$(router render 'path("Acme API", "FastAPI")?')"
if printf '%s' "$ppos" | grep -qF "VERIFIED — engine" && printf '%s' "$ppos" | grep -qF "Acme API, FastAPI"; then ok "path positive renders the dependency path as an engine answer"; else bad "path positive not rendered"; fi
check_field "path with a variable enumerates reachable pairs" evaluate 'path("Acme API", T)?' count 2

# policy-predicate evaluation needs pyrewire (run_wirelog); guard so CI without
# pyrewire skips rather than fails. Compile a tiny policy first.
if "$PYTHON" -c "import pyrewire; raise SystemExit(0 if tuple(int(x) for x in pyrewire.__version__.split('.')[:3]) >= (1,0,1) else 1)" >/dev/null 2>&1; then
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
else
  echo "SKIP: pyrewire unavailable — skipping policy-predicate evaluation assertions"
fi

# decisions/ is searched as clearly-labeled SUPPLEMENTARY context
printf '# Open Questions\n\n## review\n- needs_review widgetterm pending\n' > "$KB/decisions/open-questions.md"
dout="$(router search "widgetterm")"
if printf '%s' "$dout" | grep -qF 'decisions (supplementary)'; then ok "decisions/ searched as labeled supplementary"; else bad "decisions/ supplementary not surfaced"; fi
rm -f "$KB/decisions/open-questions.md"

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

echo ""
echo "========================================"
echo "test_ask_router: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
