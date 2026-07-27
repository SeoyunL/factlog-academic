#!/usr/bin/env bash
# accept/reject must be durable: the decision has to reach runs/*.json, the source of
# truth merge rebuilds candidates.csv from. It used to write only candidates.csv, so
# deleting that file and re-merging silently downgraded an accepted fact to candidate --
# a human's decision lost with no warning (#233). amend already did this; accept/reject
# did not.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${FACTLOG_PY:-${PYTHON:-python3}}"
export PYTHONPATH="$PWD"
fails=0
ok() { echo "  ok: $1"; }
bad() { echo "FAIL: $1"; fails=$((fails+1)); }

status_of() {  # $1=kb $2=subject  -> status in runs/*.json
  FACTLOG_ROOT="$1" "$PY" -c "
import os, sys, json, glob
for f in glob.glob(os.path.join('$1','runs','*.json')):
    for it in json.load(open(f)):
        if it.get('subject')=='$2': print(it['status']); raise SystemExit
print('MISSING')"
}
csv_status() { grep "^$2," "$1/facts/candidates.csv" 2>/dev/null | head -1 | cut -d, -f5; }

new_kb() {
  local kb; kb="$(mktemp -d)/kb"
  "$PY" -m factlog init --target "$kb" >/dev/null
  printf 'a\n' > "$kb/sources/a.md"
  printf '[{"subject":"A","relation":"knows","object":"B","source":"sources/a.md","status":"candidate","confidence":0.9,"note":""},{"subject":"C","relation":"knows","object":"D","source":"sources/a.md","status":"candidate","confidence":0.9,"note":""}]' > "$kb/runs/r1.json"
  FACTLOG_ROOT="$kb" "$PY" tools/merge_candidates.py --wiki "$kb" >/dev/null 2>&1
  echo "$kb"
}

export XDG_CONFIG_HOME="$(mktemp -d)"

KB="$(new_kb)"
FACTLOG_ROOT="$KB" "$PY" -m factlog accept A knows B >/dev/null 2>&1
[ "$(status_of "$KB" A)" = "accepted" ] && ok "(a) accept writes the decision into runs/*.json" \
  || bad "(a) accept did not update runs/*.json"

# the durability payoff: delete candidates.csv, re-merge, decision survives
rm "$KB/facts/candidates.csv"
FACTLOG_ROOT="$KB" "$PY" tools/merge_candidates.py --wiki "$KB" >/dev/null 2>&1
[ "$(csv_status "$KB" A)" = "accepted" ] && ok "(b) the accept survives deleting candidates.csv and re-merging" \
  || bad "(b) the accept was silently downgraded on re-merge"

KB2="$(new_kb)"
FACTLOG_ROOT="$KB2" "$PY" -m factlog reject A knows B >/dev/null 2>&1
[ "$(status_of "$KB2" A)" = "superseded" ] && ok "(c) reject writes superseded into runs/*.json" \
  || bad "(c) reject did not update runs/*.json"

# reject must touch ONLY the pending match, not an already-accepted sibling
KB3="$(new_kb)"
FACTLOG_ROOT="$KB3" "$PY" -m factlog accept C knows D >/dev/null 2>&1
FACTLOG_ROOT="$KB3" "$PY" -m factlog reject C knows D >/dev/null 2>&1  # C is accepted now, not pending
[ "$(status_of "$KB3" C)" = "accepted" ] && ok "(d) reject leaves a non-pending row untouched in runs too" \
  || bad "(d) reject clobbered a non-pending row in runs/*.json"

# the run count is reported, not silent
OUT="$(FACTLOG_ROOT="$(new_kb)" "$PY" -m factlog accept A knows B 2>&1)"
printf '%s' "$OUT" | grep -q "runs/\*.json row(s) updated" && ok "(e) the run update is reported" \
  || bad "(e) the run update count is not reported"

# a WILDCARD reject that matches both a pending and an accepted row must flip only the
# pending one IN RUNS too -- this is what exercises the runs helper's own status filter
# (the CSV gate lets the call through because a pending match exists).
KB5="$(new_kb)"
FACTLOG_ROOT="$KB5" "$PY" -m factlog accept C knows D >/dev/null 2>&1   # C accepted, A still pending
FACTLOG_ROOT="$KB5" "$PY" -m factlog reject - knows - >/dev/null 2>&1   # wildcard: matches A (pending) and C (accepted)
[ "$(status_of "$KB5" A)" = "superseded" ] && ok "(g) a wildcard flips the pending row in runs"   || bad "(g) the pending row was not rejected in runs"
[ "$(status_of "$KB5" C)" = "accepted" ] && ok "(g) a wildcard leaves the accepted row untouched in runs"   || bad "(g) the wildcard clobbered an accepted row in runs"

# --dry-run writes nothing to runs either
KB4="$(new_kb)"
FACTLOG_ROOT="$KB4" "$PY" -m factlog accept A knows B --dry-run >/dev/null 2>&1
[ "$(status_of "$KB4" A)" = "candidate" ] && ok "(f) --dry-run does not touch runs/*.json" \
  || bad "(f) --dry-run wrote to runs/*.json"

# a run item merge treats as PENDING (blank/unknown status -> needs_review) must be
# flipped in runs too, or the decision vanishes on re-merge -- the same silent downgrade.
KB6="$(mktemp -d)/kb"
"$PY" -m factlog init --target "$KB6" >/dev/null
printf 'a\n' > "$KB6/sources/a.md"
printf '[{"subject":"A","relation":"knows","object":"B","source":"sources/a.md","confidence":0.9,"note":""}]' > "$KB6/runs/r1.json"
FACTLOG_ROOT="$KB6" "$PY" tools/merge_candidates.py --wiki "$KB6" >/dev/null 2>&1
FACTLOG_ROOT="$KB6" "$PY" -m factlog accept A knows B >/dev/null 2>&1
[ "$(status_of "$KB6" A)" = "accepted" ] && ok "(h) a blank-status run item (merge sees pending) is flipped in runs"   || bad "(h) a blank-status run item was left pending in runs"
rm "$KB6/facts/candidates.csv"
FACTLOG_ROOT="$KB6" "$PY" tools/merge_candidates.py --wiki "$KB6" >/dev/null 2>&1
[ "$(csv_status "$KB6" A)" = "accepted" ] && ok "(h) it survives re-merge"   || bad "(h) the blank-status accept was downgraded on re-merge"

# a corrupt run file is warned about, not silently skipped while accept reports success
KB7="$(mktemp -d)/kb"
"$PY" -m factlog init --target "$KB7" >/dev/null
printf 'a\n' > "$KB7/sources/a.md"
printf '[{"subject":"A","relation":"knows","object":"B","source":"sources/a.md","status":"candidate","confidence":0.9,"note":""}]' > "$KB7/runs/good.json"
FACTLOG_ROOT="$KB7" "$PY" tools/merge_candidates.py --wiki "$KB7" >/dev/null 2>&1
printf 'not json{' > "$KB7/runs/broken.json"
ERR="$(FACTLOG_ROOT="$KB7" "$PY" -m factlog accept A knows B 2>&1 >/dev/null)"
printf '%s' "$ERR" | grep -q "could not read broken.json to record the decision"   && ok "(i) a corrupt run file is warned about, not silently skipped"   || bad "(i) a corrupt run file was skipped silently"

# --- #563: a run file whose BYTES do not decode gets the same treatment -------------
# (i) above only covers a file that decodes but is not JSON, so it stays green even
# with UnicodeDecodeError missing from the except tuple. Crashing is not a safety net:
# candidates.csv is written BEFORE runs are touched, so the traceback lands a TORN
# write -- the decision in the CSV, never in runs/*.json -- and which way it goes is
# decided by the glob order of an unrelated file name. Pin all three: rc 0, the file
# named on stderr, and the decision still reaching the run files that DO read.
status_in() {  # $1=run file $2=subject -> that file's status for the subject
  "$PY" -c "
import json, sys
for it in json.load(open(sys.argv[1], encoding='utf-8')):
    if it.get('subject') == sys.argv[2]: print(it['status']); raise SystemExit
print('MISSING')" "$1" "$2"
}
undecodable_kb() {  # a merged KB, then an undecodable run file sorting BEFORE good.json
  local kb; kb="$(mktemp -d)/kb"
  "$PY" -m factlog init --target "$kb" >/dev/null
  printf 'a\n' > "$kb/sources/a.md"
  printf '[{"subject":"A","relation":"knows","object":"B","source":"sources/a.md","status":"candidate","confidence":0.9,"note":""}]' > "$kb/runs/good.json"
  FACTLOG_ROOT="$kb" "$PY" tools/merge_candidates.py --wiki "$kb" >/dev/null 2>&1
  printf '\377\376\000binary' > "$kb/runs/bin.json"
  echo "$kb"
}

KB11="$(undecodable_kb)"
ERR11="$(FACTLOG_ROOT="$KB11" "$PY" -m factlog accept A knows B 2>&1 >/dev/null)"; RC11=$?
[ "$RC11" -eq 0 ] && ok "(m) accept survives an undecodable run file (rc 0)" \
  || bad "(m) accept died on an undecodable run file (rc $RC11)"
printf '%s' "$ERR11" | grep -q "Traceback" && bad "(m) accept printed a traceback: $ERR11" \
  || ok "(m) accept printed no traceback"
printf '%s' "$ERR11" | grep -q "could not read bin.json to record the decision" \
  && ok "(m) the undecodable file is named on stderr" \
  || bad "(m) the undecodable file was skipped silently: $ERR11"
# The CONSEQUENCE clause is pinned too, not just the prefix. It is what docs/ and
# SKILL.md quote as "what the warning says" and build a paragraph on, so deleting it
# would gut the user-facing half of this fix while every other assertion here stayed
# green. "can take", not "would take": measured, the stale row only wins a rebuild
# when it sorts first (an unreadable zzz_bin.json next to a readable aaa_good.json
# that took the decision rebuilds as `accepted`), so the guarantee is conditional and
# the wording names merge's actual rule instead.
printf '%s' "$ERR11" | grep -q "keeps its old status" \
  && printf '%s' "$ERR11" | grep -q "rebuilt from runs/\*.json alone can take that old status" \
  && printf '%s' "$ERR11" | grep -q "whichever run file comes first in glob order" \
  && ok "(m) the warning states the consequence, conditionally" \
  || bad "(m) the warning lost or overstated its consequence clause: $ERR11"
# The remedy clause (#566) is pinned in the SAME conditional shape. accept/reject may
# name repair-runs because the recovery is real, but reaching that row is CONDITIONAL --
# it must still be pending once the file reads again, its candidates.csv rows must be
# unambiguous, and it must have a source file. So the sentence is pinned with its own
# limit ("any row that file still holds as pending"), not as "run this to fix it": an
# unconditional promise here is the same class of claim as the "re-run after fixing"
# text this warning replaced. Block (p) below is the measurement behind it.
printf '%s' "$ERR11" | grep -q "factlog repair-runs" \
  && printf '%s' "$ERR11" | grep -q "any row that file still holds as pending" \
  && ok "(m) the warning names the recovery command, with its condition attached" \
  || bad "(m) the recovery clause is missing or lost its limit: $ERR11"
[ "$(status_in "$KB11/runs/good.json" A)" = "accepted" ] \
  && ok "(m) the decision still reached the readable run file" \
  || bad "(m) the decision never reached the readable run file"

KB12="$(undecodable_kb)"
ERR12="$(FACTLOG_ROOT="$KB12" "$PY" -m factlog reject A knows B 2>&1 >/dev/null)"; RC12=$?
[ "$RC12" -eq 0 ] && ok "(m) reject survives an undecodable run file (rc 0)" \
  || bad "(m) reject died on an undecodable run file (rc $RC12)"
printf '%s' "$ERR12" | grep -q "Traceback" && bad "(m) reject printed a traceback: $ERR12" \
  || ok "(m) reject printed no traceback"
printf '%s' "$ERR12" | grep -q "could not read bin.json to record the decision" \
  && ok "(m) reject names the undecodable file on stderr" \
  || bad "(m) reject skipped the undecodable file silently: $ERR12"
[ "$(status_in "$KB12/runs/good.json" A)" = "superseded" ] \
  && ok "(m) the rejection still reached the readable run file" \
  || bad "(m) the rejection never reached the readable run file"

# the "can take ... whichever comes first in glob order" clause above is a claim about
# merge, so pin the behaviour it rests on: the SAME repair loses or survives purely by
# the unreadable file's name. If merge's tie-break ever stops being glob order, this
# goes red and the warning has to be reworded with it.
glob_order_case() {  # $1 = name of the unreadable file -> rebuilt status
  local kb row; kb="$(mktemp -d)/kb"
  "$PY" -m factlog init --target "$kb" >/dev/null
  printf 'a\n' > "$kb/sources/a.md"
  row='[{"subject":"A","relation":"knows","object":"B","source":"sources/a.md","status":"candidate","confidence":0.9,"note":""}]'
  printf '%s' "$row" > "$kb/runs/aaa_good.json"
  printf '%s' "$row" > "$kb/runs/$1"
  FACTLOG_ROOT="$kb" "$PY" tools/merge_candidates.py --wiki "$kb" >/dev/null 2>&1
  printf '\377\376\000binary' > "$kb/runs/$1"
  FACTLOG_ROOT="$kb" "$PY" -m factlog accept A knows B >/dev/null 2>&1
  printf '%s' "$row" > "$kb/runs/$1"          # user restores pre-decision contents
  rm "$kb/facts/candidates.csv"               # rebuild from runs/*.json alone
  FACTLOG_ROOT="$kb" "$PY" tools/merge_candidates.py --wiki "$kb" >/dev/null 2>&1
  csv_status "$kb" A
}
[ "$(glob_order_case aaa_bin.json)" = "candidate" ] \
  && ok "(m) a stale row sorting FIRST does take over a from-scratch rebuild" \
  || bad "(m) the stale row did not win despite sorting first -- reword the warning"
[ "$(glob_order_case zzz_bin.json)" = "accepted" ] \
  && ok "(m) sorting later, it does not -- which is why the warning says 'can', not 'will'" \
  || bad "(m) the decision was lost even though the stale row sorted later"

# --- #566: the warning must not promise a repair ACCEPT does not perform -----------
# It used to say "re-run after fixing the file". Measured: after the run above the CSV
# row is no longer pending, so the same command answers "nothing to change" and the
# restored file's row stays `candidate` -- the decision never arrives. Pin the absence
# of the promise AND the behaviour that makes it false, so nobody re-adds the text.
# These assertions pin behaviour that STAYS defective on purpose, and they survive the
# #566 recovery path unchanged: that path is a SEPARATE command (`repair-runs`, block
# (o) below), not a new side effect of re-running accept. Teaching accept to reconcile
# two drifted stores is the design its own docstring refuses -- a wildcard would then
# reach rows the gate reported as "non-pending skipped" and silently retire a confirmed
# fact (#477). Do not "fix" these by making a re-run work.
printf '%s' "$ERR11" | grep -q "re-run after fixing" \
  && bad "(n) the warning promises a re-run remedy the CLI does not perform: $ERR11" \
  || ok "(n) the warning makes no re-run promise"
# the user "fixes" bin.json back to its pre-decision contents, as the old text invited
printf '[{"subject":"A","relation":"knows","object":"B","source":"sources/a.md","status":"candidate","confidence":0.9,"note":""}]' > "$KB11/runs/bin.json"
OUT_N="$(FACTLOG_ROOT="$KB11" "$PY" -m factlog accept A knows B 2>&1)"
printf '%s' "$OUT_N" | grep -q "not pending" \
  && ok "(n) re-running the same command changes nothing (why the promise was false)" \
  || bad "(n) re-running behaved differently than measured: $OUT_N"
[ "$(status_in "$KB11/runs/bin.json" A)" = "candidate" ] \
  && ok "(n) the repaired file's row is still NOT reached by a re-run" \
  || bad "(n) a re-run did reach the repaired file -- re-check the docs' claim"

# --- #566: what DOES reach the repaired file's row ---------------------------------
# End-to-end recovery, on the very KB block (n) left stranded: an unreadable run file at
# decision time, the file restored afterwards, and a candidates.csv row that is no longer
# pending. `repair-runs` does not decide anything -- it compares the two stores -- so a
# CSV row already carrying the decision is precisely its input, where it is accept's dead
# end. This is the loop #563 could not close and #566 opened.
OUT_P1="$(FACTLOG_ROOT="$KB11" "$PY" -m factlog repair-runs A knows B 2>&1)"; RC_P1=$?
[ "$RC_P1" -eq 3 ] \
  && ok "(p) reporting alone finds the drift and says so with exit code 3" \
  || bad "(p) the report did not signal drift (rc $RC_P1): $OUT_P1"
[ "$(status_in "$KB11/runs/bin.json" A)" = "candidate" ] \
  && ok "(p) reporting wrote nothing (there is no --dry-run because this IS the default)" \
  || bad "(p) the report mode wrote to runs/*.json"
OUT_P2="$(FACTLOG_ROOT="$KB11" "$PY" -m factlog repair-runs A knows B --apply 2>&1)"; RC_P2=$?
[ "$RC_P2" -eq 0 ] \
  && ok "(p) --apply exits 0 once every drifted row is repaired" \
  || bad "(p) --apply did not report a clean repair (rc $RC_P2): $OUT_P2"
[ "$(status_in "$KB11/runs/bin.json" A)" = "accepted" ] \
  && ok "(p) the decision finally reaches the repaired file's row" \
  || bad "(p) repair-runs did not reach the repaired file's row"
# The write is not the payoff. merge settles a fact claimed by two run files by glob
# order, not by status, and bin.json sorts BEFORE good.json -- which is exactly how block
# (m) showed the decision being lost. Only a from-scratch rebuild proves it now survives.
rm "$KB11/facts/candidates.csv"
FACTLOG_ROOT="$KB11" "$PY" tools/merge_candidates.py --wiki "$KB11" >/dev/null 2>&1
[ "$(csv_status "$KB11" A)" = "accepted" ] \
  && ok "(p) and it survives a candidates.csv rebuilt from runs/*.json alone" \
  || bad "(p) the repaired decision was still lost on a from-scratch rebuild"

# merge itself still refuses this KB, which is why accept must not rely on merge as a
# backstop: while the undecodable file is there, candidates.csv is never rebuilt.
# Pin ONLY the failure and the untouched CSV. Do NOT pin a traceback or a message
# shape: load_candidate_files raising raw instead of its intended SystemExit is a
# separate follow-up, and pinning the current form would force that fix to edit this.
KB13="$(undecodable_kb)"
BEFORE13="$(cat "$KB13/facts/candidates.csv")"
FACTLOG_ROOT="$KB13" "$PY" tools/merge_candidates.py --wiki "$KB13" >/dev/null 2>&1; RC13=$?
[ "$RC13" -ne 0 ] && ok "(m) merge still fails on an undecodable run file" \
  || bad "(m) merge unexpectedly succeeded on an undecodable run file"
[ "$(cat "$KB13/facts/candidates.csv")" = "$BEFORE13" ] \
  && ok "(m) the failed merge left candidates.csv untouched" \
  || bad "(m) the failed merge rewrote candidates.csv"

# --- #477: a decision must not retire a CONFIRMED fact through runs/*.json ----------
# A KB predating #233 holds the human decision in candidates.csv while runs/*.json still
# says `candidate`. If reject writes its decision into run rows it did not decide, the
# next merge rebuilds candidates.csv FROM those rows and the confirmed fact drops out of
# accepted.dl -- the engine silently loses it. Full path: merge -> confirm -> reject ->
# re-merge -> compile_facts -> the confirmed fact must still be in accepted.dl.
confirm_in_csv() {  # $1=kb $2=subject -- mark the row confirmed, leaving runs drifted
  "$PY" - "$1" "$2" <<'PYEOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1]) / "facts" / "candidates.csv"
out = []
for line in p.read_text(encoding="utf-8").splitlines(True):
    parts = line.split(",")
    if parts[0] == sys.argv[2] and len(parts) > 4 and parts[4] in ("candidate", "needs_review"):
        parts[4] = "confirmed"
        line = ",".join(parts)
    out.append(line)
p.write_text("".join(out), encoding="utf-8")
PYEOF
}
remerge_and_compile() {  # $1=kb
  FACTLOG_ROOT="$1" "$PY" tools/merge_candidates.py --wiki "$1" >/dev/null 2>&1
  FACTLOG_ROOT="$1" "$PY" tools/compile_facts.py >/dev/null 2>&1
}

# (j) single source: a wildcard reject alongside a drifted confirmed row
KB8="$(new_kb)"                       # A knows B and C knows D, both from sources/a.md
confirm_in_csv "$KB8" A
FACTLOG_ROOT="$KB8" "$PY" -m factlog reject - knows - >/dev/null 2>&1  # only C is pending
remerge_and_compile "$KB8"
[ "$(csv_status "$KB8" A)" = "confirmed" ] && ok "(j) a confirmed row survives a wildcard reject + re-merge" \
  || bad "(j) the confirmed row was retired by a wildcard reject (#477)"
grep -q 'relation("A", "knows", "B")' "$KB8/facts/accepted.dl" && ok "(j) the confirmed fact is still engine input" \
  || bad "(j) the confirmed fact vanished from accepted.dl (#477)"
[ "$(csv_status "$KB8" C)" = "superseded" ] && ok "(j) the pending row was still rejected" \
  || bad "(j) the pending row was not rejected"

# (k) multi source: the SAME triple from two sources, exact (non-wildcard) triple
KB9="$(mktemp -d)/kb"
"$PY" -m factlog init --target "$KB9" >/dev/null
printf 'n1\n' > "$KB9/sources/note1.md"
printf 'n2\n' > "$KB9/sources/note2.md"
printf '[{"subject":"A","relation":"knows","object":"B","source":"sources/note1.md","status":"candidate","confidence":0.9,"note":""},{"subject":"A","relation":"knows","object":"B","source":"sources/note2.md","status":"candidate","confidence":0.9,"note":""}]' > "$KB9/runs/r1.json"
FACTLOG_ROOT="$KB9" "$PY" tools/merge_candidates.py --wiki "$KB9" >/dev/null 2>&1
"$PY" - "$KB9" <<'PYEOF'
import pathlib, sys
p = pathlib.Path(sys.argv[1]) / "facts" / "candidates.csv"
p.write_text(
    p.read_text(encoding="utf-8").replace("sources/note1.md,candidate", "sources/note1.md,confirmed"),
    encoding="utf-8",
)
PYEOF
OUT9="$(FACTLOG_ROOT="$KB9" "$PY" -m factlog reject A knows B 2>&1)"
printf '%s' "$OUT9" | grep -q "1 candidate row(s) → superseded, 1 runs/\*.json row(s) updated" \
  && ok "(k) only the decided source's run row is reported as updated" \
  || bad "(k) the run count exceeded the rows actually decided (#477): $(printf '%s' "$OUT9" | grep 'runs/')"
run_status_for_source() {  # $1=kb $2=source
  "$PY" -c "
import glob, json, os, sys
for f in glob.glob(os.path.join(sys.argv[1],'runs','*.json')):
    for it in json.load(open(f)):
        if it.get('source')==sys.argv[2]: print(it['status']); raise SystemExit
print('MISSING')" "$1" "$2"
}
[ "$(run_status_for_source "$KB9" sources/note1.md)" = "candidate" ] \
  && ok "(k) the other source's run row is left alone" \
  || bad "(k) a decision on one source flipped another source's run row (#477)"
remerge_and_compile "$KB9"
grep -q 'sources/note1.md,confirmed' "$KB9/facts/candidates.csv" \
  && ok "(k) the confirmed multi-source row survives re-merge" \
  || bad "(k) the confirmed multi-source row was retired on re-merge (#477)"
grep -q 'relation("A", "knows", "B")' "$KB9/facts/accepted.dl" \
  && ok "(k) the confirmed fact is still engine input" \
  || bad "(k) the confirmed fact vanished from accepted.dl (#477)"

# (l) an amount object: merge canonicalises it to `amount(N,"unit")` before keying, so a
# run row still holding the bare or comma-grouped form is the SAME fact. Comparing the
# two verbatim left the decision out of runs/*.json, and the #233 downgrade came back.
KB10="$(mktemp -d)/kb"
"$PY" -m factlog init --target "$KB10" >/dev/null
printf 'n\n' > "$KB10/sources/a.md"
printf '[{"subject":"A","relation":"costs","object":"amount(7,\xec\x96\xb5)","source":"sources/a.md","status":"candidate","confidence":0.9,"note":""}]' > "$KB10/runs/r1.json"
FACTLOG_ROOT="$KB10" "$PY" tools/merge_candidates.py --wiki "$KB10" >/dev/null 2>&1
OUT10="$(FACTLOG_ROOT="$KB10" "$PY" -m factlog accept A costs 'amount(7,"억")' 2>&1)"
printf '%s' "$OUT10" | grep -q "1 runs/\*.json row(s) updated" \
  && ok "(l) an amount decision reaches the run row holding the bare form" \
  || bad "(l) the amount run row was not updated (#477): $(printf '%s' "$OUT10" | grep 'runs/')"
[ "$(status_of "$KB10" A)" = "accepted" ] && ok "(l) the run row is accepted" \
  || bad "(l) the amount run row kept its pending status"
rm "$KB10/facts/candidates.csv"          # the durability payoff, on an amount fact
remerge_and_compile "$KB10"
grep -q 'relation("A", "costs", "amount(7,' "$KB10/facts/accepted.dl" \
  && ok "(l) the amount fact survives re-merge and is engine input" \
  || bad "(l) the amount accept was downgraded on re-merge (#233 regression)"

# --- #565: `amend --accept` must be as durable as `accept` --------------------------
# Two ways to say "promote this fact", one outcome. amend wrote the promotion to
# candidates.csv only, so the two commands agreed for as long as that file existed and
# split the moment it was rebuilt from runs/*.json alone -- `accept` held, `amend
# --accept` fell back to candidate. Same seed, same payoff, compared directly: an
# equivalence is what keeps the two paths from drifting again, since each one on its own
# looks fine right up until the rebuild.
KB14="$(new_kb)"
KB15="$(new_kb)"
FACTLOG_ROOT="$KB14" "$PY" -m factlog accept A knows B >/dev/null 2>&1
FACTLOG_ROOT="$KB15" "$PY" -m factlog amend A knows B --accept >/dev/null 2>&1
rm "$KB14/facts/candidates.csv" "$KB15/facts/candidates.csv"   # rebuild from runs alone
FACTLOG_ROOT="$KB14" "$PY" tools/merge_candidates.py --wiki "$KB14" >/dev/null 2>&1
FACTLOG_ROOT="$KB15" "$PY" tools/merge_candidates.py --wiki "$KB15" >/dev/null 2>&1
S14="$(csv_status "$KB14" A)"; S15="$(csv_status "$KB15" A)"
[ "$S14" = "$S15" ] && ok "(o) accept and amend --accept agree after a from-scratch rebuild" \
  || bad "(o) the two promotion paths disagree (#565): accept=$S14 amend=$S15"
[ "$S15" = "accepted" ] && ok "(o) and both land on accepted, not a downgrade they share" \
  || bad "(o) amend --accept was downgraded on a from-scratch rebuild (#565): $S15"

echo
if [ "$fails" -eq 0 ]; then echo "accept durable: all passed"; else echo "accept durable: $fails failed"; exit 1; fi
