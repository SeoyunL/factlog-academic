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
printf '%s' "$ERR" | grep -q "could not read broken.json"   && ok "(i) a corrupt run file is warned about, not silently skipped"   || bad "(i) a corrupt run file was skipped silently"

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

echo
if [ "$fails" -eq 0 ]; then echo "accept durable: all passed"; else echo "accept durable: $fails failed"; exit 1; fi
