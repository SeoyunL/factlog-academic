#!/usr/bin/env bash
# tests/test_finalize.sh — one-shot deterministic finalize chain (#29)
#
# After extraction writes runs/*.json, `finalize.py` chains merge -> ensure
# policy -> compile -> (logic check). This pins:
#   - candidates.csv and accepted.dl are produced (pyrewire-independent)
#   - policy/logic-policy.dl is ensured so the check can load (stub if no rules)
#   - with pyrewire>=1.0.3: logic_report.txt is produced; without it the check
#     is skipped gracefully (no hard failure) and facts are still compiled
#   - idempotent: re-running does not duplicate the fact
#
# Usage: bash tests/test_finalize.sh  -> 0 if all pass, 1 otherwise.

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
FINALIZE="$PLUGIN_ROOT/tools/finalize.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# #336: without pyrewire>=1.0.3 finalize's logic check is SKIPPED and it exits 3
# (distinct from a verified pass=0, argparse=2, failure=1, timeout=124) so automation
# can tell an unverified compile from an engine-checked one. With the engine present it
# exits 0. Every "finalize exits 0" assertion below is really "0 if verified, 3 if the
# skip fired", so key them off the actual environment.
if "$PYTHON" -c "import pyrewire; raise SystemExit(0 if tuple(int(x) for x in pyrewire.__version__.split('.')[:3])>=(1,0,3) else 1)" >/dev/null 2>&1; then
  SKIP_RC=0
else
  SKIP_RC=3
fi

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf '# src\n\nAcme API uses FastAPI.\n' > "$KB/sources/acme.md"
printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/acme.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB/runs/r1.json"

# Capture rc under `set -e`: finalize now exits 3 on the no-pyrewire skip (#336), so a
# bare `out=$(...); rc=$?` would abort the whole script at the assignment.
rc=0; out="$("$PYTHON" "$FINALIZE" --target "$KB" 2>&1)" || rc=$?
[ "$rc" -eq "$SKIP_RC" ] && ok "finalize exits $SKIP_RC (0 verified / 3 unverified skip)" || bad "finalize exited $rc (expected $SKIP_RC)"

[ -s "$KB/facts/candidates.csv" ] && ok "candidates.csv produced" || bad "no candidates.csv"
if [ -f "$KB/facts/accepted.dl" ] && grep -q 'relation("Acme API", "uses", "FastAPI")' "$KB/facts/accepted.dl"; then ok "accepted.dl compiled with the fact"; else bad "accepted.dl missing the fact"; fi
[ -f "$KB/policy/logic-policy.dl" ] && ok "policy/logic-policy.dl ensured" || bad "policy/logic-policy.dl not ensured"

if "$PYTHON" -c "import pyrewire; raise SystemExit(0 if tuple(int(x) for x in pyrewire.__version__.split('.')[:3])>=(1,0,1) else 1)" >/dev/null 2>&1; then
  [ -f "$KB/facts/logic_report.txt" ] && ok "logic_report.txt produced (pyrewire present)" || bad "logic_report.txt missing despite pyrewire"
  printf '%s' "$out" | grep -qF "logic-checked" && ok "summary reports logic-checked" || bad "summary missing logic-checked"
else
  printf '%s' "$out" | grep -qF "Logic check SKIPPED" && ok "logic check skipped gracefully without pyrewire" || bad "no graceful-skip note without pyrewire"
fi

# idempotency: re-run must not duplicate the fact
"$PYTHON" "$FINALIZE" --target "$KB" >/dev/null 2>&1 || true
n="$(grep -c 'relation("Acme API", "uses", "FastAPI")' "$KB/facts/accepted.dl")"
[ "$n" = "1" ] && ok "idempotent re-run (fact not duplicated)" || bad "re-run duplicated the fact ($n)"

# idempotency with a REAL compilable policy: generate_logic_policy writes
# runs/natural-language-to-policy-response.json (a JSON object); the SECOND
# finalize must not choke on it at the merge step.
KB2="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB2" >/dev/null
printf '# src\n\nAcme API deployed on AWS.\n' > "$KB2/sources/d.md"
printf '[{"subject":"Acme API","relation":"deployed_on","object":"AWS","source":"sources/d.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB2/runs/r1.json"
printf '# Logic policy\n\n## Rules\n\n- [hosting_check] 어떤 항목이 `deployed_on` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB2/policy/logic-policy.md"
r1=0; "$PYTHON" "$FINALIZE" --target "$KB2" >/dev/null 2>&1 || r1=$?
r2=0; "$PYTHON" "$FINALIZE" --target "$KB2" >/dev/null 2>&1 || r2=$?
if [ "$r1" -eq "$SKIP_RC" ] && [ "$r2" -eq "$SKIP_RC" ]; then ok "idempotent with a real policy (2nd finalize survives policy-response JSON in runs/)"; else bad "policy-rule KB: finalize not idempotent (rc1=$r1 rc2=$r2, expected $SKIP_RC)"; fi
[ -f "$KB2/policy/logic-policy.dl" ] && grep -q "requires_review" "$KB2/policy/logic-policy.dl" && ok "real policy compiled (not stubbed over)" || bad "real policy not compiled"

# --- #194: a policy that defines rules but FAILS to compile must NOT be stubbed
# over (stub-then-skip permanently ignores it). logic-policy.md below passes the
# has-rules check ([id] + a backtick relation) but the relation name has a space,
# so generate_logic_policy rejects it -> no .dl is produced. finalize must leave
# .dl ABSENT (not write a "// no policy rules" stub) so the next run retries and
# re-warns, and check's loud detection (#190) still sees the uncompiled state.
KB3="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB3" >/dev/null
printf '# src\n\nAcme API uses FastAPI.\n' > "$KB3/sources/a.md"
printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/a.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB3/runs/r1.json"
printf '# Logic policy\n\n## Rules\n\n- [c1] flag when `foo bar` occurs\n' > "$KB3/policy/logic-policy.md"

out3="$("$PYTHON" "$FINALIZE" --target "$KB3" 2>&1)" || true
if [ ! -f "$KB3/policy/logic-policy.dl" ]; then ok "#194: uncompilable policy leaves logic-policy.dl absent (no masking stub)"; else bad "#194: a stub .dl was written over an uncompilable policy"; fi
printf '%s' "$out3" | grep -qF "NOT applied" && ok "#194: finalize warns the policy is not applied" || bad "#194: missing not-applied warning"
# the WARNING must NOT falsely promise a plain re-run will apply it via a skip;
# it must state a stub was not written (so re-run genuinely retries).
printf '%s' "$out3" | grep -qF "no empty-policy stub was written" && ok "#194: warning states no stub written (re-run retries)" || bad "#194: warning still implies stub/skip"

# re-run must RE-WARN (the old bug: run 2 skipped silently because the stub existed)
out3b="$("$PYTHON" "$FINALIZE" --target "$KB3" 2>&1)" || true
printf '%s' "$out3b" | grep -qF "NOT applied" && ok "#194: re-run re-warns (not silently skipped)" || bad "#194: re-run went silent (stub-then-skip regression)"
[ ! -f "$KB3/policy/logic-policy.dl" ] && ok "#194: re-run still writes no stub" || bad "#194: re-run wrote a stub"

# recovery: once the policy is fixed, finalize compiles it (no leftover stub blocks it)
printf '# Logic policy\n\n## Rules\n\n- [c1] 어떤 항목이 `uses` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB3/policy/logic-policy.md"
"$PYTHON" "$FINALIZE" --target "$KB3" >/dev/null 2>&1 || true
[ -f "$KB3/policy/logic-policy.dl" ] && grep -q "requires_review" "$KB3/policy/logic-policy.dl" && ok "#194: fixed policy compiles on re-run (recovery)" || bad "#194: fixed policy did not compile (stub blocked regeneration?)"

# --- #194 self-heal: a KB already poisoned by a pre-fix finalize (a leftover
# "// no policy rules" stub sitting on top of a real policy) must recover. The
# stub would otherwise satisfy the skip guard forever AND fool /factlog check.
KB4="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB4" >/dev/null
printf '# src\n\nAcme API uses FastAPI.\n' > "$KB4/sources/a.md"
printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/a.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB4/runs/r1.json"
printf '# Logic policy\n\n## Rules\n\n- [c1] 어떤 항목이 `uses` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB4/policy/logic-policy.md"
printf '// no policy rules\n' > "$KB4/policy/logic-policy.dl"   # simulate the pre-fix stub
"$PYTHON" "$FINALIZE" --target "$KB4" >/dev/null 2>&1 || true
if grep -q "requires_review" "$KB4/policy/logic-policy.dl" 2>/dev/null; then ok "#194: self-heals a leftover stub over a compilable policy (regenerates real .dl)"; else bad "#194: leftover stub was NOT healed (skip guard still fooled)"; fi

# self-heal, uncompilable variant: a leftover stub over an UNCOMPILABLE policy is
# removed (not kept), so the state becomes loud-detectable rather than masked.
KB5="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB5" >/dev/null
printf '# src\n\nAcme API uses FastAPI.\n' > "$KB5/sources/a.md"
printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/a.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB5/runs/r1.json"
printf '# Logic policy\n\n## Rules\n\n- [c1] flag when `foo bar` occurs\n' > "$KB5/policy/logic-policy.md"
printf '// no policy rules\n' > "$KB5/policy/logic-policy.dl"
out5="$("$PYTHON" "$FINALIZE" --target "$KB5" 2>&1)" || true
[ ! -f "$KB5/policy/logic-policy.dl" ] && ok "#194: leftover stub over an uncompilable policy is removed (no longer masks)" || bad "#194: stub kept over an uncompilable policy"
printf '%s' "$out5" | grep -qF "NOT applied" && ok "#194: healed uncompilable KB now warns (was silent before)" || bad "#194: healed uncompilable KB stayed silent"

# a BENIGN stub (no rules in .md) must be left alone — self-heal must not churn it
KB6="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB6" >/dev/null   # init leaves prose-only .md (no rules)
printf '// no policy rules\n' > "$KB6/policy/logic-policy.dl"
"$PYTHON" "$FINALIZE" --target "$KB6" >/dev/null 2>&1 || true
[ -f "$KB6/policy/logic-policy.dl" ] && ok "#194: benign empty-policy stub is preserved (no false heal)" || bad "#194: benign stub wrongly removed"

# pyrewire-present ONLY: with the engine installed, an uncompiled policy must fail
# LOUD at run_logic_check (rc != 0), matching #190 — the design's loud half.
if "$PYTHON" -c "import pyrewire; raise SystemExit(0 if tuple(int(x) for x in pyrewire.__version__.split('.')[:3])>=(1,0,3) else 1)" >/dev/null 2>&1; then
  KB7="$(mktemp -d)/wiki"
  "$PYTHON" -m factlog init --target "$KB7" >/dev/null
  printf '# src\n\nAcme API uses FastAPI.\n' > "$KB7/sources/a.md"
  printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/a.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB7/runs/r1.json"
  printf '# Logic policy\n\n## Rules\n\n- [c1] flag when `foo bar` occurs\n' > "$KB7/policy/logic-policy.md"
  # capture rc under `set -e`: a bare `cmd; rc=$?` aborts the whole script the
  # instant finalize (correctly) exits non-zero, so the assertion below never runs.
  rc7=0; "$PYTHON" "$FINALIZE" --target "$KB7" >/dev/null 2>&1 || rc7=$?
  [ "$rc7" -ne 0 ] && ok "#194: uncompiled policy fails loud with pyrewire (rc=$rc7)" || bad "#194: uncompiled policy did not fail loud with pyrewire"
else
  echo "SKIP: pyrewire absent — skipping the loud-fail assertion (#194 loud half)"
fi

# --- #217: a real (non-stub) logic-policy.dl that already exists must be checked
# for staleness against logic-policy.md. The pre-#217 `if not policy_dl.is_file()`
# guard skipped regeneration whenever a .dl was present, so editing the .md after
# the first finalize was silently ignored and the engine kept applying the OLD
# compiled rules. finalize must now detect the drift (via generate --check) and
# regenerate so the current rules are applied.
KB8="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB8" >/dev/null
printf '# src\n\nAcme API uses FastAPI.\n' > "$KB8/sources/a.md"
printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/a.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB8/runs/r1.json"
# step 1: policy rule R1 (relation `uses`) -> finalize -> dl compiled with `uses`
printf '# Logic policy\n\n## Rules\n\n- [c1] 어떤 항목이 `uses` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB8/policy/logic-policy.md"
"$PYTHON" "$FINALIZE" --target "$KB8" >/dev/null 2>&1 || true
if grep -q '"uses"' "$KB8/policy/logic-policy.dl" 2>/dev/null; then ok "#217: R1 policy compiled (dl references \`uses\`)"; else bad "#217: R1 policy did not compile"; fi
# step 2: change the rule to R2 (relation `deployed_on`) and re-run finalize
printf '# Logic policy\n\n## Rules\n\n- [c1] 어떤 항목이 `deployed_on` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB8/policy/logic-policy.md"
out8="$("$PYTHON" "$FINALIZE" --target "$KB8" 2>&1)" || true
if grep -q '"deployed_on"' "$KB8/policy/logic-policy.dl" 2>/dev/null && ! grep -q '"uses"' "$KB8/policy/logic-policy.dl" 2>/dev/null; then
  ok "#217: stale dl regenerated to R2 (regression guard: was R1 forever)"
else
  bad "#217: stale dl NOT regenerated (still applying old R1)"
fi
printf '%s' "$out8" | grep -qF "was stale" && ok "#217: stale regeneration is surfaced (not silent)" || bad "#217: stale regeneration was silent"

# --- #217: in-sync dl is left untouched — no needless regeneration/warning, and
# the recompile note only appears on drift (idempotent, byte-identical re-run).
before8="$($PYTHON -c "import hashlib,sys;print(hashlib.md5(open(sys.argv[1],'rb').read()).hexdigest())" "$KB8/policy/logic-policy.dl")"
out8b="$("$PYTHON" "$FINALIZE" --target "$KB8" 2>&1)" || true
after8="$($PYTHON -c "import hashlib,sys;print(hashlib.md5(open(sys.argv[1],'rb').read()).hexdigest())" "$KB8/policy/logic-policy.dl")"
[ "$before8" = "$after8" ] && ok "#217: in-sync re-run leaves dl byte-identical (idempotent)" || bad "#217: in-sync re-run churned the dl"
printf '%s' "$out8b" | grep -qF "was stale" && bad "#217: in-sync re-run wrongly reported stale" || ok "#217: in-sync re-run is silent (no stale note)"

# --- #217: hand-authored logic-policy.extra.dl is never inspected or regenerated,
# even when logic-policy.dl is stale and gets regenerated.
KB9="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB9" >/dev/null
printf '# src\n\nAcme API uses FastAPI.\n' > "$KB9/sources/a.md"
printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/a.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB9/runs/r1.json"
printf '# Logic policy\n\n## Rules\n\n- [c1] 어떤 항목이 `uses` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB9/policy/logic-policy.md"
"$PYTHON" "$FINALIZE" --target "$KB9" >/dev/null 2>&1 || true
EXTRA_SENTINEL='// hand-authored extra policy — do not touch (#217)\n.decl custom_flag(entity: symbol)\n'
printf "$EXTRA_SENTINEL" > "$KB9/policy/logic-policy.extra.dl"
extra_before="$($PYTHON -c "import hashlib,sys;print(hashlib.md5(open(sys.argv[1],'rb').read()).hexdigest())" "$KB9/policy/logic-policy.extra.dl")"
printf '# Logic policy\n\n## Rules\n\n- [c1] 어떤 항목이 `deployed_on` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB9/policy/logic-policy.md"
"$PYTHON" "$FINALIZE" --target "$KB9" >/dev/null 2>&1 || true
extra_after="$($PYTHON -c "import hashlib,sys;print(hashlib.md5(open(sys.argv[1],'rb').read()).hexdigest())" "$KB9/policy/logic-policy.extra.dl")"
if [ "$extra_before" = "$extra_after" ] && grep -q '"deployed_on"' "$KB9/policy/logic-policy.dl" 2>/dev/null; then
  ok "#217: extra.dl untouched while stale logic-policy.dl is regenerated"
else
  bad "#217: extra.dl was modified or logic-policy.dl not regenerated"
fi

# --- #217 (symmetric transition rules→empty): after a real policy compiled into a
# .dl, REMOVING all rules from logic-policy.md must reset the .dl to the empty-policy
# stub — otherwise the engine keeps applying the OLD compiled rules (silent
# stale-apply, the same bug in the other direction). Reverting the fix (leaving the
# old rules in the .dl) must make this fail: it's a real regression guard.
KB10="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB10" >/dev/null
printf '# src\n\nAcme API uses FastAPI.\n' > "$KB10/sources/a.md"
printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/a.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB10/runs/r1.json"
# step 1: real rule R1 -> finalize -> dl compiled with requires_review
printf '# Logic policy\n\n## Rules\n\n- [c1] 어떤 항목이 `uses` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB10/policy/logic-policy.md"
"$PYTHON" "$FINALIZE" --target "$KB10" >/dev/null 2>&1 || true
if grep -q "requires_review" "$KB10/policy/logic-policy.dl" 2>/dev/null; then ok "#217: R1 real policy compiled (pre-condition)"; else bad "#217: R1 real policy did not compile (pre-condition)"; fi
# step 2: remove ALL rules from the .md (prose-only) and re-run finalize
printf '# Logic policy\n\nNo rules yet.\n' > "$KB10/policy/logic-policy.md"
rc10=0; out10="$("$PYTHON" "$FINALIZE" --target "$KB10" 2>&1)" || rc10=$?
if [ "$(cat "$KB10/policy/logic-policy.dl")" = "// no policy rules" ] && ! grep -q "requires_review" "$KB10/policy/logic-policy.dl"; then
  ok "#217: rules→empty resets dl to empty-policy stub (old rules dropped)"
else
  bad "#217: rules→empty left the OLD compiled rules in dl (silent stale-apply)"
fi
printf '%s' "$out10" | grep -qF "reset to empty policy" && ok "#217: rules→empty reset is surfaced (not silent)" || bad "#217: rules→empty reset was silent"

# --- #217 (symmetric): once reset to the stub, an in-sync re-run must be a no-op
# (benign stub + no-rules .md → no churn, no false 'stale' note).
rc10b=0; out10b="$("$PYTHON" "$FINALIZE" --target "$KB10" 2>&1)" || rc10b=$?
[ "$(cat "$KB10/policy/logic-policy.dl")" = "// no policy rules" ] && ok "#217: reset stub is stable on re-run (idempotent)" || bad "#217: reset stub churned on re-run"
printf '%s' "$out10b" | grep -qF "reset to empty policy" && bad "#217: benign stub re-run wrongly reported reset" || ok "#217: benign stub re-run is silent (no reset note)"

# --- #219 (gap 1): no-pyrewire `return-0` honest-summary path. When pyrewire is
# absent, finalize skips the logic check and exits 0 with an HONEST summary —
# "Logic check SKIPPED" (finalize.py ~236) and, for a policy that defines rules
# but did not compile, "policy is NOT applied" (finalize.py ~254). This machine's
# venv HAS pyrewire, so to hit that branch deterministically (independent of the
# real environment) we SHADOW pyrewire: a pyrewire.py that raises ImportError at
# import, prepended on PYTHONPATH, makes finalize._pyrewire_ok() (and every other
# consumer) treat the engine as absent. Paired with an uncompilable-but-has-rules
# policy (same `foo bar` shape as KB3/KB7), this pins the summary phrasing + rc=0.
SHADOW="$(mktemp -d)/pyrewire-shadow"
mkdir -p "$SHADOW"
printf 'raise ImportError("shadowed for #219 no-pyrewire finalize test")\n' > "$SHADOW/pyrewire.py"
# sanity: the shadow really disables pyrewire — otherwise the pins below are vacuous.
if PYTHONPATH="$SHADOW:$PYTHONPATH" "$PYTHON" -c "import sys; sys.path.insert(0, '$PLUGIN_ROOT/tools'); import finalize; raise SystemExit(0 if not finalize._pyrewire_ok() else 1)"; then
  ok "#219: pyrewire shadow makes _pyrewire_ok() False (no-pyrewire path armed)"
else
  bad "#219: pyrewire shadow did NOT disable pyrewire (no-pyrewire pins would be vacuous)"
fi
KB11="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB11" >/dev/null
printf '# src\n\nAcme API uses FastAPI.\n' > "$KB11/sources/a.md"
printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/a.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB11/runs/r1.json"
printf '# Logic policy\n\n## Rules\n\n- [c1] flag when `foo bar` occurs\n' > "$KB11/policy/logic-policy.md"
# Capture stdout and stderr SEPARATELY: the honest final summary prints to stdout
# (finalize.py ~236 SKIPPED, ~254-257 summary), while the "policy is NOT applied"
# WARNING prints to stderr (finalize.py ~192). Merging them (2>&1) would let the
# WARNING's "so the policy is NOT applied" substring satisfy the summary pin even
# if the honest summary were reworded/removed — a false pass. Pin the summary on
# stdout only, and match a phrase UNIQUE to the honest summary ("gate on the
# policy", finalize.py ~257) that the WARNING does not contain.
err11="$(mktemp)"
rc11=0; out11="$(PYTHONPATH="$SHADOW:$PYTHONPATH" "$PYTHON" "$FINALIZE" --target "$KB11" 2>"$err11")" || rc11=$?
# #336: the no-pyrewire skip is no longer indistinguishable from a verified pass — it
# exits 3 (distinct from a verified 0) so automation can tell an unverified compile apart.
[ "$rc11" -eq 3 ] && ok "#336: no-pyrewire finalize exits 3 (unverified skip, distinct from a verified 0)" || bad "#336: no-pyrewire finalize did not exit 3 (rc=$rc11)"
printf '%s' "$out11" | grep -qF "Logic check SKIPPED" && ok "#219: no-pyrewire summary notes 'Logic check SKIPPED'" || bad "#219: missing 'Logic check SKIPPED' note on no-pyrewire path"
# #336: the closing summary must not claim a bare "no contradictions" (which reads as
# engine-verified) when the engine never ran — it says only the single-valued check ran.
printf '%s' "$out11" | grep -qF "engine logic NOT run" && ok "#336: honest summary states the engine logic was NOT run" || bad "#336: summary hides that engine verification was skipped"
printf '%s' "$out11" | grep -qF "but the policy is NOT applied (see the WARNING above)" \
  && printf '%s' "$out11" | grep -qF "gate on the policy" \
  && ok "#219: no-pyrewire STDOUT summary stays honest ('policy is NOT applied' + 'gate on the policy')" \
  || bad "#219: honest no-pyrewire summary reworded/missing on stdout (WARNING-only substring must not count)"
# #356: --allow-unverified tolerates ENGINE ABSENCE, not a KB policy defect. KB11 has an
# uncompilable-but-has-rules policy (policy_uncompiled), so the policy is silently NOT
# applied — a correctness fault the flag must not wave through. It stays non-zero (rc 3)
# EVEN with --allow-unverified. (Before #356 this asserted rc 0, which encoded the bug:
# the flag swallowed the broken policy so CI running with it passed an unapplied policy.)
rc11b=0; PYTHONPATH="$SHADOW:$PYTHONPATH" "$PYTHON" "$FINALIZE" --target "$KB11" --allow-unverified >/dev/null 2>&1 || rc11b=$?
[ "$rc11b" -eq 3 ] && ok "#356: --allow-unverified does NOT accept a broken policy (rc 3 despite the flag)" || bad "#356: --allow-unverified swallowed policy_uncompiled (rc=$rc11b, want 3)"
rm -f "$err11"

# #356: the clean counterpart — a no-pyrewire KB with a VALID (no-rules) policy is exactly
# the engine-absence case the flag is for, so --allow-unverified keeps rc 0 there. This
# separates "accept engine absence" (rc 0) from "accept a policy defect" (rc 3 above) so
# the flag can never conflate the two.
KB11b="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB11b" >/dev/null
printf '# src\n\nAcme API uses FastAPI.\n' > "$KB11b/sources/a.md"
printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/a.md","status":"confirmed","confidence":0.95,"note":""}]' > "$KB11b/runs/r1.json"
# no logic-policy.md rules authored -> nothing to compile -> policy_uncompiled stays False.
rc11c=0; PYTHONPATH="$SHADOW:$PYTHONPATH" "$PYTHON" "$FINALIZE" --target "$KB11b" --allow-unverified >/dev/null 2>&1 || rc11c=$?
[ "$rc11c" -eq 0 ] && ok "#356: --allow-unverified keeps rc 0 on a no-pyrewire KB with a valid policy" || bad "#356: --allow-unverified did not restore rc 0 on a clean no-rules KB (rc=$rc11c)"

# --- #219 (gap 2): logic-policy.extra.dl interaction. A hand-authored typed
# comparison predicate (#120/#152 shape: arity-2 (entity: symbol, reason: symbol)
# head, quoted reason, scalar kept in the body) declared in logic-policy.extra.dl
# must be evaluated by finalize's logic check and surfaced under Policy Findings —
# and finalize must leave the hand-authored file byte-identical (it only ever
# regenerates logic-policy.dl, never .extra.dl). Engine-gated: the finding only
# materialises with pyrewire>=1.0.3 (the typed side-relation is projected into
# accepted.dl and the extra.dl rule is concatenated onto the loaded policy).
if "$PYTHON" -c "import pyrewire; raise SystemExit(0 if tuple(int(x) for x in pyrewire.__version__.split('.')[:3])>=(1,0,3) else 1)" >/dev/null 2>&1; then
  KB12="$(mktemp -d)/wiki"
  "$PYTHON" -m factlog init --target "$KB12" >/dev/null
  printf '# src\n\n을서비스 정식 운영.\n' > "$KB12/sources/a.md"
  printf '[{"subject":"을서비스","relation":"정식_운영","object":"date(2030,1)","source":"sources/a.md","status":"confirmed","confidence":0.9,"note":""}]' > "$KB12/runs/r1.json"
  printf -- '- `정식_운영` : date as launch_date\n' > "$KB12/policy/typed-relations.md"
  printf '%s\n%s\n' \
    '.decl after2030(entity: symbol, reason: symbol)' \
    'after2030(S, "launch_after_2030") :- launch_date(S, D), D >= 20300101.' \
    > "$KB12/policy/logic-policy.extra.dl"
  extra_before12="$($PYTHON -c "import hashlib,sys;print(hashlib.md5(open(sys.argv[1],'rb').read()).hexdigest())" "$KB12/policy/logic-policy.extra.dl")"
  rc12=0; out12="$("$PYTHON" "$FINALIZE" --target "$KB12" 2>&1)" || rc12=$?
  extra_after12="$($PYTHON -c "import hashlib,sys;print(hashlib.md5(open(sys.argv[1],'rb').read()).hexdigest())" "$KB12/policy/logic-policy.extra.dl")"
  [ "$rc12" -eq 0 ] && ok "#219: finalize with a hand-authored extra.dl completes (rc=0)" || bad "#219: finalize broke on a valid logic-policy.extra.dl (rc=$rc12)"
  [ "$extra_before12" = "$extra_after12" ] && ok "#219: logic-policy.extra.dl left byte-identical (finalize never regenerates it)" || bad "#219: finalize modified hand-authored logic-policy.extra.dl"
  findings12="$(sed -n '/Policy Findings:/,$p' "$KB12/facts/logic_report.txt" 2>/dev/null)"
  printf '%s' "$findings12" | grep -qF 'after2030: 을서비스 (launch_after_2030)' && ok "#219: extra.dl typed-comparison predicate is evaluated and surfaced in logic_report.txt" || bad "#219: extra.dl predicate not evaluated/surfaced under Policy Findings"
else
  echo "SKIP: pyrewire absent — skipping #219 extra.dl interaction (engine-gated)"
fi

# --- #496: a policy whose every bullet was REJECTED (an [id] tag with the backticks
# missing) is an authoring defect, not an empty policy. generate exits non-zero on it and
# validate reports it, but finalize asked only "does the .md have rules?" — False for this
# shape — and so took the benign-stub route: it wrote "// no policy rules" and exited 0.
# Two consequences, both pinned below: the state was STICKY (the stub satisfied the skip
# guard, so every later run stayed silent) and, worst, DESTRUCTIVE (a real compiled .dl was
# reset to the stub, announced as "logic-policy.md defines no rules", when an author merely
# dropped the backticks from a working rule).
#
# The #491 invariant it must not disturb: a .md with NO tagged bullet at all is a normal
# ruleless KB — rc and stub unchanged (case D) — and a partial policy still compiles its
# good rules (case E).
_496_kb() {  # $1 = kb path, $2 = logic-policy.md contents; seeds one confirmed fact
  "$PYTHON" -m factlog init --target "$1" >/dev/null 2>&1
  printf '# src\n\nAcme API uses FastAPI.\n' > "$1/sources/a.md"
  printf '[{"subject":"Acme API","relation":"uses","object":"FastAPI","source":"sources/a.md","status":"confirmed","confidence":0.95,"note":""}]' > "$1/runs/r1.json"
  [ -n "${2:-}" ] && printf '%s' "$2" > "$1/policy/logic-policy.md"
  return 0
}
REJECTED_MD='# Logic policy\n\n## Rules\n\n- [c1] 어떤 항목이 uses 관계를 가지면 검토(review)가 필요하다.\n'
GOOD_MD='# Logic policy\n\n## Rules\n\n- [c1] 어떤 항목이 `uses` 관계를 가지면 검토(review)가 필요하다.\n'

# (A) rejected-only -> non-zero, NO stub written, warning; and the re-run repeats all
# three (the old bug went quiet from run 2 on, which is how a KB stayed broken unnoticed).
KB13="$(mktemp -d)/wiki"
_496_kb "$KB13" "$(printf "$REJECTED_MD")"
rc13=0; out13="$("$PYTHON" "$FINALIZE" --target "$KB13" 2>&1)" || rc13=$?
[ "$rc13" -ne 0 ] && ok "#496(A): a policy with only rejected bullets exits non-zero (rc=$rc13)" || bad "#496(A): rejected-only policy exited 0 (silently unapplied)"
[ ! -f "$KB13/policy/logic-policy.dl" ] && ok "#496(A): no empty-policy stub written over a rejected policy" || bad "#496(A): stub written over a rejected policy (masks it from check + next finalize)"
printf '%s' "$out13" | grep -qF "NOT applied" && ok "#496(A): finalize warns the policy is NOT applied" || bad "#496(A): missing not-applied warning"
# Pin the phrasing that is UNIQUE to this branch, not just a substring generate's own
# stderr also carries: finalize routes an unusable generate result through a generic
# "generation failed" warning too, and if this case fell back to that one the rc and the
# absent .dl would be identical while the operator lost the one line that says what to
# type. Without this assertion that fallback is an undetectable mutation.
printf '%s' "$out13" | grep -qF "every policy bullet in policy/logic-policy.md was REJECTED" \
  && printf '%s' "$out13" | grep -qF "quote the relation name in backticks" \
  && ok "#496(A): the warning names the actual defect and the fix (missing backticks)" \
  || bad "#496(A): warning does not name the rejected-bullet defect or its remedy"
rc13b=0; out13b="$("$PYTHON" "$FINALIZE" --target "$KB13" 2>&1)" || rc13b=$?
[ "$rc13b" -ne 0 ] && ok "#496(A): re-run stays non-zero (not sticky-silent)" || bad "#496(A): re-run went to 0 (sticky masking regression)"
printf '%s' "$out13b" | grep -qF "NOT applied" && ok "#496(A): re-run re-warns" || bad "#496(A): re-run went silent"
[ ! -f "$KB13/policy/logic-policy.dl" ] && ok "#496(A): re-run still writes no stub" || bad "#496(A): re-run wrote a stub"
# the chain is NOT short-circuited: merge/compile artefacts still land, only the exit code
# reports the defect (same shape as the #194 loud path).
[ -s "$KB13/facts/candidates.csv" ] && [ -f "$KB13/facts/accepted.dl" ] && ok "#496(A): merge+compile artefacts still produced (no early return)" || bad "#496(A): the chain was cut short (candidates.csv/accepted.dl missing)"

# (B) the destructive transition: a WORKING compiled .dl must not be reset to the stub
# when the .md's backticks are dropped. Before the fix this printed "reset to empty policy
# (logic-policy.md defines no rules)" — a false statement — and exited 0.
KB14="$(mktemp -d)/wiki"
_496_kb "$KB14" "$(printf "$GOOD_MD")"
"$PYTHON" "$FINALIZE" --target "$KB14" >/dev/null 2>&1 || true
grep -q "requires_review" "$KB14/policy/logic-policy.dl" 2>/dev/null && ok "#496(B): real policy compiled (pre-condition)" || bad "#496(B): real policy did not compile (pre-condition)"
printf "$REJECTED_MD" > "$KB14/policy/logic-policy.md"
rc14=0; out14="$("$PYTHON" "$FINALIZE" --target "$KB14" 2>&1)" || rc14=$?
if [ ! -f "$KB14/policy/logic-policy.dl" ] || [ "$(cat "$KB14/policy/logic-policy.dl")" != "// no policy rules" ]; then
  ok "#496(B): breaking a working rule does not overwrite the compiled .dl with the stub"
else
  bad "#496(B): the compiled .dl was destroyed and replaced by the empty-policy stub"
fi
[ "$rc14" -ne 0 ] && ok "#496(B): the broken transition exits non-zero (rc=$rc14)" || bad "#496(B): the broken transition exited 0"
printf '%s' "$out14" | grep -qF "reset to empty policy" && bad "#496(B): finalize falsely claims 'logic-policy.md defines no rules'" || ok "#496(B): no false 'defines no rules' reset message"

# (C) migration: a KB already poisoned by the pre-fix finalize carries the masking stub on
# disk. has_rules is False for its .md, so the #194 self-heal never fired and the KB stayed
# silently broken forever. The widened heal must drop the stub and make it loud again.
KB15="$(mktemp -d)/wiki"
_496_kb "$KB15" "$(printf "$REJECTED_MD")"
printf '// no policy rules\n' > "$KB15/policy/logic-policy.dl"   # simulate the pre-fix stub
rc15=0; out15="$("$PYTHON" "$FINALIZE" --target "$KB15" 2>&1)" || rc15=$?
[ ! -f "$KB15/policy/logic-policy.dl" ] && ok "#496(C): a legacy masking stub over a rejected policy is healed away" || bad "#496(C): legacy stub kept (KB stays silently unapplied)"
[ "$rc15" -ne 0 ] && ok "#496(C): the healed KB now reports the defect (rc=$rc15)" || bad "#496(C): healed KB still exited 0"
printf '%s' "$out15" | grep -qF "NOT applied" && ok "#496(C): the healed KB warns (was silent before)" || bad "#496(C): healed KB stayed silent"

# (D) #491 invariant: a .md with no tagged bullet at all is a legitimately ruleless KB.
# rc and the empty-policy stub must be exactly what they were before this fix.
KB16="$(mktemp -d)/wiki"
_496_kb "$KB16" "$(printf '# Logic policy\n\n## Rules\n\nNo rules yet — just prose.\n')"
rc16=0; "$PYTHON" "$FINALIZE" --target "$KB16" >/dev/null 2>&1 || rc16=$?
[ "$rc16" -eq "$SKIP_RC" ] && ok "#496(D): a prose-only policy keeps its exit code ($SKIP_RC) — #491 unmoved" || bad "#496(D): prose-only policy changed rc to $rc16 (#491 regression)"
[ "$(cat "$KB16/policy/logic-policy.dl" 2>/dev/null)" = "// no policy rules" ] && ok "#496(D): a prose-only policy still gets the empty-policy .dl" || bad "#496(D): prose-only policy lost its empty-policy .dl"

# (E) a partial policy is still a policy (#491): one good bullet + one rejected bullet
# compiles the good rule, keeps rc, and names the rejected bullet on stderr.
KB17="$(mktemp -d)/wiki"
_496_kb "$KB17" "$(printf '# Logic policy\n\n## Rules\n\n- [c1] 어떤 항목이 `uses` 관계를 가지면 검토(review)가 필요하다.\n- [c2] 어떤 항목이 deployed_on 관계를 가지면 검토가 필요하다.\n')"
rc17=0; out17="$("$PYTHON" "$FINALIZE" --target "$KB17" 2>&1)" || rc17=$?
[ "$rc17" -eq "$SKIP_RC" ] && ok "#496(E): a mixed policy keeps its exit code ($SKIP_RC)" || bad "#496(E): mixed policy changed rc to $rc17 (partial policies must stay accepted)"
grep -q "requires_review" "$KB17/policy/logic-policy.dl" 2>/dev/null && ok "#496(E): the good rule in a mixed policy is compiled" || bad "#496(E): mixed policy did not compile its good rule"
printf '%s' "$out17" | grep -qF "ignored policy/logic-policy.md line" && ok "#496(E): the rejected bullet in a mixed policy is still named on stderr" || bad "#496(E): rejected bullet in a mixed policy went unreported"

# (F) recovery: adding the backticks back compiles the rule — no leftover stub, no
# leftover refusal. Runs on KB13, whose .md has been broken across two finalizes.
printf "$GOOD_MD" > "$KB13/policy/logic-policy.md"
rc13c=0; "$PYTHON" "$FINALIZE" --target "$KB13" >/dev/null 2>&1 || rc13c=$?
grep -q "requires_review" "$KB13/policy/logic-policy.dl" 2>/dev/null && ok "#496(F): fixing the bullet compiles the policy again (recovery)" || bad "#496(F): fixed policy did not compile"
[ "$rc13c" -eq "$SKIP_RC" ] && ok "#496(F): the recovered KB returns to its normal exit code ($SKIP_RC)" || bad "#496(F): recovered KB stuck at rc=$rc13c"

# (G) #356's rule extended: --allow-unverified tolerates ENGINE ABSENCE, never a KB policy
# defect. Under the pyrewire shadow the rejected-only KB must still exit 3 with the flag —
# otherwise CI that passes the flag for offline tolerance would wave a broken policy through.
KB18="$(mktemp -d)/wiki"
_496_kb "$KB18" "$(printf "$REJECTED_MD")"
rc18=0; PYTHONPATH="$SHADOW:$PYTHONPATH" "$PYTHON" "$FINALIZE" --target "$KB18" --allow-unverified >/dev/null 2>&1 || rc18=$?
[ "$rc18" -eq 3 ] && ok "#496(G): --allow-unverified does NOT swallow a rejected-only policy (rc 3)" || bad "#496(G): --allow-unverified swallowed the defect (rc=$rc18, want 3)"

echo ""
echo "========================================"
echo "test_finalize: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
