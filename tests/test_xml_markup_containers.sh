#!/usr/bin/env bash
# .xhtml/.xml/.svg are markup whose bytes are text but whose content is not prose. A
# content sniff called them text, so coverage told the user to extract facts from the raw
# original and the tags went into extraction as prose -- the #222 bug, one set of
# extensions over (#238). .xhtml has a converter (pandoc's html reader); .xml/.svg do
# not, so they are reported unconvertible rather than fed as prose.
set -uo pipefail
cd "$(dirname "$0")/.."
PY="${FACTLOG_PY:-${PYTHON:-python3}}"
export PYTHONPATH="$PWD"
fails=0
ok() { echo "  ok: $1"; }
bad() { echo "FAIL: $1"; fails=$((fails+1)); }

KB="$(mktemp -d)/kb"
export XDG_CONFIG_HOME="$(mktemp -d)"
"$PY" -m factlog init --target "$KB" >/dev/null || { echo "FAIL: init"; exit 1; }
printf '<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml"><body><p>hi</p></body></html>\n' > "$KB/sources/page.xhtml"
printf '<?xml version="1.0"?><root><item>data</item></root>\n' > "$KB/sources/data.xml"
printf '<svg xmlns="http://www.w3.org/2000/svg"><text>cap</text></svg>\n' > "$KB/sources/pic.svg"

# (a) none of the three is offered as an extractable text source
COV="$(FACTLOG_ROOT="$KB" "$PY" tools/coverage.py --wiki "$KB" 2>&1 || true)"
printf '%s' "$COV" | grep -q '0 text gap(s)' \
  && ok "(a) coverage reports 0 text gaps — no markup is fed as prose" \
  || bad "(a) coverage still calls a markup file a text source: $COV"
printf '%s' "$COV" | grep -q '3 binary needing conversion' \
  && ok "(a) all three are flagged as needing conversion" \
  || bad "(a) not all three flagged for conversion"

# (b) is_text_source is False for each markup ext, True for real prose
"$PY" -c "
import sys, tempfile; sys.path.insert(0, '$PWD')
from pathlib import Path
from factlog.common import is_text_source
d = Path(tempfile.mkdtemp())
bad = []
for ext in ('.xhtml', '.xml', '.svg'):
    f = d / ('x' + ext); f.write_text('<x>markup</x>')
    if is_text_source(f): bad.append(ext)
t = d / 'n.md'; t.write_text('prose')
if not is_text_source(t): bad.append('.md-false-negative')
raise SystemExit(1 if bad else 0)
" && ok "(b) is_text_source is False for markup, True for prose" \
  || bad "(b) is_text_source misclassified a markup or prose file"

# (c) --scan converts .xhtml (has a converter) and hints .xml/.svg (none)
if command -v pandoc >/dev/null 2>&1; then
  OUT="$(FACTLOG_ROOT="$KB" "$PY" -m factlog ingest --scan --target "$KB" 2>&1 || true)"
  find "$KB/runs/sources" -name 'page.xhtml.*' 2>/dev/null | grep -q . \
    && ok "(c) --scan converts .xhtml via pandoc" \
    || bad "(c) --scan did not convert .xhtml"
  printf '%s' "$OUT" | grep -q "skip data.xml" \
    && ok "(c) --scan reports .xml as unconvertible, with a hint" \
    || bad "(c) --scan did not hint on .xml"
  printf '%s' "$OUT" | grep -q "skip pic.svg" \
    && ok "(c) --scan reports .svg as unconvertible, with a hint" \
    || bad "(c) --scan did not hint on .svg"
  # the xml/svg originals are NOT converted (would be markup-as-prose)
  find "$KB/runs/sources" -name 'data.xml.*' -o -name 'pic.svg.*' 2>/dev/null | grep -q . \
    && bad "(c) an unconvertible markup file was converted anyway" \
    || ok "(c) .xml/.svg are left unconverted, not fed as prose"
else
  echo "SKIP: (c) needs pandoc"
fi

echo
if [ "$fails" -eq 0 ]; then echo "xml markup containers: all passed"; else echo "xml markup containers: $fails failed"; exit 1; fi
