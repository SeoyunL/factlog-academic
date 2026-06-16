#!/usr/bin/env bash
# tests/test_page_template.sh — externalized concept-page template (#48)
#
# Pins:
#   - `factlog init` scaffolds templates/ and templates/pages.md
#   - the cli.py seed and merge_candidates.DEFAULT_PAGE_TEMPLATE are byte-identical
#   - with NO custom template, generated pages keep the default layout (backward
#     compatible) and the default render is byte-identical to the legacy layout
#   - a custom <kb>/templates/pages.md drives the generated page layout
#   - the generated marker is guaranteed even if a custom template omits it
#     (auto-prepended), so regeneration detection cannot be broken by an edit
#
# Deterministic; no pyrewire.  Usage: bash tests/test_page_template.sh

set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"
MERGE="$PLUGIN_ROOT/tools/merge_candidates.py"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

KB="$(mktemp -d)/wiki"
"$PYTHON" -m factlog init --target "$KB" >/dev/null

# --- scaffold ----------------------------------------------------------------
[ -f "$KB/templates/pages.md" ] && ok "init scaffolds templates/pages.md" || bad "templates/pages.md not scaffolded"

# --- seed == built-in default (divergence guard) + legacy byte-compat ---------
verdict="$(FACTLOG_TEST_KB="$KB" FACTLOG_TEST_ROOT="$PLUGIN_ROOT" "$PYTHON" <<'PY'
import os, sys
kb = os.environ["FACTLOG_TEST_KB"]; root = os.environ["FACTLOG_TEST_ROOT"]
os.environ["FACTLOG_ROOT"] = "/tmp"; sys.argv = ["x"]
sys.path.insert(0, os.path.join(root, "tools")); sys.path.insert(0, root)
import merge_candidates as mc
from factlog.cli import _TEMPLATES
if _TEMPLATES["templates/pages.md"] != mc.DEFAULT_PAGE_TEMPLATE:
    print("FAIL: seed != DEFAULT_PAGE_TEMPLATE"); raise SystemExit
with open(os.path.join(kb, "templates", "pages.md"), encoding="utf-8") as f:
    if f.read() != mc.DEFAULT_PAGE_TEMPLATE:
        print("FAIL: scaffolded file != DEFAULT_PAGE_TEMPLATE"); raise SystemExit
entity="갑봇"; src=["- sources/a.md"]; rel=["- 통합 -> [[을서비스]] (sources/a.md, confidence=0.90)"]; rev=[]
legacy = "\n".join([mc.GENERATED_PAGE_MARKER, f"# {entity}", "",
    "## 요약", "- `sources/`에서 추출된 candidate fact를 기준으로 정리한 개념입니다.", "",
    "## 출처", *src, "",
    "## 관련 페이지", *rel, "",
    "## 확인 필요", *(rev or ["- 현재 추출 결과에서 별도 검토 항목이 없습니다."]), ""])
got = mc.render_page(mc.DEFAULT_PAGE_TEMPLATE, entity, "\n".join(src), "\n".join(rel),
    "\n".join(rev or ["- 현재 추출 결과에서 별도 검토 항목이 없습니다."]))
print("OK" if got == legacy else "FAIL: default render diverged from legacy layout")
PY
)"
[ "$verdict" = "OK" ] && ok "seed == default; scaffold + render byte-compatible with legacy" || bad "$verdict"

# --- default layout end-to-end (backward compatible) --------------------------
printf 'a content\n' > "$KB/sources/a.md"
printf '[{"subject":"갑봇","relation":"통합","object":"을서비스","source":"sources/a.md","status":"accepted","confidence":"0.9","note":""}]\n' > "$KB/runs/extract.json"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
gen="$(grep -rl "generated-by-factlog" "$KB/pages/" | head -1)"
[ -n "$gen" ] && grep -qF "## 요약" "$gen" && grep -qF "## 관련 페이지" "$gen" && ok "default layout used when no custom template" || bad "default layout missing"

# --- custom template drives layout -------------------------------------------
cat > "$KB/templates/pages.md" <<'TMPL'
<!-- generated-by-factlog -->
ENTITY=={{ENTITY}}
SRCBLOCK::
{{SOURCES}}
RELBLOCK::
{{RELATIONS}}
TMPL
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
gen="$(grep -rl "ENTITY==" "$KB/pages/" | head -1)"
[ -n "$gen" ] && grep -qF "SRCBLOCK::" "$gen" && grep -qF "RELBLOCK::" "$gen" && ok "custom template drives generated layout" || bad "custom template not applied"
# default sections gone (template fully replaced)
[ -n "$gen" ] && ! grep -qF "## 요약" "$gen" && ok "custom template replaces default sections" || bad "default sections leaked into custom output"

# --- marker auto-prepended (on line 1) when custom template omits it ----------
printf '# {{ENTITY}} (no marker)\n{{SOURCES}}\n' > "$KB/templates/pages.md"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
gen="$(grep -rl "(no marker)" "$KB/pages/" | head -1)"
[ -n "$gen" ] && [ "$(head -1 "$gen")" = "<!-- generated-by-factlog -->" ] && ok "marker auto-prepended on line 1 when template omits it" || bad "marker not on line 1"
# re-run: the SAME file is regenerated (detected as generated, not hand-authored)
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
[ -f "$gen" ] && grep -qF "<!-- generated-by-factlog -->" "$gen" && grep -qF "(no marker)" "$gen" && ok "marker-prepended page regenerated on re-run (not orphaned)" || bad "page not regenerated on re-run"

# --- placeholder injection is NOT re-substituted (single-pass) ----------------
# A source string that literally contains {{REVIEW}} must survive verbatim.
printf 'x\n' > "$KB/sources/{{REVIEW}}.md"
printf '[{"subject":"인젝션","relation":"인용","object":"대상","source":"sources/{{REVIEW}}.md","status":"accepted","confidence":"0.9","note":""}]\n' > "$KB/runs/inject.json"
# reset to the default template for this case
"$PYTHON" -c "import sys,os; sys.path.insert(0,'$PLUGIN_ROOT/tools'); os.environ['FACTLOG_ROOT']='/tmp'; sys.argv=['x']; import merge_candidates as mc; open('$KB/templates/pages.md','w').write(mc.DEFAULT_PAGE_TEMPLATE)"
"$PYTHON" "$MERGE" --wiki "$KB" >/dev/null 2>&1
gen="$(grep -rl "# 인젝션" "$KB/pages/" | head -1)"
[ -n "$gen" ] && grep -qF "sources/{{REVIEW}}.md" "$gen" && ok "value containing {{REVIEW}} not re-substituted (single-pass)" || bad "placeholder injection corrupted output"
rm -f "$KB/runs/inject.json" "$KB/sources/{{REVIEW}}.md"

# --- empty custom template falls back to default (with warning) ---------------
: > "$KB/templates/pages.md"
err="$("$PYTHON" "$MERGE" --wiki "$KB" 2>&1 >/dev/null)"
gen="$(grep -rl "generated-by-factlog" "$KB/pages/" | head -1)"
[ -n "$gen" ] && grep -qF "## 요약" "$gen" && ok "empty custom template falls back to default layout" || bad "empty template did not fall back"
printf '%s' "$err" | grep -qF "is empty; using built-in default" && ok "empty template warns to stderr" || bad "no warning for empty template"

echo ""
echo "========================================"
echo "test_page_template: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
