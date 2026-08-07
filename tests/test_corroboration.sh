#!/usr/bin/env bash
# tests/test_corroboration.sh — multi-source corroboration (#33)
#
# Pins:
#   - a fact backed by 2 distinct sources reports "2 source(s)"; a single-source
#     fact reports "1 source(s)"
#   - same (s,r,o) from the same source twice counts as 1 (distinct sources)
#   - single-valued relations with competing values show per-source support
#   - corroboration.py is informational (always exit 0)
#
# Deterministic; no pyrewire.  Usage: bash tests/test_corroboration.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
CORR="$PLUGIN_ROOT/tools/corroboration.py"
HEADER="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
csv() { printf '%s\n' "$HEADER" "$@" > "$KB/facts/candidates.csv"; }

# Acme API/uses/FastAPI backed by 2 sources; Acme API/depends_on/Postgres by 1.
csv \
  'Acme API,uses,FastAPI,sources/a.md,confirmed,0.9,' \
  'Acme API,uses,FastAPI,sources/b.md,confirmed,0.9,' \
  'Acme API,depends_on,Postgres,sources/a.md,confirmed,0.9,'
set +e; out="$("$PYTHON" "$CORR" --wiki "$KB" 2>&1)"; rc=$?; set -e  # capture before errexit
[ "$rc" -eq 0 ] && ok "corroboration.py exits 0 (informational)" || bad "corroboration.py exit $rc"
printf '%s' "$out" | grep -qF "2 source(s): Acme API, uses, FastAPI" && ok "fact backed by 2 sources reports 2" || bad "2-source fact not reported"
printf '%s' "$out" | grep -qF "1 source(s): Acme API, depends_on, Postgres" && ok "single-source fact reports 1" || bad "1-source fact not reported"

# same source twice = 1 distinct source
csv 'X,r,Y,sources/a.md,confirmed,0.9,' 'X,r,Y,sources/a.md,confirmed,0.9,dup'
printf '%s' "$("$PYTHON" "$CORR" --wiki "$KB" 2>&1)" | grep -qF "1 source(s): X, r, Y" && ok "same source twice counts as 1 distinct" || bad "duplicate source miscounted"

# single-valued competing values show per-source support
printf '# single-valued\n- 주_속성\n' > "$KB/policy/single-valued.md"
csv \
  '을서비스,주_속성,값가,sources/a.md,confirmed,0.9,' \
  '을서비스,주_속성,값나,sources/b.md,confirmed,0.9,'
co="$("$PYTHON" "$CORR" --wiki "$KB" 2>&1)"
printf '%s' "$co" | grep -qF "competing values" && ok "single-valued competing values reported" || bad "competing values not reported"
printf '%s' "$co" | grep -qF "값가 (1 src)" && ok "competing value shows per-source support" || bad "per-source support missing"

# uniformly-NFD KB still reaches the competing-values bucket (#325): the policy
# file holds the composed relation name, the rows the decomposed one. Nothing is
# mixed — one consistently decomposed KB is enough, and a raw membership test
# matches none of it. Exercises the consumer, not just the shared helper.
"$PYTHON" - "$KB" <<'PY'
import sys, unicodedata
from pathlib import Path
kb = Path(sys.argv[1])
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
(kb / "policy" / "single-valued.md").write_text(
    f"# single-valued\n- {nfc('소속')}\n", encoding="utf-8"
)
(kb / "facts" / "candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfd('김철수')},{nfd('소속')},AAA,sources/a.md,confirmed,0.9,\n"
    f"{nfd('김철수')},{nfd('소속')},BBB,sources/b.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
co="$("$PYTHON" "$CORR" --wiki "$KB" 2>&1)"
printf '%s' "$co" | grep -qF "competing values" \
  && ok "uniformly-NFD KB reaches competing values (membership folded)" \
  || bad "NFD KB competing values missed: $(printf '%s' "$co" | tail -3)"

# the POLICY file is the decomposed side (#325). The case above folds only the
# row side: with the policy name already composed, `folded_relation_names` is the
# identity there and dropping it changes nothing. Membership compares two
# hand-written files and either one can be the decomposed one, so this pins the
# half the rows-NFD case cannot reach.
"$PYTHON" - "$KB" <<'PY'
import sys, unicodedata
from pathlib import Path
kb = Path(sys.argv[1])
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
(kb / "policy" / "single-valued.md").write_text(
    f"# single-valued\n- {nfd('소속')}\n", encoding="utf-8"
)
(kb / "facts" / "candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfc('김철수')},{nfc('소속')},AAA,sources/a.md,confirmed,0.9,\n"
    f"{nfc('김철수')},{nfc('소속')},BBB,sources/b.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
co="$("$PYTHON" "$CORR" --wiki "$KB" 2>&1)"
printf '%s' "$co" | grep -qF "competing values" \
  && ok "NFD policy file with composed rows reaches competing values" \
  || bad "NFD policy not folded: $(printf '%s' "$co" | tail -3)"

# --- two spellings of ONE value are not listed as two competitors (#325) ------
# Raw grouping printed "한국대 (1 src); 한국대 (1 src)" — one value shown as a
# contradiction between two strings that render identically, with the gate
# (check_conflicts) saying there is none. (The source re-aggregation and the
# displayed spelling are pinned separately below; this case only fires the
# object-axis fold.)
SKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$SKB" >/dev/null
printf 'x\n' > "$SKB/sources/a.md"
"$PYTHON" - "$SKB" <<'PY'
import sys, unicodedata
from pathlib import Path
kb = Path(sys.argv[1])
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
(kb / "policy" / "single-valued.md").write_text(f"# single-valued\n- {nfc('소속')}\n", encoding="utf-8")
(kb / "facts" / "candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfc('김철수')},{nfc('소속')},{nfc('한국대')},sources/a.md,confirmed,0.9,\n"
    f"{nfc('김철수')},{nfc('소속')},{nfd('한국대')},sources/a.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
co="$("$PYTHON" "$CORR" --wiki "$SKB" 2>&1)"
printf '%s' "$co" | grep -qF "competing values" \
  && bad "two spellings of one value listed as competing: $(printf '%s' "$co" | tail -2)" \
  || ok "two spellings of one value are not competing values (object axis folded)"

# --- a mixed-subject KB reaches the competing section at all (#325) -----------
# The reason the subject axis is folded here: raw, the two rows sit in separate
# (subject, relation) pairs, each holding ONE value, so `contested` is empty and
# the whole section disappears — a real single-valued competition that the gate
# (check_conflicts) reports as a conflict is never shown at the source level.
MKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$MKB" >/dev/null
printf 'x\n' > "$MKB/sources/a.md"
"$PYTHON" - "$MKB" <<'PY'
import sys, unicodedata
from pathlib import Path
kb = Path(sys.argv[1])
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
(kb / "policy" / "single-valued.md").write_text(f"# single-valued\n- {nfc('소속')}\n", encoding="utf-8")
(kb / "facts" / "candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfc('김철수')},{nfc('소속')},A사,sources/a.md,confirmed,0.9,\n"
    f"{nfd('김철수')},{nfc('소속')},B사,sources/b.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
co="$("$PYTHON" "$CORR" --wiki "$MKB" 2>&1)"
printf '%s' "$co" | grep -qF "competing values" \
  && ok "mixed-subject KB reaches the competing section (subject axis folded)" \
  || bad "mixed-subject competition invisible: $(printf '%s' "$co" | tail -3)"
printf '%s' "$co" | grep -qF "A사 (1 src); B사 (1 src)" \
  && ok "both competing values listed under one subject" \
  || bad "competing values wrong: $(printf '%s' "$co" | tail -2)"

# --- one source backing two spellings counts once (#325) ---------------------
# The report aggregates source SETS per folded value rather than summing counts:
# both spellings come from one file, and adding two per-spelling counts would
# report two sources where there is one. corroboration_counts is keyed on
# common.engine_atom_key (#342), the same fold, so neither path double-counts.
OKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$OKB" >/dev/null
printf 'x\n' > "$OKB/sources/a.md"
"$PYTHON" - "$OKB" <<'PY'
import sys, unicodedata
from pathlib import Path
kb = Path(sys.argv[1])
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
(kb / "policy" / "single-valued.md").write_text(f"# single-valued\n- {nfc('소속')}\n", encoding="utf-8")
# Both spellings of 한국대 come from the SAME file; 서울대 gives the pair a second
# value so the competing section prints at all.
(kb / "facts" / "candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfc('김철수')},{nfc('소속')},{nfc('한국대')},sources/a.md,confirmed,0.9,\n"
    f"{nfc('김철수')},{nfc('소속')},{nfd('한국대')},sources/a.md,confirmed,0.9,\n"
    f"{nfc('김철수')},{nfc('소속')},{nfc('서울대')},sources/b.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
co="$("$PYTHON" "$CORR" --wiki "$OKB" 2>&1)"
# The WHOLE line, not a substring: raw grouping printed 한국대 twice at (1 src)
# each, which a `grep -qF "한국대 (1 src)"` would happily accept.
printf '%s' "$co" | grep -qF "김철수 / 소속: 서울대 (1 src); 한국대 (1 src)" \
  && ok "one source backing two spellings counts once (and is listed once)" \
  || bad "source aggregation wrong: $(printf '%s' "$co" | tail -2)"
printf '%s' "$co" | grep -qF "한국대 (2 src)" \
  && bad "two spellings from one file reported as two sources" \
  || ok "no double-counted source"

# --- an all-NFD group is reported in the bytes actually written (#325) -------
# The group key is NFC, but the reported spelling comes from the raw strings via
# common.composed_spelling. On a uniformly decomposed KB there is no composed
# member, so printing the key would name a string that appears nowhere in the
# file — ungreppable, and a false claim about what was written.
#
# This guards the NEW code path rather than fixing an old defect: before the
# grouping was folded there was no key to print and the raw object went straight
# to stdout, so this KB passed then too. It fails the moment the report reaches
# for `obj`/`pair[0]` instead of the spelling maps.
AKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$AKB" >/dev/null
printf 'x\n' > "$AKB/sources/a.md"
"$PYTHON" - "$AKB" <<'PY'
import sys, unicodedata
from pathlib import Path
kb = Path(sys.argv[1])
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
(kb / "policy" / "single-valued.md").write_text(f"# single-valued\n- {nfc('소속')}\n", encoding="utf-8")
(kb / "facts" / "candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfd('김철수')},{nfd('소속')},{nfd('한국대')},sources/a.md,confirmed,0.9,\n"
    f"{nfd('김철수')},{nfd('소속')},{nfd('서울대')},sources/b.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
nfd_subject="$("$PYTHON" -c "import unicodedata,sys;sys.stdout.write(unicodedata.normalize('NFD','김철수'))")"
nfd_object="$("$PYTHON" -c "import unicodedata,sys;sys.stdout.write(unicodedata.normalize('NFD','한국대'))")"
co="$("$PYTHON" "$CORR" --wiki "$AKB" 2>&1)"
printf '%s' "$co" | grep -qF "$nfd_object (1 src)" \
  && ok "all-NFD value reported in the bytes actually written" \
  || bad "reported a spelling never written: $(printf '%s' "$co" | tail -2)"
printf '%s' "$co" | grep -qF "$nfd_subject / " \
  && ok "all-NFD subject reported in the bytes actually written" \
  || bad "reported a subject spelling never written: $(printf '%s' "$co" | tail -2)"

# --- the head line and the fact list use the same equivalence as the clause ---
# One fact written in two forms and backed by two different files. Keyed on the
# raw triple it is two facts with one source each, and the corroboration signal
# this tool exists to give — "backed by >1 source" — is under-reported on the
# one KB shape #325 is about. The clause below already folded, so before this
# the two halves of one report disagreed.
HKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$HKB" >/dev/null
printf 'x\n' > "$HKB/sources/a.md"
printf 'y\n' > "$HKB/sources/b.md"
"$PYTHON" - "$HKB" <<'PY'
import sys, unicodedata
from pathlib import Path
kb = Path(sys.argv[1])
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
(kb / "policy" / "single-valued.md").write_text(f"# single-valued\n- {nfc('소속')}\n", encoding="utf-8")
(kb / "facts" / "candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfc('김철수')},{nfc('소속')},{nfc('에이사')},sources/a.md,confirmed,0.9,\n"
    f"{nfc('김철수')},{nfc('소속')},{nfd('에이사')},sources/b.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
co="$("$PYTHON" "$CORR" --wiki "$HKB" 2>&1)"
printf '%s' "$co" | grep -qF "1 fact(s); 1 backed by >1 source" \
  && ok "head line counts two spellings of one fact as one corroborated fact" \
  || bad "head line still raw: $(printf '%s' "$co" | head -2)"
printf '%s' "$co" | grep -qF "2 source(s): 김철수, 소속," \
  && ok "fact list credits both sources to the one folded fact" \
  || bad "fact list still raw: $(printf '%s' "$co" | head -3)"
printf '%s' "$co" | grep -qF "competing values" \
  && bad "one value in two spellings listed as a competition" \
  || ok "the clause and the head line agree (no competition on one value)"

# --- one file backing two spellings is still one source (#325) ----------------
# The other edge of the same fold, and the reason the report aggregates source
# SETS rather than summing per-spelling counts: both spellings come from ONE
# file, so the merged fact has one source, not two. Fails before this change for
# the same reason as the case above (two facts, not one).
SKB2="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$SKB2" >/dev/null
printf 'x\n' > "$SKB2/sources/a.md"
"$PYTHON" - "$SKB2" <<'PY'
import sys, unicodedata
from pathlib import Path
kb = Path(sys.argv[1])
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
(kb / "policy" / "single-valued.md").write_text(f"# single-valued\n- {nfc('소속')}\n", encoding="utf-8")
(kb / "facts" / "candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfc('김철수')},{nfc('소속')},{nfc('에이사')},sources/a.md,confirmed,0.9,\n"
    f"{nfc('김철수')},{nfc('소속')},{nfd('에이사')},sources/a.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
co="$("$PYTHON" "$CORR" --wiki "$SKB2" 2>&1)"
printf '%s' "$co" | grep -qF "1 fact(s); 0 backed by >1 source" \
  && ok "two spellings from one file stay one source" \
  || bad "source double-counted: $(printf '%s' "$co" | head -2)"

# --- genuinely different values still compete (control) ----------------------
DKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$DKB" >/dev/null
printf 'x\n' > "$DKB/sources/a.md"
printf '# single-valued\n- 소속\n' > "$DKB/policy/single-valued.md"
printf '%s\n%s\n%s\n' "subject,relation,object,source,status,confidence,note" \
  '김철수,소속,A사,sources/a.md,confirmed,0.9,' \
  '김철수,소속,B사,sources/a.md,confirmed,0.9,' > "$DKB/facts/candidates.csv"
co="$("$PYTHON" "$CORR" --wiki "$DKB" 2>&1)"
printf '%s' "$co" | grep -qF "competing values" \
  && ok "distinct values still reported as competing" \
  || bad "folding swallowed a real competition: $(printf '%s' "$co" | tail -2)"

echo ""
echo "========================================"
echo "test_corroboration: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
