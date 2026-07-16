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

echo ""
echo "========================================"
echo "test_ask_router: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
