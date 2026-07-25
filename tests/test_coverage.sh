#!/usr/bin/env bash
# tests/test_coverage.sh — source coverage critic (#36)
#
# Pins:
#   - a source cited by >=1 ENGINE fact reports its count; 0-fact sources are gaps
#   - only engine-input rows count: a source backed solely by superseded /
#     needs_review rows is a gap, not "covered"
#   - TEXT gap vs BINARY gap distinguished; a binary under runs/sources/ gets a
#     distinct "ingest output should be text" message (not "run ingest")
#   - a fact citing a non-existent source file is reported as an ORPHAN
#   - default run is informational (exit 0) — even with no candidates.csv and on
#     an empty KB; --strict exits non-zero ONLY when a TEXT source is uncovered
#   - counting spans sources/ and runs/sources/; the '#anchor' is ignored
#
# Deterministic; no pyrewire.  Usage: bash tests/test_coverage.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
COV="$PLUGIN_ROOT/tools/source_coverage.py"
HEADER="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
csv() { printf '%s\n' "$HEADER" "$@" > "$KB/facts/candidates.csv"; }
run() { set +e; out="$("$PYTHON" "$COV" --wiki "$KB" "$@" 2>&1)"; rc=$?; set -e; }

# --- empty KB (init scaffolds an empty sources/, no candidates.csv) -----------
rm -f "$KB/facts/candidates.csv"
run
[ "$rc" -eq 0 ] && ok "missing candidates.csv exits 0 (not a hard fail)" || bad "missing CSV exit $rc"
printf '%s' "$out" | grep -qF "coverage: no source files" && ok "empty KB reports no source files" || bad "empty KB message missing"

# --- main matrix --------------------------------------------------------------
# a.md cited (via #anchor), b.md text-but-uncited, c.docx binary-uncited,
# d.md under runs/sources cited.
printf 'a content\n' > "$KB/sources/a.md"
printf 'b content\n' > "$KB/sources/b.md"
printf '\x00\x01bin\x00'  > "$KB/sources/c.docx"
mkdir -p "$KB/runs/sources"
printf 'd content\n' > "$KB/runs/sources/d.md"
csv \
  '갑봇,통합,을서비스,sources/a.md#sec,accepted,0.9,' \
  '구성_요소,포함,주_속성,runs/sources/d.md,accepted,0.9,'
run
[ "$rc" -eq 0 ] && ok "default run exits 0 (informational)" || bad "default exit $rc"
printf '%s' "$out" | grep -qE "1 fact\(s\): sources/a.md" && ok "cited source reports count (anchor stripped)" || bad "a.md count missing"
printf '%s' "$out" | grep -qE "1 fact\(s\): runs/sources/d.md" && ok "runs/sources cited source counted" || bad "d.md count missing"
printf '%s' "$out" | grep -qF "GAP (text, run /factlog sync): sources/b.md" && ok "uncited text source flagged" || bad "b.md text gap missing"
printf '%s' "$out" | grep -qF "GAP (binary, run factlog ingest): sources/c.docx" && ok "uncited binary under sources flagged" || bad "c.docx binary gap missing"
printf '%s' "$out" | grep -qF "4 source(s); 2 covered, 1 text gap(s), 1 binary needing conversion, 0 orphan citation(s)" && ok "summary tallies" || bad "summary tally wrong"

# --strict fails on the text gap.
run --strict
[ "$rc" -ne 0 ] && ok "--strict exits non-zero on text gap" || bad "--strict did not fail on text gap"

# --- engine-only counting: superseded / needs_review do NOT cover -------------
# b.md cited only by a superseded row, c.md only by needs_review -> both gaps.
rm -f "$KB/sources/c.docx"
printf 'c content\n' > "$KB/sources/c.md"
csv \
  '갑봇,통합,을서비스,sources/a.md,accepted,0.9,' \
  '구성_요소,포함,주_속성,runs/sources/d.md,accepted,0.9,' \
  '값가,대체,값나,sources/b.md,superseded,0.9,' \
  '항목,후보,자료,sources/c.md,needs_review,0.5,'
run
printf '%s' "$out" | grep -qF "GAP (text, run /factlog sync): sources/b.md" && ok "superseded-only source is a gap" || bad "superseded source falsely covered"
printf '%s' "$out" | grep -qF "GAP (text, run /factlog sync): sources/c.md" && ok "needs_review-only source is a gap" || bad "needs_review source falsely covered"
printf '%s' "$out" | grep -qE "0 fact\(s\): sources/b.md" && ok "superseded source counts 0 engine facts" || bad "superseded counted as fact"

# --- binary under runs/sources/ gets the distinct anomaly message -------------
printf '\x00\x01bin\x00' > "$KB/runs/sources/conv.pdf"
csv '갑봇,통합,을서비스,sources/a.md,accepted,0.9,' '구성_요소,포함,주_속성,runs/sources/d.md,accepted,0.9,'
rm -f "$KB/sources/b.md" "$KB/sources/c.md"
run
printf '%s' "$out" | grep -qF "GAP (binary under runs/sources — ingest output should be text): runs/sources/conv.pdf" && ok "binary under runs/sources gets anomaly message" || bad "runs/sources binary message wrong"
rm -f "$KB/runs/sources/conv.pdf"

# --- orphan citation: fact cites a file that does not exist -------------------
csv '갑봇,통합,을서비스,sources/a.md,accepted,0.9,' '유령,참조,대상,sources/ghost.md,accepted,0.9,'
run
printf '%s' "$out" | grep -qF "ORPHAN citation (source file missing): sources/ghost.md" && ok "orphan citation reported" || bad "orphan citation missing"
printf '%s' "$out" | grep -qF "1 orphan citation(s)" && ok "orphan count in summary" || bad "orphan count missing"
[ "$rc" -eq 0 ] && ok "orphan alone does not fail default run" || bad "orphan caused non-zero exit"

# --- all text sources covered -> --strict clean -------------------------------
csv '갑봇,통합,을서비스,sources/a.md,accepted,0.9,' '구성_요소,포함,주_속성,runs/sources/d.md,accepted,0.9,'
run --strict
[ "$rc" -eq 0 ] && ok "--strict clean when all text sources covered" || bad "--strict false-positive"

# --- binary original paired with its conversion is "covered via conversion" ---
PAIRKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$PAIRKB" >/dev/null
printf '\x00\x01bin\x00' > "$PAIRKB/sources/report.pdf"        # binary original (facts attach to its conversion)
printf 'converted text\n' > "$PAIRKB/runs/sources/report.txt"  # its text conversion
printf '\x00\x01bin\x00' > "$PAIRKB/sources/lonely.pdf"        # binary with NO conversion -> still a gap
printf '\x00\x01bin\x00' > "$PAIRKB/sources/empty.pdf"         # converted but conversion has 0 facts
printf 'nothing extracted\n' > "$PAIRKB/runs/sources/empty.txt"
printf '%s\n%s\n' "$HEADER" \
  '갑봇,통합,을서비스,runs/sources/report.txt,accepted,0.9,' > "$PAIRKB/facts/candidates.csv"
pout="$("$PYTHON" "$COV" --wiki "$PAIRKB" 2>&1)"
printf '%s' "$pout" | grep -qF "covered via runs/sources/report.txt: 1 fact(s)" && ok "binary original covered via its conversion" || bad "pairing not shown: $pout"
printf '%s' "$pout" | grep -qE 'GAP .*report\.pdf' && bad "paired binary still flagged as a gap" || ok "paired binary is not a binary gap"
printf '%s' "$pout" | grep -qF "GAP (binary, run factlog ingest): sources/lonely.pdf" && ok "unconverted binary is still a gap" || bad "unconverted binary not flagged"
printf '%s' "$pout" | grep -qF "(1 via conversion)" && ok "summary notes the 'via conversion' count" || bad "via-conversion summary note missing"
# a binary whose conversion exists but has 0 facts is not a binary gap; the empty
# conversion surfaces as its own text gap instead.
printf '%s' "$pout" | grep -qF "converted → runs/sources/empty.txt (0 facts" && ok "empty conversion: binary shown converted, not a binary gap" || bad "empty-conversion binary mishandled"
printf '%s' "$pout" | grep -qE 'GAP .*empty\.pdf' && bad "converted-but-empty binary wrongly a binary gap" || ok "converted-but-empty binary not a binary gap"
printf '%s' "$pout" | grep -qF "GAP (text, run /factlog sync): runs/sources/empty.txt" && ok "empty conversion surfaces as a text gap" || bad "empty conversion not a text gap"

# a stray BINARY under runs/sources/ is an anomaly, not a usable conversion: it
# must not pair with (and thus mask the gap on) a same-stem binary original.
printf '\x00\x01bin\x00' > "$PAIRKB/sources/doc.pdf"
printf '\x00\x01bin\x00' > "$PAIRKB/runs/sources/doc.bin"
aout="$("$PYTHON" "$COV" --wiki "$PAIRKB" 2>&1)"
printf '%s' "$aout" | grep -qF "GAP (binary, run factlog ingest): sources/doc.pdf" && ok "binary original not masked by a same-stem binary in runs/sources" || bad "anomalous binary masked a real gap"
printf '%s' "$aout" | grep -qF "GAP (binary under runs/sources — ingest output should be text): runs/sources/doc.bin" && ok "stray binary under runs/sources still flagged as anomaly" || bad "runs/sources binary anomaly missing"

# --- same stem in different subdirs each pair to THEIR OWN conversion ----------
# (ingest now mirrors subdirs; coverage pairs by subdir-aware rel key)
SUBKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$SUBKB" >/dev/null
mkdir -p "$SUBKB/sources/a" "$SUBKB/sources/b" "$SUBKB/runs/sources/a" "$SUBKB/runs/sources/b"
printf '\x00\x01bin\x00' > "$SUBKB/sources/a/report.pdf"
printf '\x00\x01bin\x00' > "$SUBKB/sources/b/report.pdf"
printf 'a text\n' > "$SUBKB/runs/sources/a/report.md"
printf 'b text\n' > "$SUBKB/runs/sources/b/report.md"
printf '%s\n%s\n%s\n' "$HEADER" \
  '갑봇,통합,을서비스,runs/sources/a/report.md,accepted,0.9,' \
  '값가,대체,값나,runs/sources/b/report.md,accepted,0.9,' > "$SUBKB/facts/candidates.csv"
sout="$("$PYTHON" "$COV" --wiki "$SUBKB" 2>&1)"
printf '%s' "$sout" | grep -qF "covered via runs/sources/a/report.md" && ok "subdir a binary pairs to a/ conversion" || bad "subdir a mispaired: $sout"
printf '%s' "$sout" | grep -qF "covered via runs/sources/b/report.md" && ok "subdir b binary pairs to b/ conversion (no stem collision)" || bad "subdir b mispaired"
printf '%s' "$sout" | grep -qE 'GAP .*report\.pdf' && bad "nested binary wrongly flagged as gap" || ok "nested binaries not flagged as gaps"

# --- NFC/NFD: an NFD-named source cited NFC is covered, not orphan (#64) ------
NFCKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$NFCKB" >/dev/null
FACTLOG_NFCKB="$NFCKB" "$PYTHON" - <<'PY'
import os, unicodedata
kb = os.environ["FACTLOG_NFCKB"]
nfd = unicodedata.normalize("NFD", "각노트.md")
open(os.path.join(kb, "sources", nfd), "w", encoding="utf-8").write("내용\n")   # NFD on disk
H = "subject,relation,object,source,status,confidence,note\n"
open(os.path.join(kb, "facts", "candidates.csv"), "w", encoding="utf-8").write(
    H + "갑봇,포함,값가,sources/각노트.md,accepted,0.9,\n")               # NFC citation
PY
nout="$("$PYTHON" "$COV" --wiki "$NFCKB" 2>&1)"
printf '%s' "$nout" | grep -qE "1 fact\(s\): sources/각노트.md" && ok "NFD-named source cited NFC is covered (not 0)" || bad "NFD source shows 0 facts"
printf '%s' "$nout" | grep -qF "ORPHAN" && bad "NFD source falsely reported as orphan" || ok "no orphan false-positive for NFD-named source"

# --- run rows citing a deleted source, with candidates.csv already clean (#558)
# The state a KB settles into after the first merge past a source deletion: the
# rows are gone from candidates.csv (so the orphan axis is silent) but still sit
# in runs/*.json, dropped and warned about on every merge from now on.
RUNKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$RUNKB" >/dev/null
printf 'live\n' > "$RUNKB/sources/live.md"
printf '%s\n%s\n' "$HEADER" \
  '갑봇,통합,을서비스,sources/live.md,accepted,0.9,' > "$RUNKB/facts/candidates.csv"
runjson() { printf '%s' "$1" > "$RUNKB/runs/$2"; }
# One run file citing only the live source: the baseline this KB reports today.
runjson '[{"subject":"갑봇","relation":"통합","object":"을서비스","source":"sources/live.md","status":"accepted","confidence":"0.9","note":""}]' "2026-01-01-live.json"
set +e; rout="$("$PYTHON" "$COV" --wiki "$RUNKB" 2>&1)"; rrc=$?; set -e
# BYTE-identical to the pre-#558 summary line: the new field is omitted at 0, so
# no existing reader or grep sees a changed line on a KB with nothing to report.
printf '%s' "$rout" | grep -qF "coverage: 1 source(s); 1 covered, 0 text gap(s), 0 binary needing conversion, 0 orphan citation(s)" \
  && ok "no run orphans: summary line unchanged" || bad "summary line changed with 0 run orphans: $rout"
printf '%s' "$rout" | grep -qF "run-cited source(s) missing" && bad "run-orphan field printed at 0" || ok "run-orphan field omitted at 0"
printf '%s' "$rout" | grep -qF "RUN ROWS cite" && bad "run-orphan line printed at 0" || ok "no run-orphan stderr line at 0"
# The OTHER half of "omitted at 0": the summary hint. Asserting only the per-source
# lines left the early return in report_run_orphans unpinned — deleting it still
# passed while the tool printed `run rows cite 0 missing source(s) (0 row(s) total)`
# on every clean KB.
printf '%s' "$rout" | grep -qF "run rows cite" && bad "remedy hint printed at 0" || ok "remedy hint omitted at 0"
[ "$rrc" -eq 0 ] && ok "clean run files exit 0" || bad "clean run files exit $rrc"

# Now three rows citing sources that are NOT on disk (one via an #anchor), while
# candidates.csv stays clean — exactly what merge leaves behind.
runjson '[{"subject":"유령","relation":"참조","object":"대상","source":"sources/ghost.md","status":"candidate","confidence":"0.9","note":""},{"subject":"유령","relation":"참조","object":"둘","source":"sources/ghost.md#sec","status":"candidate","confidence":"0.9","note":""},{"subject":"유령","relation":"참조","object":"셋","source":"sources/gone.md","status":"candidate","confidence":"0.9","note":""}]' "2026-01-02-ghost.json"
set +e; rout="$("$PYTHON" "$COV" --wiki "$RUNKB" 2>&1)"; rrc=$?; set -e
printf '%s' "$rout" | grep -qF "0 orphan citation(s), 2 run-cited source(s) missing" && ok "run-cited missing sources counted in summary" || bad "run-orphan summary field missing: $rout"
printf '%s' "$rout" | grep -qF "RUN ROWS cite a missing source (dropped at merge, 2 row(s)): sources/ghost.md" && ok "per-source line carries the row count (anchor folded)" || bad "ghost.md run-orphan line missing"
printf '%s' "$rout" | grep -qF "RUN ROWS cite a missing source (dropped at merge, 1 row(s)): sources/gone.md" && ok "second missing source reported on its own line" || bad "gone.md run-orphan line missing"
# The hint must stay TRUTHFUL: `eject --orphans` reads the same candidates.csv,
# so naming it as the remedy would send the reader to a command that reports
# nothing to do. The issue reference is pinned with it — dropping the number
# would leave a dead end where there is a tracked fix (#559).
printf '%s' "$rout" | grep -qF 'run rows cite 2 missing source(s) (3 row(s) total); inspect runs/*.json — `factlog eject --orphans` does not cover these (see #559)' \
  && ok "remedy hint is truthful and names the tracked eject fix" || bad "remedy hint missing/wrong: $rout"
[ "$rrc" -eq 0 ] && ok "run orphans alone do not fail the default run" || bad "run orphans caused exit $rrc"
# The new axis is informational: --strict's contract is text gaps, nothing else.
set +e; "$PYTHON" "$COV" --wiki "$RUNKB" --strict >/dev/null 2>&1; srrc=$?; set -e
[ "$srrc" -eq 0 ] && ok "--strict does not gate on run orphans" || bad "--strict failed on run orphans (exit $srrc)"

# The worst case: every source deleted, so coverage takes the "no source files"
# early return — the branch that used to say nothing at all about these rows.
rm -f "$RUNKB/sources/live.md"
printf '%s\n' "$HEADER" > "$RUNKB/facts/candidates.csv"
set +e; eout="$("$PYTHON" "$COV" --wiki "$RUNKB" 2>&1)"; erc=$?; set -e
printf '%s' "$eout" | grep -qF "coverage: no source files" && ok "empty-sources KB still takes the early return" || bad "early-return line missing"
printf '%s' "$eout" | grep -qF "RUN ROWS cite a missing source (dropped at merge, 1 row(s)): sources/live.md" && ok "early return reports run orphans too" || bad "early return silent about run orphans: $eout"
[ "$erc" -eq 0 ] && ok "early return with run orphans exits 0" || bad "early return exit $erc"

# --- the remedy depends on whether candidates.csv still carries the ref (#558) -
# A ghost row a human ruled on survives the rebuild (#218 ratchet), so
# `factlog eject --orphans` — which builds its cited set from candidates.csv —
# CAN retire it. Printing "does not cover these" there was a false remedy.
REMKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$REMKB" >/dev/null
printf 'live\n' > "$REMKB/sources/live.md"
# kept.md and review.md are still cited by candidates.csv; blind.md is not
# (merge dropped it). review.md is `needs_review` on purpose: it is EJECTABLE
# (eject reads the raw CSV at any status) but invisible to the orphan axis, which
# counts engine statuses only — so a hint keyed on the orphan list, the obvious
# shortcut, would tell the reader to hand-edit runs/*.json for a source
# `eject --orphans` retires. Only a raw-CSV predicate gets this row right.
printf '%s\n%s\n%s\n%s\n' "$HEADER" \
  '갑봇,통합,을서비스,sources/live.md,accepted,0.9,' \
  '유령,참조,대상,sources/kept.md,confirmed,0.9,' \
  '유령,참조,검토,sources/review.md,needs_review,0.9,' > "$REMKB/facts/candidates.csv"
printf '%s' '[{"subject":"유령","relation":"참조","object":"대상","source":"sources/kept.md","status":"confirmed","confidence":"0.9","note":""},{"subject":"유령","relation":"참조","object":"검토","source":"sources/review.md","status":"needs_review","confidence":"0.9","note":""},{"subject":"유령","relation":"참조","object":"둘","source":"sources/blind.md","status":"candidate","confidence":"0.9","note":""}]' > "$REMKB/runs/2026-01-01-mixed.json"
set +e; mout="$("$PYTHON" "$COV" --wiki "$REMKB" 2>&1)"; mrc=$?; set -e
printf '%s' "$mout" | grep -qF "RUN ROWS cite a missing source (1 row(s); candidates.csv still carries rows for it): sources/kept.md" \
  && ok "a ref candidates.csv still carries is not called 'dropped at merge'" || bad "kept.md line wrong: $mout"
printf '%s' "$mout" | grep -qF "RUN ROWS cite a missing source (1 row(s); candidates.csv still carries rows for it): sources/review.md" \
  && ok "a needs_review ghost is ejectable even though it is not an orphan citation" || bad "review.md line wrong: $mout"
printf '%s' "$mout" | grep -qF 'run rows cite 2 missing source(s) (2 row(s) total) that candidates.csv still carries; retire them with `factlog eject --orphans`' \
  && ok "ejectable refs are pointed at the command that retires them" || bad "eject remedy missing: $mout"
printf '%s' "$mout" | grep -qF "RUN ROWS cite a missing source (dropped at merge, 1 row(s)): sources/blind.md" \
  && ok "the blind-spot ref keeps its own wording in the same report" || bad "blind.md line wrong: $mout"
printf '%s' "$mout" | grep -qF 'run rows cite 1 missing source(s) (1 row(s) total); inspect runs/*.json — `factlog eject --orphans` does not cover these (see #559)' \
  && ok "both classes get their own truthful hint in one KB" || bad "blind-spot hint missing: $mout"
# The orphan axis is untouched by any of this: kept.md is an engine-status row
# citing a missing file, so it was, and still is, exactly one orphan citation.
printf '%s' "$mout" | grep -qF "1 orphan citation(s), 3 run-cited source(s) missing" \
  && ok "orphan citation count and meaning unchanged" || bad "orphan count changed: $mout"
[ "$mrc" -eq 0 ] && ok "mixed KB still exits 0" || bad "mixed KB exit $mrc"

# --- an unreadable run file is skipped, but SAID so ---------------------------
# Skipping keeps the report alive (merge itself exits 1 here); skipping in
# silence would recreate this PR's own complaint — rows nobody reports.
printf '%s' '{ not json at all' > "$REMKB/runs/2026-01-02-corrupt.json"
set +e; uout="$("$PYTHON" "$COV" --wiki "$REMKB" 2>&1)"; urc=$?; set -e
printf '%s' "$uout" | grep -qF "skipped unreadable runs/2026-01-02-corrupt.json — its rows are NOT in the counts above" \
  && ok "unreadable run file is named on stderr" || bad "unreadable run file skipped in silence: $uout"
[ "$urc" -eq 0 ] && ok "an unreadable run file does not take the report down" || bad "unreadable run file exit $urc"
printf '%s' "$uout" | grep -qF "3 run-cited source(s) missing" && ok "the counts are unchanged by the unreadable file" || bad "counts changed: $uout"

echo ""
echo "========================================"
echo "test_coverage: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
