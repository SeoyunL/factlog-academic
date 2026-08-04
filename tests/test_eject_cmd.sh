#!/usr/bin/env bash
# tests/test_eject_cmd.sh — `factlog eject`, the inverse of ingest
#
# Pins (XDG-isolated; synthetic data; no pyrewire needed — eject recompiles
# accepted.dl deterministically via compile_facts):
#   - naming a binary original (deck.pptx) also matches its runs/sources/<stem>
#     conversion; eject deletes the conversion, strips the runs/*.json rows
#     (removing the now-empty file), and supersedes the citing candidate row
#   - the original under sources/ is KEPT by default (with a note); accepted.dl
#     drops the retired fact but keeps the others
#   - --dry-run changes nothing
#   - --purge deletes the candidate row instead of superseding it
#   - --delete-original also removes the user's original under sources/
#   - a bare stem matches; an unknown name errors (rc != 0); non-KB path errors
#   - naming a *path* stays inside that path: `eject sub/report.html` never
#     reaches a same-name original in another directory (#324), while a bare
#     filename keeps matching every directory
#
# Usage: bash tests/test_eject_cmd.sh

set -euo pipefail

export XDG_CONFIG_HOME="$(mktemp -d)/factlog-test-cfg"  # isolate active-KB config (#62)

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

H="subject,relation,object,source,status,confidence,note"

# Scaffold a fresh KB with a binary original + its conversion + a text source,
# candidate rows citing each, and a runs/*.json extraction file for the deck.
seed() {  # $1 = KB path
  local kb="$1"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  printf 'PK\003\004 fake pptx\000' > "$kb/sources/deck.pptx"
  printf '<!-- ingested-by-factlog | source: deck.pptx | converter: factlog-pptx | date: 2026-01-01T00:00:00Z -->\nslide text\n' \
    > "$kb/runs/sources/deck.md"
  printf 'plain text\n' > "$kb/sources/notes.md"
  printf '%s\n%s\n%s\n%s\n' "$H" \
    'A,rel,B,runs/sources/deck.md,confirmed,0.9,' \
    'C,rel,D,sources/notes.md,confirmed,0.9,' \
    'E,rel,F,sources/notes.md,confirmed,0.9,' > "$kb/facts/candidates.csv"
  printf '[{"subject":"A","relation":"rel","object":"B","source":"runs/sources/deck.md","status":"candidate","confidence":0.9,"note":""}]\n' \
    > "$kb/runs/2026-01-01-deck.json"
}

# --- default: supersede; conversion + run file removed; original kept ----------
KB="$(mktemp -d)/wiki"; seed "$KB"
out="$("$PYTHON" -m factlog eject deck.pptx --target "$KB" 2>&1)"
printf '%s\n' "$out"; echo "---"
[ ! -f "$KB/runs/sources/deck.md" ] && ok "conversion deleted" || bad "conversion still present"
[ ! -f "$KB/runs/2026-01-01-deck.json" ] && ok "emptied runs/*.json removed" || bad "runs json still present"
[ -f "$KB/sources/deck.pptx" ] && ok "original kept by default" || bad "original was deleted without --delete-original"
grep -q "A,rel,B,runs/sources/deck.md,superseded," "$KB/facts/candidates.csv" && ok "citing row marked superseded" || bad "row not superseded"
grep -q '"A", "rel", "B"' "$KB/facts/accepted.dl" && bad "retired fact still in accepted.dl" || ok "retired fact dropped from accepted.dl"
grep -q '"C", "rel", "D"' "$KB/facts/accepted.dl" && ok "unrelated fact preserved in accepted.dl" || bad "unrelated fact lost"
printf '%s' "$out" | grep -qF "matched source ref" && printf '%s' "$out" | grep -qF "runs/sources/deck.md" && printf '%s' "$out" | grep -qF "sources/deck.pptx" \
  && ok "binary name matches both original and its conversion" || bad "stem-conversion match missing"

# --- --dry-run changes nothing -----------------------------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
before="$(cat "$KB/facts/candidates.csv")"
"$PYTHON" -m factlog eject deck.pptx --target "$KB" --dry-run >/dev/null 2>&1
[ -f "$KB/runs/sources/deck.md" ] && [ -f "$KB/runs/2026-01-01-deck.json" ] && [ "$(cat "$KB/facts/candidates.csv")" = "$before" ] \
  && ok "--dry-run leaves files and candidates.csv untouched" || bad "--dry-run mutated state"

# --- --purge deletes the candidate row ---------------------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
"$PYTHON" -m factlog eject deck.pptx --target "$KB" --purge >/dev/null 2>&1
grep -q "runs/sources/deck.md" "$KB/facts/candidates.csv" && bad "--purge left the row" || ok "--purge deletes the candidate row"

# --- --delete-original removes the user's original ----------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
"$PYTHON" -m factlog eject notes.md --target "$KB" --purge --delete-original >/dev/null 2>&1
[ ! -f "$KB/sources/notes.md" ] && ok "--delete-original removes the original" || bad "original not deleted"
grep -q "sources/notes.md" "$KB/facts/candidates.csv" && bad "notes rows not purged" || ok "text-source rows purged"

# --- bare stem matches; unknown name errors ----------------------------------
KB="$(mktemp -d)/wiki"; seed "$KB"
"$PYTHON" -m factlog eject deck --target "$KB" >/dev/null 2>&1 \
  && [ ! -f "$KB/runs/sources/deck.md" ] && ok "bare stem 'deck' matches the source" || bad "bare stem did not match"
set +e; "$PYTHON" -m factlog eject nope --target "$KB" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -ne 0 ] && ok "unknown source name errors (rc != 0)" || bad "unknown name should error"

# --- a sibling sharing the stem is NOT pulled in by the conversion's provenance -
# report.pptx was ingested (provenance source: report.pptx); report.docx was not.
# Ejecting report.docx must not delete report.pptx's conversion or retire its fact.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'PK\003\004\000' > "$KB/sources/report.pptx"
printf 'PK\003\004\000' > "$KB/sources/report.docx"
printf '<!-- ingested-by-factlog | source: report.pptx | converter: factlog-pptx | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/report.md"
printf '%s\n%s\n' "$H" 'A,rel,B,runs/sources/report.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject report.docx --target "$KB" >/dev/null 2>&1 || true
[ -f "$KB/runs/sources/report.md" ] && ok "ejecting report.docx keeps report.pptx's conversion (provenance-tied)" || bad "wrong conversion deleted"
grep -q "runs/sources/report.md,confirmed," "$KB/facts/candidates.csv" && ok "report.pptx's fact not retired by ejecting report.docx" || bad "wrong fact retired"

# --- a full KB-relative path does NOT match a same-name file in another dir ----
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/a" "$KB/sources/b"
printf 'a\n' > "$KB/sources/a/dup.md"; printf 'b\n' > "$KB/sources/b/dup.md"
printf '%s\n%s\n%s\n' "$H" \
  'A,rel,B,sources/a/dup.md,confirmed,0.9,' \
  'C,rel,D,sources/b/dup.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject sources/a/dup.md --target "$KB" --delete-original >/dev/null 2>&1 || true
[ ! -f "$KB/sources/a/dup.md" ] && [ -f "$KB/sources/b/dup.md" ] && ok "full path ejects only that file, not the same-name sibling" || bad "full path matched across directories"
grep -q "sources/b/dup.md,confirmed," "$KB/facts/candidates.csv" && ok "sibling's fact preserved" || bad "sibling fact wrongly retired"

# =============================================================================
# #324: naming a path must not reach a same-name original in another directory
# =============================================================================

# Two same-name originals (sources/report.html and sources/sub/report.html), each
# with its mirrored conversion. $1 = KB, $2 = header style (path|legacy): the
# nested conversion's provenance records either the #214 sources-relative path or
# a legacy bare basename. Both must select identically — the conversion's own
# mirrored location is what pairs it with its original.
seed_dup() {  # $1 = KB path, $2 = path|legacy
  local kb="$1" nested_src="sub/report.html"
  [ "$2" = "legacy" ] && nested_src="report.html"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  mkdir -p "$kb/sources/sub" "$kb/runs/sources/sub"
  printf '<html>top</html>\n' > "$kb/sources/report.html"
  printf '<html>nested</html>\n' > "$kb/sources/sub/report.html"
  printf '<!-- ingested-by-factlog | source: report.html | converter: pandoc | date: 2026-01-01T00:00:00Z -->\ntop\n' \
    > "$kb/runs/sources/report.html.md"
  printf '<!-- ingested-by-factlog | source: %s | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nnested\n' \
    "$nested_src" > "$kb/runs/sources/sub/report.html.md"
  printf '%s\n%s\n%s\n%s\n%s\n' "$H" \
    'A,rel,B,runs/sources/report.html.md,confirmed,0.9,' \
    'C,rel,D,runs/sources/sub/report.html.md,confirmed,0.9,' \
    'E,rel,F,sources/report.html,confirmed,0.9,' \
    'G,rel,H,sources/sub/report.html,confirmed,0.9,' > "$kb/facts/candidates.csv"
}

# A KB whose flat conversion really did come from an original outside sources/:
# no original *anywhere* under sources/ bears that basename, so the pairing is
# unambiguous. The mirrored pair deliberately uses a different filename — give
# it the same one and the flat conversion becomes attributable to it instead,
# which is the ambiguity the guard refuses.
seed_outside() {  # $1 = KB path
  local kb="$1"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  mkdir -p "$kb/sources/sub" "$kb/runs/sources/sub"
  printf '<html>nested</html>\n' > "$kb/sources/sub/other.html"
  printf '<!-- ingested-by-factlog | source: report.html | converter: pandoc | date: 2026-01-01T00:00:00Z -->\noutside\n' \
    > "$kb/runs/sources/report.html.md"
  printf '<!-- ingested-by-factlog | source: sub/other.html | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nnested\n' \
    > "$kb/runs/sources/sub/other.html.md"
  printf '%s\n%s\n%s\n' "$H" \
    'A,rel,B,runs/sources/report.html.md,confirmed,0.9,' \
    'C,rel,D,runs/sources/sub/other.html.md,confirmed,0.9,' > "$kb/facts/candidates.csv"
}

# --- a sources-relative path ejects only the conversion made from that path ----
for style in path legacy; do
  KB="$(mktemp -d)/wiki"; seed_dup "$KB" "$style"
  "$PYTHON" -m factlog eject sub/report.html --target "$KB" >/dev/null 2>&1
  [ ! -f "$KB/runs/sources/sub/report.html.md" ] && [ -f "$KB/runs/sources/report.html.md" ] \
    && ok "[$style header] 'sub/report.html' ejects the nested conversion only" \
    || bad "[$style header] path eject hit the same-name conversion in another directory"
  grep -q "A,rel,B,runs/sources/report.html.md,confirmed," "$KB/facts/candidates.csv" \
    && ok "[$style header] the top-level conversion's fact is not retired" \
    || bad "[$style header] unrequested fact retired"
  [ -f "$KB/sources/report.html" ] && [ -f "$KB/sources/sub/report.html" ] \
    && ok "[$style header] both originals kept" || bad "[$style header] an original was deleted"
done

# --- './name' narrows to the root-level original's conversion -----------------
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
"$PYTHON" -m factlog eject ./report.html --target "$KB" >/dev/null 2>&1
[ ! -f "$KB/runs/sources/report.html.md" ] && [ -f "$KB/runs/sources/sub/report.html.md" ] \
  && ok "'./report.html' ejects the root-level conversion only" || bad "'./report.html' reached into sub/"

# --- --delete-original on a sources-relative path says what it would need -----
# 'sub/report.html' names the original a conversion was made *from*, so it
# selects the conversion and never the original — deliberate, and documented.
# But --delete-original then printed 0 with no explanation, leaving no way to
# tell a deliberate no-op from a missed file. Report the spelling that includes
# the original instead of widening what this one deletes.
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
out="$("$PYTHON" -m factlog eject sub/report.html --target "$KB" --delete-original 2>&1)"
printf '%s' "$out" | grep -qF "sources/sub/report.html is on disk but was not named" \
  && ok "--delete-original explains why a sources-relative path deleted no original" \
  || bad "--delete-original silently reported 0 originals"
[ -f "$KB/sources/sub/report.html" ] && [ -f "$KB/sources/report.html" ] \
  && ok "the hint does not widen what a sources-relative path deletes" \
  || bad "sources-relative --delete-original deleted an original"

# --- a KB-relative 'sources/...' path matches that original + its conversion ---
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
out="$("$PYTHON" -m factlog eject sources/report.html --target "$KB" --delete-original 2>&1)"
[ ! -f "$KB/sources/report.html" ] && [ ! -f "$KB/runs/sources/report.html.md" ] \
  && ok "'sources/report.html' ejects that original and its conversion" || bad "KB-relative path eject incomplete"
[ -f "$KB/sources/sub/report.html" ] && [ -f "$KB/runs/sources/sub/report.html.md" ] \
  && ok "'sources/report.html' leaves the sub/ pair untouched" || bad "KB-relative path eject hit sub/"

# --- a bare filename stays deliberately wide (a filename is not a path) -------
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
out="$("$PYTHON" -m factlog eject report.html --target "$KB" --dry-run 2>&1)"
[ "$(printf '%s' "$out" | grep -c '^  - ')" -eq 4 ] \
  && ok "a bare filename still matches every source with that name (4 refs)" \
  || bad "bare filename matching narrowed: $(printf '%s' "$out" | grep -c '^  - ') refs"

# --- an absolute path inside the KB resolves to its KB-relative ref -----------
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
"$PYTHON" -m factlog eject "$KB/sources/sub/report.html" --target "$KB" >/dev/null 2>&1
[ ! -f "$KB/runs/sources/sub/report.html.md" ] && [ -f "$KB/runs/sources/report.html.md" ] \
  && ok "an absolute path under sources/ ejects only its own conversion" || bad "absolute path matched by basename"

# --- an absolute path + --delete-original removes exactly that one original ---
# Reducing an absolute path to its KB-relative ref makes it match the original
# itself, so --delete-original now reaches an original that a basename fallback
# never selected. It must stay at exactly one file: the same-name sibling in the
# other directory keeps both its original and its conversion.
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
"$PYTHON" -m factlog eject "$KB/sources/sub/report.html" --target "$KB" --delete-original >/dev/null 2>&1
[ ! -f "$KB/sources/sub/report.html" ] && [ ! -f "$KB/runs/sources/sub/report.html.md" ] \
  && ok "absolute path + --delete-original removes that original and its conversion" \
  || bad "absolute path + --delete-original did not remove the named original"
[ -f "$KB/sources/report.html" ] && [ -f "$KB/runs/sources/report.html.md" ] \
  && ok "absolute path + --delete-original leaves the same-name sibling intact" \
  || bad "--delete-original deleted a file in another directory"

# --- a KB whose sources/ is a symlink is still recognised as inside the KB -----
# ingest decides containment by filesystem identity ((target / "sources").resolve(),
# cli.py:2050). A string-prefix test on the resolved path disagrees with that
# oracle as soon as sources/ is a symlink: the argument looks like it lies
# outside the KB and drops to the basename fallback, which deletes the same-name
# conversion in *another* directory while sparing the one that was named.
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
LINKED="$(mktemp -d)/docs"; mv "$KB/sources" "$LINKED"; ln -s "$LINKED" "$KB/sources"
"$PYTHON" -m factlog eject "$KB/sources/sub/report.html" --target "$KB" --delete-original >/dev/null 2>&1
[ ! -f "$KB/sources/sub/report.html" ] && [ ! -f "$KB/runs/sources/sub/report.html.md" ] \
  && ok "a symlinked sources/ still reduces an absolute path to its KB ref" \
  || bad "symlinked sources/: the named pair survived"
[ -f "$KB/sources/report.html" ] && [ -f "$KB/runs/sources/report.html.md" ] \
  && ok "a symlinked sources/ does not reach the same-name sibling" \
  || bad "symlinked sources/: deleted a file in another directory"

# --- a --target spelled in a different case on a case-insensitive filesystem ---
# Path.resolve() does not fold case on macOS/Windows, so a string-prefix test
# misses and falls back to the basename. Nothing unusual has to be typed: only
# --target is spelled differently from the argument. Skipped where the
# filesystem is case-sensitive (Linux CI), because there the two spellings name
# genuinely different directories.
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
if [ -d "$(dirname "$KB")/WIKI" ]; then
  "$PYTHON" -m factlog eject "$KB/sources/sub/report.html" \
    --target "$(dirname "$KB")/WIKI" --delete-original >/dev/null 2>&1
  [ ! -f "$KB/sources/sub/report.html" ] && [ ! -f "$KB/runs/sources/sub/report.html.md" ] \
    && ok "a case-different --target still reduces the argument to its KB ref" \
    || bad "case-different --target: the named pair survived"
  [ -f "$KB/sources/report.html" ] && [ -f "$KB/runs/sources/report.html.md" ] \
    && ok "a case-different --target does not reach the same-name sibling" \
    || bad "case-different --target: deleted a file in another directory"
fi

# --- deleting an original that a flat conversion depends on is announced ------
# ingest and eject do not agree about containment: ingest reduces with
# relative_to() on resolved strings, so a --target spelled in a different case
# makes it treat an in-sources/ file as outside and write a *flat* conversion
# with a bare-basename header. eject resolves the same argument *into* sources/
# and cannot pair that conversion. Deleting the original would silently orphan
# it, so say so.
BASE="$(mktemp -d)"; KB="$BASE/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/sub"
printf '<html>nested</html>\n' > "$KB/sources/sub/report.html"
if [ -d "$BASE/Wiki" ]; then    # case-insensitive filesystem only
  "$PYTHON" -m factlog ingest "$KB/sources/sub/report.html" --target "$BASE/Wiki" >/dev/null 2>&1
  [ -f "$KB/runs/sources/report.html.md" ] \
    && ok "ingest with a case-different --target writes a FLAT conversion" \
    || bad "fixture wrong: expected a flat conversion from ingest"
  printf '%s\n%s\n' "$H" 'A,rel,B,runs/sources/report.html.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
  out="$("$PYTHON" -m factlog eject "$KB/sources/sub/report.html" --target "$BASE/Wiki" --delete-original 2>&1)"
  printf '%s' "$out" | grep -qF "runs/sources/report.html.md" \
    && printf '%s' "$out" | grep -qF "will have no original left" \
    && ok "deleting an original warns about the flat conversion it orphans" \
    || bad "orphaning a flat conversion was silent"
fi

# --- an unresolvable path errors like any unknown name (no traceback) ---------
# On Python 3.11/3.12 resolve() re-raises ELOOP as RuntimeError, not OSError;
# letting it escape turned an ordinary no-match into a crash. 3.13+ returns the
# path unresolved instead, so this assertion only bites on the older two — which
# includes the interpreter CI pins.
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
LOOP="$(mktemp -d)"; ln -s "$LOOP/b" "$LOOP/a"; ln -s "$LOOP/a" "$LOOP/b"
set +e
err="$("$PYTHON" -m factlog eject "$LOOP/a/absent.html" --target "$KB" --dry-run 2>&1)"; rc=$?
set -e
[ "$rc" -ne 0 ] && ! printf '%s' "$err" | grep -qF "Traceback" \
  && ok "a symlink-loop path reports no match instead of crashing" \
  || bad "symlink-loop path crashed: $(printf '%s' "$err" | tail -1)"

# --- ...and it deletes nothing, on every interpreter --------------------------
# The assertion above is silent on 3.13+, where resolve() raises nothing at all.
# What must hold everywhere is that an unresolvable argument reaches no file: an
# unresolved path is still absolute, still lands outside the KB, and so still
# meets the basename fallback. Name the loop after a basename that two live
# conversions share, so a degraded match has something to destroy.
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
LOOP="$(mktemp -d)"; ln -s "$LOOP/b" "$LOOP/a"; ln -s "$LOOP/a" "$LOOP/b"
set +e
"$PYTHON" -m factlog eject "$LOOP/a/report.html" --target "$KB" >/dev/null 2>&1; rc=$?
set -e
[ "$rc" -ne 0 ] && [ -f "$KB/runs/sources/report.html.md" ] && [ -f "$KB/runs/sources/sub/report.html.md" ] \
  && ok "a symlink-loop path matching a live basename still deletes nothing" \
  || bad "symlink-loop path deleted a conversion by basename"

# --- an absolute original outside the KB matches only a FLAT conversion -------
# ingest gives a path outside sources/ no subtree to mirror, so its conversion is
# flat; a mirrored conversion can never have come from that path. seed_outside
# is the honest fixture for this: the KB holds no sources/report.html, so the
# flat conversion really is the one that argument produced.
KB="$(mktemp -d)/wiki"; seed_outside "$KB"
OUTDIR="$(mktemp -d)"; printf '<html>elsewhere</html>\n' > "$OUTDIR/report.html"
"$PYTHON" -m factlog eject "$OUTDIR/report.html" --target "$KB" >/dev/null 2>&1
[ ! -f "$KB/runs/sources/report.html.md" ] && [ -f "$KB/runs/sources/sub/other.html.md" ] \
  && ok "an outside-the-KB original matches its flat conversion, not a mirrored one" \
  || bad "outside-the-KB path reached a mirrored conversion"

# --- ...but not one already paired with an in-KB original of the same name ----
# ingest stores only src.name for an original outside sources/ (cli.py:2186), so
# a flat conversion whose header says "report.html" cannot say *which*
# report.html. When the KB has a sources/report.html of its own, that file — not
# some path elsewhere on the disk — is what the conversion was made from.
# Deletion is unprompted and rc=0, so the ambiguous request must select nothing.
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
OUTDIR="$(mktemp -d)"; printf '<html>elsewhere</html>\n' > "$OUTDIR/report.html"
set +e
"$PYTHON" -m factlog eject "$OUTDIR/report.html" --target "$KB" --purge >/dev/null 2>&1; rc=$?
set -e
[ "$rc" -ne 0 ] && [ -f "$KB/runs/sources/report.html.md" ] \
  && grep -q "A,rel,B,runs/sources/report.html.md,confirmed," "$KB/facts/candidates.csv" \
  && ok "an outside path does not claim a flat conversion paired with sources/" \
  || bad "outside path purged a conversion paired with an in-KB original"

# --- ...including when the in-KB original lives in a subdirectory -------------
# The original claiming that basename need not sit at the top of sources/. A
# flat conversion paired with sources/sub/report.html is exactly what --orphans
# already treats as paired (it compares against every source basename, not just
# the top-level ones), so the two must not disagree inside one command.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/sub" "$KB/runs/sources"
printf '<html>nested</html>\n' > "$KB/sources/sub/report.html"
printf '<!-- ingested-by-factlog | source: report.html | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nflat\n' \
  > "$KB/runs/sources/report.html.md"
printf '%s\n%s\n' "$H" 'A,rel,B,runs/sources/report.html.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
OUTDIR="$(mktemp -d)"; printf '<html>elsewhere</html>\n' > "$OUTDIR/report.html"
set +e
"$PYTHON" -m factlog eject "$OUTDIR/report.html" --target "$KB" --purge >/dev/null 2>&1; rc=$?
set -e
[ "$rc" -ne 0 ] && [ -f "$KB/runs/sources/report.html.md" ] \
  && grep -q "A,rel,B,runs/sources/report.html.md,confirmed," "$KB/facts/candidates.csv" \
  && ok "an outside path does not claim a flat conversion paired with a subdir original" \
  || bad "outside path purged a conversion paired with sources/sub/"

# --- a leading '//' is absolute, and must not degrade to a basename match -----
# Path("//sub/report.html").is_absolute() is True on POSIX, so it takes the
# absolute branch, resolves to /sub/report.html, and reaches nothing in the KB.
# Before the guard it fell back to the basename and deleted the *top-level*
# conversion — so 'sub//report.html' and '//sub/report.html' deleted different
# files.
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
set +e
"$PYTHON" -m factlog eject "//sub/report.html" --target "$KB" >/dev/null 2>&1; rc=$?
set -e
[ "$rc" -ne 0 ] && [ -f "$KB/runs/sources/report.html.md" ] && [ -f "$KB/runs/sources/sub/report.html.md" ] \
  && ok "'//sub/report.html' selects nothing instead of falling back to the basename" \
  || bad "'//sub/report.html' deleted a conversion by basename"

# --- a path is compared as written: '..' and case differences do not match ----
# Deliberate: eject never normalises a path away from the form a provenance
# header records, and never case-folds. Both now select nothing (rc != 0)
# instead of falling back to a basename match.
KB="$(mktemp -d)/wiki"; seed_dup "$KB" path
set +e
"$PYTHON" -m factlog eject sub/../report.html --target "$KB" --dry-run >/dev/null 2>&1; rc=$?
"$PYTHON" -m factlog eject SUB/report.html --target "$KB" --dry-run >/dev/null 2>&1; rc2=$?
set -e
[ "$rc" -ne 0 ] && ok "a '..' path selects nothing instead of falling back to the basename" \
  || bad "'..' path still matched by basename"
[ "$rc2" -ne 0 ] && ok "a case-different path selects nothing (no case folding)" \
  || bad "case-different path matched"

# --- a candidates.csv whose header lacks 'status' is not truncated -------------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'plain\n' > "$KB/sources/x.md"; printf 'plain\n' > "$KB/sources/y.md"
printf 'subject,relation,object,source\n%s\n%s\n' \
  'A,rel,B,sources/x.md' 'C,rel,D,sources/y.md' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject x.md --target "$KB" >/dev/null 2>&1 || true
grep -q "sources/y.md" "$KB/facts/candidates.csv" && ok "rows preserved when header lacks a status column (no truncation)" || bad "candidates.csv truncated on missing status column"

# =============================================================================
# Fact mode: eject a single fact, leaving the source in place (#74)
# =============================================================================

# Seed a text source with two facts + a runs/*.json asserting both.
seed_facts() {  # $1 = KB path
  local kb="$1"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  printf 'plain\n' > "$kb/sources/a.md"
  printf '%s\n%s\n%s\n' "$H" \
    'X,wrongrel,Y,sources/a.md,confirmed,0.9,' \
    'X,goodrel,Z,sources/a.md,confirmed,0.9,' > "$kb/facts/candidates.csv"
  printf '[{"subject":"X","relation":"wrongrel","object":"Y","source":"sources/a.md","status":"confirmed","confidence":0.9,"note":""},{"subject":"X","relation":"goodrel","object":"Z","source":"sources/a.md","status":"confirmed","confidence":0.9,"note":""}]\n' \
    > "$kb/runs/r.json"
}

# --- default (supersede): retire one fact, keep source + runs + other fact -----
KB="$(mktemp -d)/wiki"; seed_facts "$KB"
"$PYTHON" -m factlog eject --fact X wrongrel Y --target "$KB" >/dev/null 2>&1
grep -q "X,wrongrel,Y,sources/a.md,superseded," "$KB/facts/candidates.csv" && ok "fact mode: matched triple superseded" || bad "fact not superseded"
grep -q "X,goodrel,Z,sources/a.md,confirmed," "$KB/facts/candidates.csv" && ok "fact mode: other fact untouched" || bad "other fact altered"
[ -f "$KB/sources/a.md" ] && ok "fact mode: source kept" || bad "source deleted in fact mode"
grep -q "wrongrel" "$KB/runs/r.json" && ok "fact mode default: runs/*.json kept (durable supersede)" || bad "runs stripped on default supersede"
grep -q '"X", "goodrel", "Z"' "$KB/facts/accepted.dl" && ! grep -q '"X", "wrongrel", "Y"' "$KB/facts/accepted.dl" \
  && ok "fact mode: accepted.dl drops only the retired fact" || bad "accepted.dl wrong after fact eject"

# --- supersede is durable across a re-merge -----------------------------------
"$PYTHON" tools/merge_candidates.py --wiki "$KB" >/dev/null 2>&1
grep -q "X,wrongrel,Y,sources/a.md,superseded," "$KB/facts/candidates.csv" && ok "fact mode: supersede survives re-merge" || bad "supersede lost after re-merge"

# --- supersede survives re-merge even when the section anchor drifts -----------
# candidate cites sources/a.md#sec3; a later run re-asserts the bare path. The
# supersede preservation key is anchor-insensitive, so the fact stays retired.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'plain\n' > "$KB/sources/a.md"
printf '%s\n%s\n' "$H" 'X,wrongrel,Y,sources/a.md#sec3,confirmed,0.9,' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject --fact X wrongrel Y --target "$KB" >/dev/null 2>&1
# next sync re-asserts the same triple from the same file, anchor dropped
printf '[{"subject":"X","relation":"wrongrel","object":"Y","source":"sources/a.md","status":"confirmed","confidence":0.9,"note":""}]\n' > "$KB/runs/drift.json"
"$PYTHON" tools/merge_candidates.py --wiki "$KB" >/dev/null 2>&1
grep -q ",superseded," "$KB/facts/candidates.csv" && ! grep -q "wrongrel,Y,sources/a.md,confirmed" "$KB/facts/candidates.csv" \
  && ok "fact mode: supersede survives anchor drift across re-merge" || bad "supersede lost on anchor drift: $(grep wrongrel "$KB/facts/candidates.csv")"

# --- --purge: delete the row and strip runs -----------------------------------
KB="$(mktemp -d)/wiki"; seed_facts "$KB"
"$PYTHON" -m factlog eject --fact X wrongrel Y --target "$KB" --purge >/dev/null 2>&1
grep -q "wrongrel" "$KB/facts/candidates.csv" && bad "--purge left the fact row" || ok "fact mode --purge: row deleted"
grep -q "wrongrel" "$KB/runs/r.json" && bad "--purge left runs row" || ok "fact mode --purge: runs row stripped"
grep -q "goodrel" "$KB/runs/r.json" && ok "fact mode --purge: unrelated runs row kept" || bad "unrelated runs row lost"

# --- --dry-run changes nothing ------------------------------------------------
KB="$(mktemp -d)/wiki"; seed_facts "$KB"
before="$(cat "$KB/facts/candidates.csv")"
"$PYTHON" -m factlog eject --fact X wrongrel Y --target "$KB" --dry-run >/dev/null 2>&1
[ "$(cat "$KB/facts/candidates.csv")" = "$before" ] && ok "fact mode --dry-run: no change" || bad "--dry-run mutated state"

# --- validation: mode mixing, --delete-original, neither, no-match -------------
KB="$(mktemp -d)/wiki"; seed_facts "$KB"
set +e
"$PYTHON" -m factlog eject a.md --fact X wrongrel Y --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "rejects source + --fact together" || bad "mode mixing not rejected"
"$PYTHON" -m factlog eject --fact X wrongrel Y --delete-original --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "rejects --delete-original in fact mode" || bad "--delete-original not rejected in fact mode"
"$PYTHON" -m factlog eject --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "rejects neither source nor --fact" || bad "empty invocation not rejected"
"$PYTHON" -m factlog eject --fact No Such Triple --target "$KB" >/dev/null 2>&1; [ $? -eq 1 ] && ok "fact mode: unknown triple errors (rc 1)" || bad "no-match triple did not error"
set -e

# =============================================================================
# Orphan mode: auto-detect and eject sources whose original is gone (#orphans)
# =============================================================================

# Seed a KB mixing: a live conversion (original present), an orphaned conversion
# (original deleted), a hand-placed conversion (no provenance), a live text
# source, and a cited-but-missing text source.
seed_orphans() {  # $1 = KB path
  local kb="$1"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  printf 'PK\003\004\000' > "$kb/sources/live.pdf"                       # original present
  printf '<!-- ingested-by-factlog | source: live.pdf | converter: pdftotext | date: 2026-01-01T00:00:00Z -->\nlive\n' \
    > "$kb/runs/sources/live.md"
  printf '<!-- ingested-by-factlog | source: gone.docx | converter: pandoc | date: 2026-01-01T00:00:00Z -->\ngone\n' \
    > "$kb/runs/sources/gone.md"                                          # original gone.docx absent
  printf 'hand authored, no provenance header\n' > "$kb/runs/sources/hand.md"  # hand-placed
  printf 'plain\n' > "$kb/sources/keep.md"                               # live text source
  printf '%s\n%s\n%s\n%s\n%s\n%s\n' "$H" \
    'L,rel,M,runs/sources/live.md,confirmed,0.9,' \
    'G,rel,H,runs/sources/gone.md,confirmed,0.9,' \
    'N,rel,O,runs/sources/hand.md,confirmed,0.9,' \
    'K,rel,P,sources/keep.md,confirmed,0.9,' \
    'D,rel,E,sources/deleted.md,confirmed,0.9,' > "$kb/facts/candidates.csv"  # deleted.md has no file
  printf '[{"subject":"G","relation":"rel","object":"H","source":"runs/sources/gone.md","status":"confirmed","confidence":0.9,"note":""}]\n' \
    > "$kb/runs/2026-01-01-gone.json"
}

# --- default (supersede): only the orphans are retired ------------------------
KB="$(mktemp -d)/wiki"; seed_orphans "$KB"
out="$("$PYTHON" -m factlog eject --orphans --target "$KB" 2>&1)"
printf '%s\n' "$out"; echo "---"
printf '%s' "$out" | grep -qF "orphan scan" && ok "orphan mode announces the scan" || bad "no orphan scan header"
[ ! -f "$KB/runs/sources/gone.md" ] && ok "orphan conversion (original gone) deleted" || bad "orphan conversion kept"
[ -f "$KB/runs/sources/live.md" ] && ok "live conversion (original present) kept" || bad "live conversion wrongly deleted"
[ -f "$KB/runs/sources/hand.md" ] && ok "hand-placed conversion (no provenance) kept" || bad "hand-placed conversion wrongly ejected"
[ -f "$KB/sources/keep.md" ] && ok "live text source kept" || bad "live text source deleted"
grep -q "G,rel,H,runs/sources/gone.md,superseded," "$KB/facts/candidates.csv" && ok "orphan conversion fact superseded" || bad "orphan conversion fact not retired"
grep -q "D,rel,E,sources/deleted.md,superseded," "$KB/facts/candidates.csv" && ok "cited-but-missing source fact superseded" || bad "missing-file fact not retired"
grep -q "L,rel,M,runs/sources/live.md,confirmed," "$KB/facts/candidates.csv" && ok "live conversion fact preserved" || bad "live fact wrongly retired"
grep -q "N,rel,O,runs/sources/hand.md,confirmed," "$KB/facts/candidates.csv" && ok "hand-placed conversion fact preserved" || bad "hand-placed fact wrongly retired"
grep -q "K,rel,P,sources/keep.md,confirmed," "$KB/facts/candidates.csv" && ok "live text fact preserved" || bad "live text fact wrongly retired"
grep -q '"G", "rel", "H"' "$KB/facts/accepted.dl" && bad "orphan fact still in accepted.dl" || ok "orphan facts dropped from accepted.dl"
grep -q '"L", "rel", "M"' "$KB/facts/accepted.dl" && ok "live fact kept in accepted.dl" || bad "live fact lost from accepted.dl"

# --- --orphans --purge removes rows and strips runs/*.json --------------------
KB="$(mktemp -d)/wiki"; seed_orphans "$KB"
"$PYTHON" -m factlog eject --orphans --purge --target "$KB" >/dev/null 2>&1
grep -q "runs/sources/gone.md" "$KB/facts/candidates.csv" && bad "--orphans --purge left the orphan row" || ok "--orphans --purge deletes the orphan row"
[ ! -f "$KB/runs/2026-01-01-gone.json" ] && ok "--orphans --purge strips the emptied runs/*.json" || bad "runs json not stripped"
grep -q "sources/keep.md" "$KB/facts/candidates.csv" && ok "--orphans --purge keeps live rows" || bad "live rows wrongly purged"

# --- --dry-run changes nothing ------------------------------------------------
KB="$(mktemp -d)/wiki"; seed_orphans "$KB"
before="$(cat "$KB/facts/candidates.csv")"
"$PYTHON" -m factlog eject --orphans --dry-run --target "$KB" >/dev/null 2>&1
[ -f "$KB/runs/sources/gone.md" ] && [ "$(cat "$KB/facts/candidates.csv")" = "$before" ] \
  && ok "--orphans --dry-run leaves files and candidates.csv untouched" || bad "--orphans --dry-run mutated state"

# --- a clean KB (no orphans) reports none and exits 0, touching nothing -------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf 'plain\n' > "$KB/sources/ok.md"
printf '%s\n%s\n' "$H" 'A,rel,B,sources/ok.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
set +e; out="$("$PYTHON" -m factlog eject --orphans --target "$KB" 2>&1)"; rc=$?; set -e
[ "$rc" -eq 0 ] && printf '%s' "$out" | grep -qF "no orphaned sources found" && ok "clean KB: orphan scan finds none (rc 0)" || bad "clean KB orphan scan misbehaved (rc=$rc)"
grep -q "sources/ok.md,confirmed," "$KB/facts/candidates.csv" && ok "clean KB: rows untouched" || bad "clean KB rows changed"

# --- validation: --orphans cannot mix with source(s) or --fact ----------------
KB="$(mktemp -d)/wiki"; seed_orphans "$KB"
set +e
"$PYTHON" -m factlog eject --orphans live.pdf --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "rejects --orphans + source" || bad "--orphans + source not rejected"
"$PYTHON" -m factlog eject --orphans --fact G rel H --target "$KB" >/dev/null 2>&1; [ $? -eq 2 ] && ok "rejects --orphans + --fact" || bad "--orphans + --fact not rejected"
set -e

# --- edge cases: subdir original, NFC Korean name, empty/malformed header, ----
# --- fragment citation, malformed-not-under-root, uncited orphan conversion ---
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/sub"
# (a) original in a subdirectory, header records basename only -> kept
printf 'PK\003\004\000' > "$KB/sources/sub/deep.pdf"
printf '<!-- ingested-by-factlog | source: deep.pdf | converter: pdftotext | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/deep.md"
# (b) Korean-named original present -> its conversion kept (NFC compare)
printf 'PK\003\004\000' > "$KB/sources/한글문서.pdf"
printf '<!-- ingested-by-factlog | source: 한글문서.pdf | converter: pdftotext | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/한글문서.md"
# (c) empty source value in header -> no reliable origin -> kept (not an orphan)
printf '<!-- ingested-by-factlog | source:  | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/empty.md"
# (d) uncited orphan conversion (header original absent, no candidates row) -> deleted
printf '<!-- ingested-by-factlog | source: lonely.docx | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/lonely.md"
printf '%s\n%s\n%s\n%s\n%s\n' "$H" \
  'P,rel,Q,runs/sources/deep.md,confirmed,0.9,' \
  'R,rel,S,runs/sources/한글문서.md,confirmed,0.9,' \
  'T,rel,U,runs/sources/empty.md,confirmed,0.9,' \
  'V,rel,W,/etc/passwd,confirmed,0.9,' > "$KB/facts/candidates.csv"  # (e) malformed, not under a source root
out="$("$PYTHON" -m factlog eject --orphans --target "$KB" 2>&1)"
[ -f "$KB/runs/sources/deep.md" ] && ok "subdir original (basename header) keeps its conversion" || bad "subdir original wrongly orphaned"
[ -f "$KB/runs/sources/한글문서.md" ] && ok "NFC Korean original keeps its conversion" || bad "Korean original wrongly orphaned"
[ -f "$KB/runs/sources/empty.md" ] && ok "empty source: header is not parsed as an orphan" || bad "empty-header conversion wrongly ejected"
[ ! -f "$KB/runs/sources/lonely.md" ] && ok "uncited orphan conversion is cleaned up" || bad "uncited orphan conversion kept"
printf '%s' "$out" | grep -qF "/etc/passwd" && bad "malformed citation not under a source root was auto-ejected" || ok "malformed citation (not under sources/) never auto-ejected"

# --- fragment-cited orphan is still retired (anchor-insensitive match) ---------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
printf '<!-- ingested-by-factlog | source: frag.docx | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/frag.md"  # original frag.docx absent -> orphan
printf '%s\n%s\n' "$H" 'A,rel,B,runs/sources/frag.md#sec2,confirmed,0.9,' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject --orphans --target "$KB" >/dev/null 2>&1
grep -q ",superseded," "$KB/facts/candidates.csv" && ok "orphan fact cited with a #fragment is still retired" || bad "fragment-cited orphan not retired"

# --- same-basename originals in different subtrees: only the deleted one's -----
# --- mirrored conversion is orphaned, not the surviving sibling (#103) ---------
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/a" "$KB/sources/b" "$KB/runs/sources/a" "$KB/runs/sources/b"
printf 'PK\003\004\000' > "$KB/sources/b/report.pdf"   # b survives; a/report.pdf is "deleted" (never created)
printf '<!-- ingested-by-factlog | source: report.pdf | converter: pdftotext | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/a/report.md"
printf '<!-- ingested-by-factlog | source: report.pdf | converter: pdftotext | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/b/report.md"
printf '%s\n%s\n%s\n' "$H" \
  'P,rel,Q,runs/sources/a/report.md,confirmed,0.9,' \
  'R,rel,S,runs/sources/b/report.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
"$PYTHON" -m factlog eject --orphans --target "$KB" >/dev/null 2>&1
[ ! -f "$KB/runs/sources/a/report.md" ] && ok "orphaned subdir conversion (deleted original) is ejected" || bad "same-basename collision masked the orphan"
[ -f "$KB/runs/sources/b/report.md" ] && ok "surviving subdir sibling's conversion is kept" || bad "surviving sibling wrongly orphaned"

# --- a #214 path-form header pairs with its mirrored original (no orphan) -----
# ingest records `source: sub/report.html` for sources/sub/report.html, and the
# conversion sits in the matching mirrored subdir. Both same-name originals are
# present, so neither conversion may be scanned as an orphan.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/sub" "$KB/runs/sources/sub"
printf '<html>top</html>\n' > "$KB/sources/report.html"
printf '<html>nested</html>\n' > "$KB/sources/sub/report.html"
printf '<!-- ingested-by-factlog | source: report.html | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/report.html.md"
printf '<!-- ingested-by-factlog | source: sub/report.html | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/sub/report.html.md"
printf '%s\n%s\n%s\n' "$H" \
  'A,rel,B,runs/sources/report.html.md,confirmed,0.9,' \
  'C,rel,D,runs/sources/sub/report.html.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog eject --orphans --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "no orphaned sources found" \
  && [ -f "$KB/runs/sources/report.html.md" ] && [ -f "$KB/runs/sources/sub/report.html.md" ] \
  && ok "path-form provenance header: mirrored conversions are not orphans" \
  || bad "path-form header wrongly orphaned a live conversion"

# --- a path-form header on a FLAT conversion still pairs by basename ----------
# An original named by an explicit path converts flat (no subtree to mirror)
# while its header can still spell a subdir. Reconstructing the original's
# location from the header instead of the conversion's own path would look for
# sources/sub/... under a flat conversion and auto-delete every such file.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/sub"
printf '<html>nested</html>\n' > "$KB/sources/sub/report.html"
printf '<!-- ingested-by-factlog | source: sub/report.html | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/report.html.md"
printf '%s\n%s\n' "$H" 'A,rel,B,runs/sources/report.html.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
out="$("$PYTHON" -m factlog eject --orphans --target "$KB" 2>&1)"
printf '%s' "$out" | grep -qF "no orphaned sources found" && [ -f "$KB/runs/sources/report.html.md" ] \
  && ok "path-form header on a flat conversion pairs by basename (not auto-deleted)" \
  || bad "flat conversion with a path header wrongly ejected"

# --- a header with no filename component is kept, like an empty one -----------
# `source: /` carries no original name; it must not be reconstructed into an
# original named after the conversion's subdir.
KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
mkdir -p "$KB/sources/sub" "$KB/runs/sources/sub"
printf 'PK\003\004\000' > "$KB/sources/sub/real.pdf"
printf '<!-- ingested-by-factlog | source: / | converter: pandoc | date: 2026-01-01T00:00:00Z -->\nx\n' \
  > "$KB/runs/sources/sub/slash.md"
printf '%s\n%s\n' "$H" 'A,rel,B,runs/sources/sub/slash.md,confirmed,0.9,' > "$KB/facts/candidates.csv"
set +e
"$PYTHON" -m factlog eject sub --target "$KB" --dry-run >/dev/null 2>&1; rc=$?
# ...and a root path, which names no file either, must not collide with that
# sentinel and select the conversion.
"$PYTHON" -m factlog eject / --target "$KB" --dry-run >/dev/null 2>&1; rc2=$?
set -e
[ "$rc" -ne 0 ] && ok "a filename-less header ('source: /') names no original" \
  || bad "a filename-less header matched the subdir name"
[ "$rc2" -ne 0 ] && ok "a root path selects nothing (no empty-origin collision)" \
  || bad "'/' matched a conversion through the empty-origin sentinel"

# --- non-KB path errors -------------------------------------------------------
set +e; "$PYTHON" -m factlog eject anything --target "$(mktemp -d)" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -ne 0 ] && ok "eject on a non-KB path errors" || bad "non-KB path should error"

echo ""
echo "========================================"
echo "test_eject_cmd: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
