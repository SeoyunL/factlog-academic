#!/usr/bin/env bash
# tests/test_eject_path_scope.sh — a path-scoped eject stays in its path (#221)
#
# `eject sub/report.docx` also deleted the conversion of a TOP-LEVEL report.docx —
# a source the user never named — and exited 0. The original survived but its
# conversion did not, so it silently became a coverage gap. README:70 promises the
# opposite ("same-stem files in different folders never collide"), and :616 that
# `eject report.docx` never disturbs another original's conversion.
#
# The cause: the provenance map kept only the original's BASENAME, and the path
# branch compared that against the requested basename, so the conversion's mirrored
# subdirectory was never looked at.
#
# Every pin asserts BOTH directions — the named thing went AND the unnamed thing
# stayed. A one-sided check passes for a "matches nothing at all" implementation,
# which is precisely the failure the first version of this fix introduced.
#
#   (a)(b) a nested eject deletes the nested conversion, keeps the top-level one
#   (c)(d) a top-level eject deletes the top-level one, keeps the nested one
#   (e)    a flat conversion whose header records only a basename CANNOT be tied to a
#          path (it may have come from a document outside sources/), so a path request
#          must NOT guess -- it leaves it and names it on stderr
#   (f)    a `./`-prefixed path is not a miss (both sides normalised)
#   (g)    a headerless conversion is still reachable by path
#   (h)    a path + --delete-original deletes THAT original and no other
#   (i)    the message offers an exit that works (name the ref directly)
#   (j)    the warning still prints when nothing else matched (it sat after an early
#          return, so it was dead code in the commonest state)
#   (k)    a headerless flat conversion warns too
#   (l)    --delete-original never does the IRREVERSIBLE half of a job whose
#          reversible half we just refused
#
# Usage: bash tests/test_eject_path_scope.sh

set -euo pipefail

TMP_ROOT="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$TMP_ROOT"' EXIT
export XDG_CONFIG_HOME="$TMP_ROOT/cfg"

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

# Converter availability is decided BEFORE the run, never inferred from "nothing
# was converted" — that inference turns a broken ingest into a green skip.
if ! command -v pandoc >/dev/null 2>&1 && ! command -v textutil >/dev/null 2>&1; then
  echo "SKIP: neither pandoc nor textutil is available; .html cannot be converted here"
  exit 0
fi

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

top_conv()    { find "$1/runs/sources" -maxdepth 1 -type f -name 'report.html.*' 2>/dev/null | grep -q .; }
nested_conv() { find "$1/runs/sources/sub" -type f -name 'report.html.*' 2>/dev/null | grep -q .; }

# Two originals sharing a basename, in different folders.
new_kb() {
  local kb
  kb="$(mktemp -d "$TMP_ROOT/kb.XXXXXX")/wiki"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  mkdir -p "$kb/sources/sub"
  printf 'top\n' > "$kb/sources/report.html"
  printf 'nested\n' > "$kb/sources/sub/report.html"
  # Explicit ingest, not --scan: on this base --scan skips text containers (#222 is
  # a separate branch), and this harness is about eject's path scoping, not scan.
  "$PYTHON" -m factlog ingest "$kb/sources/report.html" --target "$kb" >/dev/null 2>&1
  "$PYTHON" -m factlog ingest "$kb/sources/sub/report.html" --target "$kb" >/dev/null 2>&1
  echo "$kb"
}

KB="$(new_kb)"
if ! top_conv "$KB" || ! nested_conv "$KB"; then
  bad "setup: --scan did not produce both conversions"
  echo "passed: $pass, failed: $fail"
  exit 1
fi

# ------------------------------------------------------------------ (a) (b)
"$PYTHON" -m factlog eject sub/report.html --target "$KB" >/dev/null 2>&1 || true
if nested_conv "$KB"; then
  bad "(a) the nested conversion the user named was NOT deleted"
else
  ok "(a) the nested conversion the user named was deleted"
fi
if top_conv "$KB"; then
  ok "(b) the top-level conversion — never named — survived"
else
  bad "(b) the top-level conversion was deleted: a source the user never named"
fi

# ------------------------------------------------------------------ (c) (d)
KB2="$(new_kb)"
"$PYTHON" -m factlog eject sources/report.html --target "$KB2" >/dev/null 2>&1 || true
if top_conv "$KB2"; then
  bad "(c) the top-level conversion the user named was NOT deleted"
else
  ok "(c) the top-level conversion the user named was deleted"
fi
if nested_conv "$KB2"; then
  ok "(d) the nested conversion — never named — survived"
else
  bad "(d) a top-level request reached into sub/"
fi

# ------------------------------------------------------------------ (e)
# A pre-mirroring KB: a FLAT conversion whose header records only a basename. The
# subdir is not recorded anywhere, so reconstructing one would be a GUESS — and the
# guess is unsafe, because this state is byte-identical to a conversion made from a
# document that was never under sources/ at all (README:84 documents that ingest
# form) or from an original since deleted. Guessing by basename is exactly what
# #221 reported: a conversion of a document the user never named, deleted with
# exit 0. So a path request must NOT match it — and must say so, or the
# under-ejection is just a quieter kind of wrong.
KB3="$(mktemp -d "$TMP_ROOT/kb.XXXXXX")/wiki"
"$PYTHON" -m factlog init --target "$KB3" >/dev/null
mkdir -p "$KB3/sources/sub"
printf 'nested\n' > "$KB3/sources/sub/report.html"
printf -- '<!-- ingested-by-factlog | source: report.html | converter: pandoc -->\nold\n' \
  > "$KB3/runs/sources/report.md"
OUT_E="$("$PYTHON" -m factlog eject sub/report.html --target "$KB3" 2>&1 || true)"
if [ -f "$KB3/runs/sources/report.md" ]; then
  ok "(e) an unattributable flat conversion is not deleted by a path request"
else
  bad "(e) #221 is back: guessed a flat conversion's origin from its basename"
fi
if printf '%s' "$OUT_E" | grep -q "NOT ejecting runs/sources/report.md"; then
  ok "(e) the un-ejected conversion is named, with a working exit"
else
  bad "(e) left the conversion behind SILENTLY — the user cannot tell"
fi

# ------------------------------------------------------------------ (f)
KB4="$(new_kb)"
"$PYTHON" -m factlog eject ./sub/report.html --target "$KB4" >/dev/null 2>&1 || true
if nested_conv "$KB4"; then
  bad "(f) a ./-prefixed path missed — the two sides are not normalised the same way"
else
  ok "(f) a ./-prefixed path matches"
fi

# ------------------------------------------------------------------ (g)
KB5="$(mktemp -d "$TMP_ROOT/kb.XXXXXX")/wiki"
"$PYTHON" -m factlog init --target "$KB5" >/dev/null
mkdir -p "$KB5/sources/sub" "$KB5/runs/sources/sub"
printf 'nested\n' > "$KB5/sources/sub/report.html"
printf 'no provenance header\n' > "$KB5/runs/sources/sub/report.html.md"
"$PYTHON" -m factlog eject sub/report.html --target "$KB5" >/dev/null 2>&1 || true
if [ -f "$KB5/runs/sources/sub/report.html.md" ]; then
  bad "(g) a headerless conversion is unreachable by path"
else
  ok "(g) a headerless conversion is reachable by its mirrored path"
fi

# ------------------------------------------------------------------ (h)
KB6="$(new_kb)"
"$PYTHON" -m factlog eject sub/report.html --delete-original --target "$KB6" >/dev/null 2>&1 || true
if [ -f "$KB6/sources/sub/report.html" ]; then
  bad "(h) --delete-original left the original in place while reporting success"
else
  ok "(h) a path + --delete-original deletes the original"
fi
if [ -f "$KB6/sources/report.html" ]; then
  ok "(h) --delete-original did not touch the same-named original elsewhere"
else
  bad "(h) --delete-original deleted an original the user never named"
fi

# --- (i) the ambiguous flat conversion: origin is NOT knowable from a bare header ---
# A flat conversion whose original was never in sources/ (README documents this:
# `factlog ingest report.docx --target ~/wiki`) is indistinguishable from a
# pre-mirroring legacy conversion. Guessing by basename re-created #221: a document
# the user never named got deleted with exit 0. We must NOT match it, and must SAY SO.
KB7="$(mktemp -d "$TMP_ROOT/kb.XXXXXX")/wiki"
"$PYTHON" -m factlog init --target "$KB7" >/dev/null 2>&1
mkdir -p "$KB7/sources/sub"
printf 'nested\n' > "$KB7/sources/sub/report.md"
# a flat conversion carrying a bare-name header, from a document outside sources/
mkdir -p "$KB7/runs/sources"
printf '<!-- ingested-by-factlog | source: report.md | converter: pandoc -->\nouter body\n' > "$KB7/runs/sources/report.md"
printf 'subject,relation,object,source,status,confidence,note\n' > "$KB7/facts/candidates.csv"
printf 'X,r,Y,sources/sub/report.md,accepted,0.9,\n' >> "$KB7/facts/candidates.csv"

OUT="$(FACTLOG_ROOT="$KB7" "$PYTHON" -m factlog eject sub/report.md 2>&1)"
if [ -f "$KB7/runs/sources/report.md" ]; then
  ok "(i) a flat conversion of an unnamed document is NOT deleted"
else
  bad "(i) #221 is back: deleted a conversion the user never named"
fi
if printf '%s' "$OUT" | grep -q "NOT ejecting runs/sources/report.md"; then
  ok "(i) the skipped conversion is named, so under-ejection is not silent"
else
  bad "(i) skipped the conversion silently"
fi
# The message must offer a way out that actually WORKS. `ingest --scan --force` does
# not: it adds a mirrored conversion beside the flat one and leaves the flat one, and
# the facts citing it, in place — so following the advice returns you to this warning.
# Naming the ref directly is the route measured to work.
if printf '%s' "$OUT" | grep -q "factlog eject runs/sources/report.md"; then
  ok "(i) the message offers an exit that works — name the ref directly"
else
  bad "(i) no usable exit offered"
fi

# --- (j) the warning must survive the early return ------------------------------
# When the path matches nothing ELSE -- the commonest state on a legacy KB -- a
# warning printed after `if not matched: return 1` is dead code, and the user gets a
# bare "nothing to eject" while the conversion sits right there.
KB8="$(mktemp -d "$TMP_ROOT/kb.XXXXXX")/wiki"
"$PYTHON" -m factlog init --target "$KB8" >/dev/null
printf -- '<!-- ingested-by-factlog | source: report.html | converter: pandoc -->\nbody\n' \
  > "$KB8/runs/sources/report.html.md"
OUT_J="$("$PYTHON" -m factlog eject docs/report.html --target "$KB8" 2>&1 || true)"
if printf '%s' "$OUT_J" | grep -q "NOT ejecting runs/sources/report.html.md"; then
  ok "(j) the warning still prints when nothing else matched"
else
  bad "(j) the warning is dead code on the no-match path"
fi
if printf '%s' "$OUT_J" | grep -q "scan --force"; then
  bad "(j) still advertises --scan --force, which does not migrate anything"
else
  ok "(j) does not advertise a migration that does not migrate"
fi

# --- (k) a HEADERLESS flat conversion warns too ---------------------------------
# conv_origin has no entry for it, so keying the warning on conv_origin left the
# commonest legacy shape silently un-ejected — a quieter kind of wrong.
KB9="$(mktemp -d "$TMP_ROOT/kb.XXXXXX")/wiki"
"$PYTHON" -m factlog init --target "$KB9" >/dev/null
mkdir -p "$KB9/sources/sub"
printf 'nested\n' > "$KB9/sources/sub/report.html"
printf 'no header here\n' > "$KB9/runs/sources/report.md"
OUT_K="$("$PYTHON" -m factlog eject sub/report.html --target "$KB9" 2>&1 || true)"
if [ -f "$KB9/runs/sources/report.md" ]; then
  ok "(k) a headerless flat conversion is not deleted by a path request"
else
  bad "(k) deleted a headerless flat conversion the user never named"
fi
if printf '%s' "$OUT_K" | grep -q "NOT ejecting runs/sources/report.md"; then
  ok "(k) a headerless flat conversion is named, not skipped silently"
else
  bad "(k) left a headerless flat conversion behind SILENTLY"
fi

# --- (l) never do the IRREVERSIBLE half of a job whose reversible half we refused --
# Deleting the original while leaving a conversion we could not attribute strands the
# conversion's facts in accepted.dl with no source file, and --purge takes the audit
# trail too. The user cannot get the original back. main deleted both; refusing one
# and doing the other is worse than either.
KB10="$(mktemp -d "$TMP_ROOT/kb.XXXXXX")/wiki"
"$PYTHON" -m factlog init --target "$KB10" >/dev/null
mkdir -p "$KB10/sources/sub"
printf 'nested\n' > "$KB10/sources/sub/report.html"
printf -- '<!-- ingested-by-factlog | source: report.html | converter: pandoc -->\nbody\n' \
  > "$KB10/runs/sources/report.md"
printf 'subject,relation,object,source,status,confidence,note\n' > "$KB10/facts/candidates.csv"
printf 'FROM_CONV,rel,B,runs/sources/report.md,accepted,0.9,\n' >> "$KB10/facts/candidates.csv"
FACTLOG_ROOT="$KB10" "$PYTHON" tools/compile_facts.py >/dev/null 2>&1

RC_L=0
OUT_L="$(FACTLOG_ROOT="$KB10" "$PYTHON" -m factlog eject sources/sub/report.html --purge --delete-original 2>&1)" || RC_L=$?
if [ -f "$KB10/sources/sub/report.html" ]; then
  ok "(l) the original is NOT deleted while an unattributable conversion survives"
else
  bad "(l) deleted the original and left the conversion — facts are stranded"
fi
if [ "$RC_L" -ne 0 ]; then
  ok "(l) the refusal is a non-zero exit, not a silent half-job"
else
  bad "(l) exited 0 after refusing half the work"
fi
if printf '%s' "$OUT_L" | grep -q "refusing --delete-original"; then
  ok "(l) the refusal says why"
else
  bad "(l) refused without saying why"
fi

# and the two-step way out actually completes the job
FACTLOG_ROOT="$KB10" "$PYTHON" -m factlog eject runs/sources/report.md >/dev/null 2>&1
RC_L2=0
FACTLOG_ROOT="$KB10" "$PYTHON" -m factlog eject sources/sub/report.html --purge --delete-original >/dev/null 2>&1 || RC_L2=$?
if [ ! -f "$KB10/sources/sub/report.html" ] && [ "$RC_L2" -eq 0 ]; then
  ok "(l) removing the conversion first lets --delete-original complete"
else
  bad "(l) the advised two-step way out does not work — a dead end"
fi

echo "---"
echo "passed: $pass, failed: $fail"
[ "$fail" -eq 0 ]
