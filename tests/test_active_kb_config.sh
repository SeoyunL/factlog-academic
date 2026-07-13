#!/usr/bin/env bash
# tests/test_active_kb_config.sh — persistent active-KB config (#62)
#
# Pins (XDG_CONFIG_HOME isolated so a developer's real config never interferes):
#   - resolve_root precedence: --flag > $FACTLOG_ROOT > config > cwd
#   - `factlog init`/`use` record the active KB; `factlog where` reports it
#   - `factlog ingest` with no --target uses the active KB (from any cwd)
#   - a tool (coverage) with no --wiki uses the active KB
#   - `factlog use <missing>` errors; no config -> cwd fallback (backward compat)
#
# Usage: bash tests/test_active_kb_config.sh

set -euo pipefail

PLUGIN_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PYTHONPATH="$PLUGIN_ROOT${PYTHONPATH:+:$PYTHONPATH}"
PYTHON="${PYTHON:-python3}"

pass=0
fail=0
ok() { echo "PASS: $*"; pass=$((pass + 1)); }
bad() { echo "FAIL: $*" >&2; fail=$((fail + 1)); }

# Isolate config so this never reads/writes the developer's ~/.config/factlog.
export XDG_CONFIG_HOME="$(mktemp -d)/cfg"
KB="$(mktemp -d)/wiki"
KB2="$(mktemp -d)/wiki2"

# --- resolve_root precedence (pure) ------------------------------------------
verdict="$(FACTLOG_TEST_ROOT="$PLUGIN_ROOT" KB="$KB" KB2="$KB2" "$PYTHON" - <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.environ["FACTLOG_TEST_ROOT"], "tools"))
import factlog_config as fc
kb, kb2 = os.environ["KB"], os.environ["KB2"]
os.makedirs(kb, exist_ok=True)
problems = []
# no config, no env -> cwd
os.environ.pop("FACTLOG_ROOT", None)
if fc.config_path().exists():
    os.remove(fc.config_path())
r, s = fc.resolve_root()
if s != "cwd":
    problems.append(f"empty -> {s}, want cwd")
# config set
fc.write_root(kb)
r, s = fc.resolve_root()
if s != "config" or r != os.path.realpath(kb):
    problems.append(f"config -> ({r},{s})")
# env overrides config
os.environ["FACTLOG_ROOT"] = kb2
r, s = fc.resolve_root()
if s != "env" or r != os.path.realpath(kb2):
    problems.append(f"env should override config -> ({r},{s})")
# flag overrides env
r, s = fc.resolve_root(kb)
if s != "flag" or r != os.path.realpath(kb):
    problems.append(f"flag should override env -> ({r},{s})")
# malformed configs fall back to cwd (no crash): non-string root, empty, bad JSON, non-dict
os.environ.pop("FACTLOG_ROOT", None)
for bad_content in ['{"root": 123}', '{"root": ""}', '{"root": null}', 'not json', '[1,2,3]']:
    fc.config_path().write_text(bad_content, encoding="utf-8")
    try:
        r, s = fc.resolve_root()
    except Exception as e:
        problems.append(f"malformed {bad_content!r} crashed: {e}")
        continue
    if s != "cwd":
        problems.append(f"malformed {bad_content!r} -> {s}, want cwd")
print("OK" if not problems else "FAIL: " + " | ".join(problems))
PY
)"
[ "$verdict" = "OK" ] && ok "resolve_root precedence: flag > env > config > cwd" || bad "$verdict"

# --- init records active KB; where reports it --------------------------------
rm -f "$XDG_CONFIG_HOME/factlog/config.json"
"$PYTHON" -m factlog init --target "$KB" >/dev/null
unset FACTLOG_ROOT
"$PYTHON" -m factlog where | grep -qF "active KB: $(cd "$KB" && pwd -P)" && ok "init records active KB; where reports it" || bad "where did not report init'd KB"

# --- ingest with no --target uses active KB (from a different cwd) ------------
printf '\x00\x01bin' > "$KB/sources/c.bin.docx"   # binary-ish; conversion may fail, target is the point
out="$(cd /tmp && "$PYTHON" -m factlog ingest --scan 2>&1 || true)"
printf '%s' "$out" | grep -qF "target KB $(cd "$KB" && pwd -P) (from config)" && ok "ingest (no --target) targets active KB from another cwd" || bad "ingest did not use active KB: $out"

# --- a tool (coverage) with no --wiki uses active KB -------------------------
out="$(cd /tmp && "$PYTHON" "$PLUGIN_ROOT/tools/source_coverage.py" 2>&1 || true)"
printf '%s' "$out" | grep -qF "sources/c.bin.docx" && ok "coverage (no --wiki) uses active KB" || bad "coverage did not use active KB: $out"

# --- factlog use switches active KB ------------------------------------------
"$PYTHON" -m factlog init --target "$KB2" >/dev/null   # also records KB2
"$PYTHON" -m factlog use "$KB" >/dev/null
"$PYTHON" -m factlog where | grep -qF "active KB: $(cd "$KB" && pwd -P)" && ok "factlog use switches active KB" || bad "use did not switch active KB"

# --- factlog use <missing> errors --------------------------------------------
set +e; "$PYTHON" -m factlog use "/no/such/kb/path" >/dev/null 2>&1; rc=$?; set -e
[ "$rc" -ne 0 ] && ok "use on a missing path errors" || bad "use on missing path should error"

# --- where --porcelain prints ONLY the root (no label), honouring precedence --
# Active KB is currently $KB (set via `use` above). --porcelain must emit exactly
# one line = the resolved root, with no "active KB:" / "resolved from:" prose.
unset FACTLOG_ROOT
porc="$("$PYTHON" -m factlog where --porcelain)"
[ "$porc" = "$(cd "$KB" && pwd -P)" ] && ok "where --porcelain prints only the config-active KB root" || bad "porcelain (config) = '$porc', want '$(cd "$KB" && pwd -P)'"
printf '%s' "$porc" | grep -qE 'active KB|resolved from|config file' && bad "porcelain output leaked a human label: $porc" || ok "where --porcelain emits no human labels"
[ "$(printf '%s\n' "$porc" | wc -l | tr -d ' ')" = "1" ] && ok "where --porcelain emits a single line" || bad "porcelain emitted multiple lines: $porc"

# $FACTLOG_ROOT wins over config in --porcelain too (precedence preserved).
porc_env="$(FACTLOG_ROOT="$KB2" "$PYTHON" -m factlog where --porcelain)"
[ "$porc_env" = "$(cd "$KB2" && pwd -P)" ] && ok "where --porcelain honours \$FACTLOG_ROOT over config" || bad "porcelain (env) = '$porc_env', want '$(cd "$KB2" && pwd -P)'"

# --- plain `factlog where` (no flag) output is UNCHANGED ----------------------
plain="$("$PYTHON" -m factlog where)"
printf '%s' "$plain" | grep -qF "active KB: $(cd "$KB" && pwd -P)" \
  && printf '%s' "$plain" | grep -qF "resolved from:" \
  && printf '%s' "$plain" | grep -qF "config file:" \
  && ok "plain factlog where output unchanged (active KB / resolved from / config file)" \
  || bad "plain where output regressed: $plain"

echo ""
echo "========================================"
echo "test_active_kb_config: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
