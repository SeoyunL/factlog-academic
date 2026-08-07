#!/usr/bin/env bash
# tests/test_vocab.sh — `factlog vocab` vocabulary discovery (#85)
#
# Pins (XDG-isolated; synthetic data):
#   - default lists BOTH entities and relations with usage counts
#   - entity = subject + object whose relation is NOT an attribute relation
#     (an attribute-relation object is a literal -> excluded from entities)
#   - relations tagged [attribute] / [single-valued] from policy files
#   - --entities / --relations show only one section
#   - default scope is engine facts; --all adds candidate-only names
#   - empty KB graceful; non-KB path errors
#
# Usage: bash tests/test_vocab.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
H="subject,relation,object,source,status,confidence,note"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'x\n' > "$KB/sources/a.md"
printf -- '- 운영\n' > "$KB/policy/attribute-relations.md"     # 운영 object is a literal
printf '# single-valued\n- 주속성\n' > "$KB/policy/single-valued.md"
# 운영 is also typed (date): its object is a literal already, so declaring it typed
# adds a [typed:date] tag without changing entity/relation counts. It is already in
# attribute-relations.md, so _warn_typed_not_attribute stays silent (no stderr noise).
printf -- '- `운영` : date as op_date\n' > "$KB/policy/typed-relations.md"
printf '%s\n%s\n%s\n%s\n%s\n' "$H" \
  '갑봇,통합,을서비스,sources/a.md,confirmed,0.9,' \
  '갑봇,운영,2030,sources/a.md,confirmed,0.9,' \
  '주체,주속성,값,sources/a.md,confirmed,0.9,' \
  '후보주체,후보관계,후보객체,sources/a.md,candidate,0.5,' > "$KB/facts/candidates.csv"

# --- default: both sections, engine scope ------------------------------------
out="$("$PYTHON" -m factlog vocab --target "$KB" 2>&1)"
printf '%s\n' "$out"; echo "---"
printf '%s' "$out" | grep -qF "— engine facts" && ok "default scope is engine facts" || bad "scope label wrong"
printf '%s' "$out" | grep -qE "entities \(4\):" && ok "entity count excludes literals + candidate-only (4)" || bad "entity count wrong: $(printf '%s' "$out"|grep 'entities (')"
printf '%s' "$out" | grep -qE "relations \(3\):" && ok "relation count is engine-only (3)" || bad "relation count wrong: $(printf '%s' "$out"|grep 'relations (')"
printf '%s' "$out" | grep -qE "\] 을서비스" && ok "non-attribute object listed as an entity" || bad "entity object missing"
printf '%s' "$out" | grep -qF "2030" && bad "attribute-relation object (literal) leaked into vocab" || ok "literal object excluded from entities"
printf '%s' "$out" | grep -qF "후보관계" && bad "candidate-only name shown without --all" || ok "candidate-only names excluded by default"
printf '%s' "$out" | grep -qE "\] 운영  \[attribute(,|\])" && ok "attribute relation tagged" || bad "attribute tag missing"
printf '%s' "$out" | grep -qE "\] 주속성  \[single-valued\]" && ok "single-valued relation tagged" || bad "single-valued tag missing"
printf '%s' "$out" | grep -qE "\] 운영  \[attribute, typed:date\]" && ok "typed relation tagged [typed:date]" || bad "typed tag missing: $(printf '%s' "$out"|grep '운영')"

# --- --entities / --relations show one section -------------------------------
out="$("$PYTHON" -m factlog vocab --entities --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "entities (" && ! printf '%s' "$out" | grep -qF "relations (" && ok "--entities shows only entities" || bad "--entities leaked relations"
out="$("$PYTHON" -m factlog vocab --relations --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "relations (" && ! printf '%s' "$out" | grep -qF "entities (" && ok "--relations shows only relations" || bad "--relations leaked entities"

# --- --all includes candidate-only names -------------------------------------
out="$("$PYTHON" -m factlog vocab --all --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "— all candidate facts" && ok "--all scope label" || bad "--all scope label wrong"
printf '%s' "$out" | grep -qF "후보관계" && ok "--all includes candidate-only relation" || bad "--all missing candidate relation"
printf '%s' "$out" | grep -qE "\] 후보주체" && ok "--all includes candidate-only entity" || bad "--all missing candidate entity"

# --- empty KB graceful -------------------------------------------------------
EKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$EKB" >/dev/null
out="$("$PYTHON" -m factlog vocab --target "$EKB" 2>&1)"
printf '%s' "$out" | grep -qE "entities \(0\):" && printf '%s' "$out" | grep -qF "(none)" && ok "empty KB shows 0 entities/(none)" || bad "empty KB not graceful: $out"

# --- vocab counts reconcile with `status` vocabulary line --------------------
sout="$("$PYTHON" -m factlog status --target "$KB" 2>&1)"
s_ent="$(printf '%s' "$sout" | sed -nE 's/.*vocabulary: +([0-9]+) entit.*/\1/p')"
s_rel="$(printf '%s' "$sout" | sed -nE 's/.*, ([0-9]+) relation\(s\).*/\1/p')"
vout="$("$PYTHON" -m factlog vocab --target "$KB" 2>&1)"
v_ent="$(printf '%s' "$vout" | sed -nE 's/.*entities \(([0-9]+)\).*/\1/p')"
v_rel="$(printf '%s' "$vout" | sed -nE 's/.*relations \(([0-9]+)\).*/\1/p')"
[ -n "$v_ent" ] && [ "$s_ent" = "$v_ent" ] && ok "vocab entity count matches status ($v_ent)" || bad "entity count mismatch: status=$s_ent vocab=$v_ent"
[ -n "$v_rel" ] && [ "$s_rel" = "$v_rel" ] && ok "vocab relation count matches status ($v_rel)" || bad "relation count mismatch: status=$s_rel vocab=$v_rel"

# --- uniformly-NFD KB still gets the [single-valued] tag (#325) ---------------
# vocab is the third membership consumer. With the tag missing, `status` says
# "conflicts: 1 (over 1 single-valued relation(s))" on this KB while vocab
# declines to say WHICH relation is functional — the one question the reader
# came to vocab with. Written from Python so the two forms survive editor
# normalization; the composed name goes in policy, the decomposed one in the rows.
NKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$NKB" >/dev/null
printf 'x\n' > "$NKB/sources/a.md"
"$PYTHON" - "$NKB" <<'PY'
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
    f"{nfd('김철수')},{nfd('소속')},BBB,sources/a.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
out="$("$PYTHON" -m factlog vocab --relations --target "$NKB" 2>&1)"
printf '%s' "$out" | grep -qF "[single-valued]" \
  && ok "uniformly-NFD KB gets the [single-valued] tag (membership folded)" \
  || bad "NFD KB untagged: $(printf '%s' "$out" | tail -2)"
sout="$("$PYTHON" -m factlog status --target "$NKB" 2>&1)"
printf '%s' "$sout" | grep -qE "conflicts: +1 \(over 1 single-valued" \
  && ok "status and vocab agree on the same NFD KB" \
  || bad "status disagrees: $(printf '%s' "$sout" | grep conflicts)"

# --- the POLICY file is the decomposed side (#325) ----------------------------
# The case above folds only the row side: with the policy name already composed,
# `folded_relation_names(sv)` is the identity there and dropping it changes
# nothing. Membership compares two hand-written files and either one can be the
# decomposed one, so this pins the half the rows-NFD case cannot reach.
"$PYTHON" - "$NKB" <<'PY'
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
    f"{nfc('김철수')},{nfc('소속')},BBB,sources/a.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
out="$("$PYTHON" -m factlog vocab --relations --target "$NKB" 2>&1)"
printf '%s' "$out" | grep -qF "[single-valued]" \
  && ok "NFD policy file with composed rows still gets the [single-valued] tag" \
  || bad "NFD policy not folded: $(printf '%s' "$out" | tail -2)"

# --- two spellings of one relation are told apart on screen (#325) ------------
# Counting is on the raw name, so a KB with both spellings prints two lines that
# render identically. Tagging membership made them indistinguishable: before,
# one of the two was at least untagged. The form label is the same remedy
# check_conflicts applies with its escaped spelling lists.
FKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$FKB" >/dev/null
printf 'x\n' > "$FKB/sources/a.md"
"$PYTHON" - "$FKB" <<'PY'
import sys, unicodedata
from pathlib import Path
kb = Path(sys.argv[1])
nfc = lambda s: unicodedata.normalize("NFC", s)
nfd = lambda s: unicodedata.normalize("NFD", s)
(kb / "policy" / "single-valued.md").write_text(f"# single-valued\n- {nfc('소속')}\n", encoding="utf-8")
(kb / "facts" / "candidates.csv").write_text(
    "subject,relation,object,source,status,confidence,note\n"
    f"{nfc('김철수')},{nfc('소속')},AAA,sources/a.md,confirmed,0.9,\n"
    f"{nfd('김철수')},{nfd('소속')},BBB,sources/a.md,confirmed,0.9,\n",
    encoding="utf-8",
)
PY
out="$("$PYTHON" -m factlog vocab --relations --target "$FKB" 2>&1)"
printf '%s' "$out" | grep -qF "(NFC)  [single-valued]" \
  && printf '%s' "$out" | grep -qF "(NFD)  [single-valued]" \
  && ok "both spellings of one relation are labelled with their form" \
  || bad "indistinguishable lines: $(printf '%s' "$out" | tail -3)"
# ...and a KB with no such pair is untouched (control: passes before and after).
out="$("$PYTHON" -m factlog vocab --relations --target "$NKB" 2>&1)"
printf '%s' "$out" | grep -qE "\((NFC|NFD|mixed)\)" \
  && bad "form label printed where no two names share a spelling" \
  || ok "single-spelling KB prints no form label"

# --- the ENTITY list needs the same label (#342) ------------------------------
# The engine folds canonically equivalent entity names into one atom, so this
# list — which counts raw candidate spellings — no longer matches the engine's
# entity count. It is allowed to differ, but not to be unreadable: without a
# label the reader sees two identical-looking lines and counts two entities
# where accepted.dl has one. $FKB above has 김철수 in both forms.
out="$("$PYTHON" -m factlog vocab --entities --target "$FKB" 2>&1)"
n_nfc="$(printf '%s' "$out" | grep -cF "(NFC)" || true)"
n_nfd="$(printf '%s' "$out" | grep -cF "(NFD)" || true)"
[ "$n_nfc" -ge 1 ] && [ "$n_nfd" -ge 1 ] \
  && ok "both spellings of one entity are labelled with their form" \
  || bad "entity lines are indistinguishable: $(printf '%s' "$out" | tail -4)"
# control: no folded pair -> byte-identical to before
out="$("$PYTHON" -m factlog vocab --entities --target "$NKB" 2>&1)"
printf '%s' "$out" | grep -qE "\((NFC|NFD|mixed)\)" \
  && bad "entity form label printed where no two names share a spelling" \
  || ok "single-spelling KB prints no entity form label"

# --- fullwidth relation names are NOT merged by the fold (control) ------------
# NFC only, never NFKC: ＡＢＣ and ABC are different relations. This passes
# before the fold too — it is the guard on how far the fold may reach.
FKB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$FKB" >/dev/null
printf 'x\n' > "$FKB/sources/a.md"
printf '# single-valued\n- ABC\n' > "$FKB/policy/single-valued.md"
printf '%s\n%s\n' "$H" \
  'X,ＡＢＣ,V,sources/a.md,confirmed,0.9,' > "$FKB/facts/candidates.csv"
out="$("$PYTHON" -m factlog vocab --relations --target "$FKB" 2>&1)"
printf '%s' "$out" | grep -qF "[single-valued]" \
  && bad "fullwidth relation wrongly tagged single-valued" \
  || ok "fullwidth relation stays distinct from ASCII (NFC, not NFKC)"

# --- non-KB path errors ------------------------------------------------------
set +e; "$PYTHON" -m factlog vocab --target "$(mktemp -d)" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -ne 0 ] && ok "vocab on a non-KB path errors" || bad "non-KB path should error"

echo ""
echo "========================================"
echo "test_vocab: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
