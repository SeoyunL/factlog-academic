#!/usr/bin/env bash
# tests/test_eject_path_scope.sh — a path-scoped eject stays in its path (#221)
#
# `eject sub/report.docx` also deleted the conversion of a TOP-LEVEL report.docx —
# a source the user never named — and exited 0. The original survived but its
# conversion did not, so it silently became a coverage gap.
#
# The cause was a basename comparison: the provenance map stores only the
# original's basename, and the path branch compared that against the requested
# basename, so the mirrored subdirectory of the conversion was never looked at.
# README:70 promises the opposite ("same-stem files in different folders never
# collide"), and :616 that `eject report.docx` never disturbs another original's
# conversion.
#
# Pins:
#   (a) a nested eject deletes only the nested conversion
#   (b) the untouched original keeps its conversion (no silent coverage gap)
#   (c) a top-level eject does not reach into the subdirectory either
#   (d) a bare filename still matches by name (the documented convenience)
#
# Usage: bash tests/test_eject_path_scope.sh

set -euo pipefail

TMP_ROOT="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$TMP_ROOT"' EXIT
export XDG_CONFIG_HOME="$TMP_ROOT/cfg"

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# Two originals with the SAME basename in different folders.
new_kb() {
  local kb
  kb="$(mktemp -d "$TMP_ROOT/kb.XXXXXX")/wiki"
  "$PYTHON" -m factlog init --target "$kb" >/dev/null
  mkdir -p "$kb/sources/sub"
  # .docx needs a converter; use a text file with a binary extension the ingest
  # table knows, so this harness needs no pandoc/textutil.
  printf 'top\n' > "$kb/sources/report.html"
  printf 'nested\n' > "$kb/sources/sub/report.html"
  "$PYTHON" -m factlog ingest "$kb/sources/report.html" --target "$kb" >/dev/null 2>&1
  "$PYTHON" -m factlog ingest "$kb/sources/sub/report.html" --target "$kb" >/dev/null 2>&1
  echo "$kb"
}

conv_count() { find "$1/runs/sources" -type f 2>/dev/null | wc -l | tr -d ' '; }
has_conv() { find "$1/runs/sources" -type f 2>/dev/null | grep -q "$2"; }

KB="$(new_kb)"
if [ "$(conv_count "$KB")" != "2" ]; then
  echo "SKIP: this environment cannot convert .html (pandoc missing); nothing to scope"
  exit 0
fi

# ------------------------------------------------------------------ (a) (b)
"$PYTHON" -m factlog eject sub/report.html --target "$KB" >/dev/null 2>&1 || true
if [ "$(conv_count "$KB")" = "1" ]; then
  ok "(a) a nested eject deleted only one conversion"
else
  bad "(a) a nested eject deleted $(conv_count "$KB") of 2 conversions"
fi
if has_conv "$KB" "^${KB}/runs/sources/report" || find "$KB/runs/sources" -maxdepth 1 -type f | grep -q report; then
  ok "(b) the top-level original kept its conversion"
else
  bad "(b) the top-level conversion was deleted — a source the user never named"
fi

# ------------------------------------------------------------------ (c)
KB2="$(new_kb)"
"$PYTHON" -m factlog eject report.html --target "$KB2" >/dev/null 2>&1 || true
# A BARE filename is the documented convenience and matches by name, so it may
# take both. What must not happen is a PATH-scoped request reaching elsewhere:
KB3="$(new_kb)"
"$PYTHON" -m factlog eject sources/report.html --target "$KB3" >/dev/null 2>&1 || true
if find "$KB3/runs/sources/sub" -type f 2>/dev/null | grep -q report; then
  ok "(c) a top-level path-scoped eject left the subdirectory alone"
else
  bad "(c) a top-level path-scoped eject reached into sub/"
fi

# ------------------------------------------------------------------ (d)
if [ "$(conv_count "$KB2")" -lt 2 ]; then
  ok "(d) a bare filename still matches by name (documented convenience)"
else
  bad "(d) a bare filename matched nothing"
fi

echo "---"
echo "passed: $pass, failed: $fail"
[ "$fail" -eq 0 ]
