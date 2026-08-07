#!/usr/bin/env bash
# tests/test_compile_dedup.sh — accepted.dl triple dedup across sources (#191)
#
# The same (subject, relation, object) accepted from several sources must become
# a SINGLE engine atom in facts/accepted.dl so ask/evaluate and run_logic_check
# report set semantics (one row / true count), not an inflated duplicated count.
# Source aggregation (sources: N, provenance) lives on the separate candidates
# path and must stay lossless.
#
# Pins:
#   (a) compile: a multi-source triple appears exactly once in accepted.dl
#   (b) ask evaluate: count=1, one row (no duplicate row)
#   (c) render: the single row still shows (sources: 2) — provenance lossless
#   (d) run_logic_check: `engine facts` count reflects the deduped set (no dup)
#   (e) byte-stability: a KB with no duplicate triple compiles unchanged
#   (f) Unicode: one fact written NFC and NFD is ONE atom, in the composed
#       spelling, and the engine sees one (#342)
#   (g) a uniformly decomposed KB keeps its decomposed spelling byte-for-byte
#
# Synthetic data only (relation path needs no pyrewire).
# Usage: bash tests/test_compile_dedup.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62) from the dev machine

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
ROUTER="$PLUGIN_ROOT/tools/ask_router.py"
HEADER="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
# Two distinct sources on disk backing the SAME triple, both accepted.
printf 'a\n' > "$KB/sources/a.md"; printf 'b\n' > "$KB/sources/b.md"
printf '%s\n%s\n%s\n' "$HEADER" \
  'PMID:16354850,게재저널,Chest,sources/a.md,confirmed,0.90,' \
  'PMID:16354850,게재저널,Chest,sources/b.md,confirmed,0.95,' \
  > "$KB/facts/candidates.csv"

# --- compile ---------------------------------------------------------------
compile_out="$(FACTLOG_ROOT="$KB" "$PYTHON" -m factlog.compile_facts)"

# (a) exactly one relation() line for the triple in accepted.dl
n_lines="$(grep -cF 'relation("PMID:16354850", "게재저널", "Chest")' "$KB/facts/accepted.dl" || true)"
if [ "$n_lines" = "1" ]; then ok "multi-source triple appears once in accepted.dl"; else bad "expected 1 accepted.dl line, got $n_lines"; fi

# (a2) compile stdout surfaces the distinct-source count for the merged triple
printf '%s' "$compile_out" | grep -F 'PMID:16354850 / 게재저널 / Chest' | grep -qF 'sources=2' \
  && ok "compile stdout annotates the merged triple with sources=2 (observability)" \
  || bad "compile stdout missing sources=2 on the merged triple: $compile_out"

router() { "$PYTHON" "$ROUTER" "$@" --target "$KB"; }
field() { "$PYTHON" -c "import json,sys; print(json.load(sys.stdin).get(sys.argv[1]))" "$1"; }

# (b) ask evaluate: count=1 and exactly one row
ev="$(router evaluate 'relation("PMID:16354850", "게재저널", O)?')"
cnt="$(printf '%s' "$ev" | field count)"
nrows="$(printf '%s' "$ev" | "$PYTHON" -c "import json,sys; print(len(json.load(sys.stdin).get('rows', [])))")"
[ "$cnt" = "1" ] && ok "evaluate count=1 (no inflation)" || bad "evaluate count=$cnt (expected 1)"
[ "$nrows" = "1" ] && ok "evaluate returns exactly one row" || bad "evaluate returned $nrows rows (expected 1)"

# (c) render keeps both sources on the single row — provenance lossless
rn="$(router render 'relation("PMID:16354850", "게재저널", O)?')"
printf '%s' "$rn" | grep -qF "sources: 2" && ok "render keeps (sources: 2) — provenance lossless" || bad "render lost the second source: $rn"
# still just one rendered fact row
frows="$(printf '%s' "$rn" | grep -cF '게재저널, Chest' || true)"
[ "$frows" = "1" ] && ok "render shows the fact once (deduped)" || bad "render showed the fact $frows times"

# (d) run_logic_check: engine-facts count reflects the deduped set. Needs a
# compiled policy (and pyrewire); guard so environments without it skip.
if "$PYTHON" -c "import pyrewire" >/dev/null 2>&1; then
  printf '# policy\n## Rules\n- [j] 어떤 항목이 `게재저널` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB/policy/logic-policy.md"
  ( cd "$PLUGIN_ROOT" && FACTLOG_ROOT="$KB" "$PYTHON" tools/generate_logic_policy.py >/dev/null 2>&1 )
  lc="$(FACTLOG_ROOT="$KB" "$PYTHON" "$PLUGIN_ROOT/tools/run_logic_check.py" 2>/dev/null || true)"
  ef="$(printf '%s' "$lc" | grep -oE 'engine facts: [0-9]+' | grep -oE '[0-9]+' | head -1)"
  [ "$ef" = "1" ] && ok "run_logic_check engine facts=1 (no duplicate)" || bad "run_logic_check engine facts=$ef (expected 1)"
else
  echo "SKIP: pyrewire unavailable — skipping run_logic_check assertion"
fi

# --- (e) byte-stability: no duplicate triple -> accepted.dl unchanged -------
KB2="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB2" >/dev/null
printf 'a\n' > "$KB2/sources/a.md"
printf '%s\n%s\n%s\n' "$HEADER" \
  'A,uses,X,sources/a.md,confirmed,0.90,' \
  'B,uses,Y,sources/a.md,confirmed,0.90,' \
  > "$KB2/facts/candidates.csv"
FACTLOG_ROOT="$KB2" "$PYTHON" -m factlog.compile_facts >/dev/null
before="$(cat "$KB2/facts/accepted.dl")"
FACTLOG_ROOT="$KB2" "$PYTHON" -m factlog.compile_facts >/dev/null
after="$(cat "$KB2/facts/accepted.dl")"
[ "$before" = "$after" ] && ok "no-duplicate KB: accepted.dl compiles byte-stable" || bad "accepted.dl changed on a no-duplicate KB"
# and each distinct triple present exactly once
[ "$(grep -cE '^relation\(' "$KB2/facts/accepted.dl")" = "2" ] && ok "distinct triples preserved (2 rows)" || bad "distinct triple count wrong"

# --- (f) canonically equivalent spellings collapse to one atom (#342) -------
# The dedup key folds subject and object to NFC, so a fact written once composed
# and once decomposed is one engine atom, not two byte-different visually
# identical relation() lines. Before the fix this KB compiled to two.
KB3="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB3" >/dev/null
printf 'a\n' > "$KB3/sources/a.md"; printf 'b\n' > "$KB3/sources/b.md"
"$PYTHON" - "$KB3" <<'PY'
import sys, unicodedata
from pathlib import Path
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
rows = [
    "subject,relation,object,source,status,confidence,note",
    f"{nfd('삼성')},대표,{nfd('이재용')},sources/a.md,confirmed,0.90,",
    f"{nfc('삼성')},대표,{nfc('이재용')},sources/b.md,confirmed,0.95,",
]
Path(sys.argv[1], "facts/candidates.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
PY
compile3="$(FACTLOG_ROOT="$KB3" "$PYTHON" -m factlog.compile_facts)"

n_atoms="$(grep -cE '^relation\(' "$KB3/facts/accepted.dl" || true)"
[ "$n_atoms" = "1" ] && ok "NFC/NFD spellings of one fact compile to a single atom" \
  || bad "expected 1 atom for the folded fact, got $n_atoms"

# the surviving spelling is the composed one — the row was authored, and it is
# what a reader greps for from an NFC editor. Decomposed first in the CSV, so
# this dies if the fold merely kept first-occurrence.
"$PYTHON" - "$KB3" <<'PY' && ok "the atom written is the composed spelling" || bad "atom is not composed"
import sys, unicodedata
from pathlib import Path
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
lines = [l for l in Path(sys.argv[1], "facts/accepted.dl").read_text(encoding="utf-8").split("\n")
         if l.startswith("relation(")]
assert len(lines) == 1, lines
ok = all(f'"{nfc(v)}"' in lines[0] for v in ("삼성", "이재용"))
ok = ok and not any(f'"{nfd(v)}"' in lines[0] for v in ("삼성", "이재용"))
sys.exit(0 if ok else 1)
PY

# the merged atom's source count covers both spellings (log-only observability)
printf '%s' "$compile3" | grep -qF 'sources=2' \
  && ok "compile stdout reports sources=2 for the folded atom" \
  || bad "compile stdout lost a source of the folded atom: $compile3"

# (f2) ...and so does the ANSWER. The compile log and `ask` read two different
# maps; folding the atom without folding fact_signals leaves the answer unable to
# find its own atom, and the row silently loses its sources, its backing paths
# and its staleness marker to the [no extraction backing] branch. Block (c)
# checks this for the same-spelling case; the folded case needs it more.
rn3="$(FACTLOG_ROOT="$KB3" "$PYTHON" "$ROUTER" render 'relation(S, "대표", O)?' --target "$KB3")"
printf '%s' "$rn3" | grep -qF "sources: 2" \
  && ok "render keeps (sources: 2) on the folded atom" \
  || bad "render lost a source of the folded atom: $rn3"
printf '%s' "$rn3" | grep -qF "no extraction backing" \
  && bad "folded atom lost its provenance entirely: $rn3" \
  || ok "folded atom is not reported as unbacked"
for s in a b; do
  printf '%s' "$rn3" | grep -qF "sources/$s.md" \
    && ok "render lists backing source sources/$s.md" \
    || bad "render dropped backing source sources/$s.md: $rn3"
done

# the engine, not just the python helper: one row out of pyrewire
if "$PYTHON" -c "import pyrewire" >/dev/null 2>&1; then
  ev3="$(FACTLOG_ROOT="$KB3" "$PYTHON" "$ROUTER" evaluate 'relation(S, "대표", O)?' --target "$KB3")"
  cnt3="$(printf '%s' "$ev3" | field count)"
  [ "$cnt3" = "1" ] && ok "engine evaluate count=1 on the folded fact" || bad "engine evaluate count=$cnt3 (expected 1)"
  printf '# policy\n## Rules\n- [d] 어떤 항목이 `대표` 관계를 가지면 검토(review)가 필요하다.\n' > "$KB3/policy/logic-policy.md"
  ( cd "$PLUGIN_ROOT" && FACTLOG_ROOT="$KB3" "$PYTHON" tools/generate_logic_policy.py >/dev/null 2>&1 )
  lc3="$(FACTLOG_ROOT="$KB3" "$PYTHON" "$PLUGIN_ROOT/tools/run_logic_check.py" 2>/dev/null || true)"
  ef3="$(printf '%s' "$lc3" | grep -oE 'engine facts: [0-9]+' | grep -oE '[0-9]+' | head -1)"
  [ "$ef3" = "1" ] && ok "run_logic_check engine facts=1 on the folded fact" || bad "run_logic_check engine facts=$ef3 (expected 1)"
else
  echo "SKIP: pyrewire unavailable — skipping engine assertions for the folded fact"
fi

# --- (g) uniformly decomposed KB keeps its spelling ------------------------
# Folding decides identity; it never rewrites the output. A KB with no composed
# member has no composed spelling to prefer, and normalizing on the way out
# would invent a string the KB never wrote. This dies if the fix writes
# NFC(object) into the atom instead of choosing a spelling that was authored.
KB4="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB4" >/dev/null
printf 'a\n' > "$KB4/sources/a.md"; printf 'b\n' > "$KB4/sources/b.md"
"$PYTHON" - "$KB4" <<'PY'
import sys, unicodedata
from pathlib import Path
nfd = lambda s: unicodedata.normalize("NFD", s)
rows = [
    "subject,relation,object,source,status,confidence,note",
    f"{nfd('삼성')},대표,{nfd('이재용')},sources/a.md,confirmed,0.90,",
    f"{nfd('삼성')},대표,{nfd('이재용')},sources/b.md,confirmed,0.95,",
]
Path(sys.argv[1], "facts/candidates.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
PY
compile4="$(FACTLOG_ROOT="$KB4" "$PYTHON" -m factlog.compile_facts)"

# The all-NFD KB is what pins compile_facts' source-count LOOKUP. Both rows are
# decomposed, so the atom is decomposed too and its RAW triple differs from its
# folded key — a raw lookup misses and silently falls to the `, 1` default. In
# block (f) the group has a composed member, so the written atom is NFC and the
# raw and folded keys coincide by accident; that case cannot see this. Same
# defect shape as the ask-side one (a source lost), with the compile log as the
# surface.
printf '%s' "$compile4" | grep -qF 'sources=2' \
  && ok "all-NFD atom's source count survives the lookup (sources=2)" \
  || bad "all-NFD atom lost a source in the compile log: $compile4"
"$PYTHON" - "$KB4" <<'PY' && ok "uniformly decomposed KB keeps its decomposed spelling" || bad "decomposed KB was normalized on the way out"
import sys, unicodedata
from pathlib import Path
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
lines = [l for l in Path(sys.argv[1], "facts/accepted.dl").read_text(encoding="utf-8").split("\n")
         if l.startswith("relation(")]
assert len(lines) == 1, lines
# subject and object as authored (decomposed), and not silently recomposed.
# The relation is ASCII-free but written composed in the CSV, so the LINE is
# never wholly NFD — check the two folded axes, which is what the fix touches.
ok = all(f'"{nfd(v)}"' in lines[0] for v in ("삼성", "이재용"))
ok = ok and not any(f'"{nfc(v)}"' in lines[0] for v in ("삼성", "이재용"))
sys.exit(0 if ok else 1)
PY

# --- (h) a pre-fold accepted.dl must not decode as a bare intern id ---------
# run_wirelog parses the FILE TEXT but interns from the loader's rows. If the
# loader folds identity, the losing spelling stays in the program and never
# reaches session.intern, and decode_wirelog_value returns the raw integer — so
# facts/logic_report.txt names an entity "3". Any accepted.dl compiled by an
# earlier release carries both spellings, so this needs no hand-editing to hit;
# writing the two atoms directly is exactly what that release produced.
if "$PYTHON" -c "import pyrewire" >/dev/null 2>&1; then
  KB5="$(mktemp -d)/wiki"
  "$PYTHON" -m factlog init --target "$KB5" >/dev/null
  printf 'a\n' > "$KB5/sources/a.md"
  "$PYTHON" - "$KB5" <<'PY'
import sys, unicodedata
from pathlib import Path
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
kb = Path(sys.argv[1])
kb.joinpath("facts/candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfc('부산항만공사')},관할,부산항,sources/a.md,confirmed,0.90,\n",
    encoding="utf-8",
)
# What a pre-fold release compiled: both spellings, as separate atoms.
kb.joinpath("facts/accepted.dl").write_text(
    "// generated from facts/candidates.csv\n\n"
    f'relation("{nfd("부산항만공사")}", "관할", "부산항").\n'
    f'relation("{nfc("부산항만공사")}", "관할", "부산항").\n',
    encoding="utf-8",
)
kb.joinpath("policy/logic-policy.md").write_text(
    "# policy\n## Rules\n- [g] 어떤 항목이 `관할` 관계를 가지면 검토(review)가 필요하다.\n",
    encoding="utf-8",
)
PY
  ( cd "$PLUGIN_ROOT" && FACTLOG_ROOT="$KB5" "$PYTHON" tools/generate_logic_policy.py >/dev/null 2>&1 )
  # Checked on the engine's DECODED values, not on the report text: the report
  # also carries a "- requires_review: N rows" summary whose N is legitimately a
  # number, and a grep for a digit matches that line on a perfectly healthy run.
  ( cd "$PLUGIN_ROOT" && FACTLOG_ROOT="$KB5" "$PYTHON" - <<'PY'
import sys
sys.path.insert(0, ".")
from factlog import common
inferred = common.run_wirelog()
rows = inferred.get("requires_review", set())
assert rows, "PROBE FOUND NO requires_review ROWS — refusing to report a clean result"
digits = [r for r in rows if any(str(v).isdigit() for v in r)]
if digits:
    print(f"decoded as bare intern id(s): {digits}", file=sys.stderr)
    sys.exit(1)
assert any("부산항만공사" in str(v) for r in rows for v in r), "entity name absent entirely"
sys.exit(0)
PY
  ) 2>/dev/null \
    && ok "no atom decodes as a bare intern id on a pre-fold accepted.dl" \
    || bad "engine decoded an entity as a bare intern id on a pre-fold accepted.dl"
else
  echo "SKIP: pyrewire unavailable — skipping intern-sync assertions"
fi

# --- (i) the composed spelling must reach the TYPED table -------------------
# literal_types.normalize gets the object as written, so a decomposed ordinal
# returns None and the fact leaves the typed table silently. Picking the
# representative per axis is what keeps the composed spelling on the object even
# when no single row is composed on both axes.
if "$PYTHON" -c "import pyrewire" >/dev/null 2>&1; then
  KB6="$(mktemp -d)/wiki"
  "$PYTHON" -m factlog init --target "$KB6" >/dev/null
  printf 'a\n' > "$KB6/sources/a.md"; printf 'b\n' > "$KB6/sources/b.md"
  "$PYTHON" - "$KB6" <<'PY'
import sys, unicodedata
from pathlib import Path
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
kb = Path(sys.argv[1])
# The cross group: neither row is composed on both axes.
kb.joinpath("facts/candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfc('현대건설')},순위,{nfd('7위')},sources/a.md,confirmed,0.90,\n"
    f"{nfd('현대건설')},순위,{nfc('7위')},sources/b.md,confirmed,0.95,\n",
    encoding="utf-8",
)
# `as <alias>` is REQUIRED by the parser — without it the line is skipped as
# malformed, typed_relations() returns {}, and _project_typed_relations never
# runs at all, so the assertion below would pass without the typed table ever
# existing.
kb.joinpath("policy/typed-relations.md").write_text(
    "# typed\n- `순위` : ordinal as rank\n", encoding="utf-8",
)
# A typed relation's object is a literal, so it must also be declared an
# attribute relation — otherwise typed_relations() warns and the value stays in
# the entity graph.
kb.joinpath("policy/attribute-relations.md").write_text(
    "# attributes\n- `순위`\n", encoding="utf-8",
)
PY
  FACTLOG_ROOT="$KB6" "$PYTHON" -m factlog.compile_facts >/dev/null
  # Guard the premise first: if the spec does not parse, nothing below is a test
  # of the typed table.
  ( cd "$PLUGIN_ROOT" && FACTLOG_ROOT="$KB6" "$PYTHON" -c "
from factlog.common import typed_relations
specs = typed_relations()
assert specs, 'typed-relations.md did not parse — the typed-table assertions below are vacuous'
" ) && ok "typed spec parses (the typed-table premise holds)" || bad "typed-relations.md did not parse"

  "$PYTHON" - "$KB6" <<'PY' && ok "cross group writes a composed object that normalizes as an ordinal" || bad "cross group wrote a decomposed object — the typed literal is dropped"
import sys, unicodedata
from pathlib import Path
sys.path.insert(0, ".")
from factlog import literal_types
nfc = lambda s: unicodedata.normalize("NFC", s)
lines = [l for l in Path(sys.argv[1], "facts/accepted.dl").read_text(encoding="utf-8").split("\n")
         if l.startswith("relation(")]
assert len(lines) == 1, lines
obj = lines[0].rsplit('", "', 1)[1].rstrip('").')
sys.exit(0 if obj == nfc(obj) and literal_types.normalize("ordinal", obj) is not None else 1)
PY

  # ...and the ENGINE really loads it typed. This is what earns the pyrewire
  # gate. Asserted through a RULE over the projected side-relation, not by
  # reading `rank` back: typed side-relations are EDB inserts, and run_wirelog
  # only collects what `session.step()` derives, so an EDB probe reads empty on
  # a perfectly healthy run. `top_ranked` is IDB, so it fires only if the
  # ordinal really reached the typed table.
  printf '.decl top_ranked(entity: symbol, reason: symbol)\ntop_ranked(S, "rank_le_10") :- rank(S, V), V <= 10.\n' \
    > "$KB6/policy/logic-policy.extra.dl"
  ( cd "$PLUGIN_ROOT" && FACTLOG_ROOT="$KB6" "$PYTHON" - <<'PY'
import sys
sys.path.insert(0, ".")
from factlog import common
rows = common.run_wirelog().get("top_ranked", set())
assert rows, "top_ranked did not fire — the ordinal never reached the typed table"
assert any("현대건설" in str(v) for r in rows for v in r), f"unexpected rows: {rows}"
PY
  ) 2>/dev/null \
    && ok "a rule over the typed side-relation fires on the folded atom" \
    || bad "the typed side-relation never received the folded atom"
else
  echo "SKIP: pyrewire unavailable — skipping typed-literal assertions"
fi

echo ""
echo "========================================"
echo "test_compile_dedup: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
