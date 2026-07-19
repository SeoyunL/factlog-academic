#!/usr/bin/env bash
# tests/test_output_lang.sh — narration language selection (#269)
#
# Pins (XDG_CONFIG_HOME isolated so a developer's real config never interferes):
#   - `factlog lang` with no code prints the setting on one line (empty when unset)
#   - `factlog lang <code>` sets it (root untouched); porcelain query reflects it
#   - `factlog use <kb> --lang <code>` sets root AND lang together
#   - `factlog where` shows the language; `where --porcelain` stays root-only
#   - a root-only config (pre-#269) still resolves with no regression, lang empty
#
# Usage: bash tests/test_output_lang.sh

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
unset FACTLOG_ROOT

# --- `factlog lang` with no config -> empty line ------------------------------
out="$("$PYTHON" -m factlog lang)"
[ -z "$out" ] && ok "lang (unset) prints an empty line" || bad "lang (unset) leaked: '$out'"

# --- `factlog lang ko` sets it; porcelain query reflects it -------------------
"$PYTHON" -m factlog lang ko >/dev/null
out="$("$PYTHON" -m factlog lang)"
[ "$out" = "ko" ] && ok "lang ko sets the narration language" || bad "lang query = '$out', want 'ko'"

# The porcelain lang query must be a single bare line (no label).
[ "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" = "1" ] && ok "lang query emits a single line" || bad "lang query emitted multiple lines: $out"
printf '%s' "$out" | grep -qiE 'language|config|narration' && bad "lang query leaked a human label: $out" || ok "lang query emits no human label"

# --- `factlog lang` did NOT set a root (config had no root before) ------------
set +e; "$PYTHON" -m factlog where --porcelain >/dev/null 2>&1; set -e
porc_src="$("$PYTHON" -m factlog where 2>/dev/null | grep -F 'resolved from:' || true)"
printf '%s' "$porc_src" | grep -qF 'current directory' && ok "lang set leaves root unset (falls back to cwd)" || bad "lang unexpectedly affected root: $porc_src"

# --- `factlog use <kb> --lang en` sets root AND lang together -----------------
"$PYTHON" -m factlog init --target "$KB" >/dev/null
"$PYTHON" -m factlog use "$KB" --lang en >/dev/null
out="$("$PYTHON" -m factlog lang)"
[ "$out" = "en" ] && ok "use --lang sets the narration language" || bad "use --lang: lang = '$out', want 'en'"
"$PYTHON" -m factlog where --porcelain | grep -qF "$(cd "$KB" && pwd -P)" && ok "use --lang also sets the active KB root" || bad "use --lang did not set root"

# --- `factlog use` WITHOUT --lang preserves the existing language -------------
"$PYTHON" -m factlog use "$KB" >/dev/null
out="$("$PYTHON" -m factlog lang)"
[ "$out" = "en" ] && ok "use without --lang preserves the existing language" || bad "use dropped lang: '$out'"

# --- `factlog where` shows the language; --porcelain stays root-only ----------
"$PYTHON" -m factlog where | grep -qiF "narration language: en" && ok "where shows the narration language" || bad "where did not show the language"
porc="$("$PYTHON" -m factlog where --porcelain)"
KB_ROOT="$(cd "$KB" && pwd -P)"
[ "$porc" = "$KB_ROOT" ] && ok "where --porcelain stays root-only (no lang)" || bad "porcelain = '$porc', want root only"
# Detect a narration-language LEAK, robust to a temp-KB root path that carries an
# incidental "en" from mktemp's random suffix (e.g. /tmp/tmp.oqGEnoyAO8/wiki -> "GEn"),
# which used to false-alarm 'lang leaked' and made the run a rerun-passes flake (#358).
# The legitimate output is the root path itself, so strip that exact prefix and inspect
# only the RESIDUAL — a real lang leak lands past the root, an "en" inside the root does not.
lang_leaks() { printf '%s' "${1#"$2"}" | grep -qiE 'narration|language|en'; }
lang_leaks "$porc" "$KB_ROOT" && bad "porcelain leaked lang: $porc" || ok "where --porcelain emits no lang"
# #358 self-check, BOTH directions, so the flake fix cannot silently blind the detector:
FAKE="/tmp/tmp.oqGEnoyAO8/wiki"  # mktemp-style suffix carrying "GEn" — the flake trigger
# (a) an incidental "en" in the root path alone must NOT fire (porc == root, residual empty)
lang_leaks "$FAKE" "$FAKE" && bad "#358: incidental 'en' in the root path false-alarmed" || ok "#358: 'en' inside the root path does not trip the leak detector"
# (b) a real leak PAST the root prefix must still fire (detection preserved)
lang_leaks "$FAKE"$'\nnarration language: en' "$FAKE" && ok "#358: a narration-language leak past the root is still caught" || bad "#358: leak detector went blind to a real lang leak"

# --- over-length --lang is rejected symmetrically across entry points ---------
# Guards the #269 review WARNING: `lang`, `use --lang`, and `setup --lang` must
# share one validation contract (same rc, same message body). Removing the shared
# `_normalize_lang` from any entry point makes this block FAIL.
LONG="$("$PYTHON" -c 'print("x"*100)')"
set +e
msg_lang="$("$PYTHON" -m factlog lang "$LONG" 2>&1 >/dev/null)"; rc_lang=$?
msg_use="$("$PYTHON" -m factlog use "$KB" --lang "$LONG" 2>&1 >/dev/null)"; rc_use=$?
set -e
{ [ "$rc_lang" = "2" ] && [ "$rc_use" = "2" ]; } && ok "over-length --lang rejected by both lang and use (rc 2)" || bad "over-length rc mismatch: lang=$rc_lang use=$rc_use"
{ printf '%s' "$msg_lang" | grep -qF "language code too long (max 32 chars)" \
  && printf '%s' "$msg_use" | grep -qF "language code too long (max 32 chars)"; } \
  && ok "over-length rejection shares one message across entry points" || bad "over-length messages diverged: lang='$msg_lang' use='$msg_use'"
# the rejected `use --lang` must NOT have persisted the bad value
out="$("$PYTHON" -m factlog lang)"
[ "$out" = "en" ] && ok "rejected use --lang left the previous language intact" || bad "rejected use --lang mutated lang: '$out'"

# --- interior control characters are rejected (porcelain one-line contract) ---
# #274: `.strip()` keeps an interior newline/tab/CR, which would make `factlog lang`
# (no arg) emit >1 line — breaking the porcelain contract SKILL.md parses and
# opening a self-config narration prose-injection vector. `_normalize_lang` must
# reject them with rc 2, symmetrically across entry points, WITHOUT mutating state.
NL_LANG="$("$PYTHON" -c 'print("ko\nHACKED")')"
set +e
msg_nl="$("$PYTHON" -m factlog lang "$NL_LANG" 2>&1 >/dev/null)"; rc_nl=$?
msg_nl_use="$("$PYTHON" -m factlog use "$KB" --lang "$NL_LANG" 2>&1 >/dev/null)"; rc_nl_use=$?
tab_rc=$("$PYTHON" -m factlog lang "$("$PYTHON" -c 'print("ko\tx")')" >/dev/null 2>&1; echo $?)
cr_rc=$("$PYTHON" -m factlog lang "$("$PYTHON" -c 'print("ko\rx")')" >/dev/null 2>&1; echo $?)
set -e
{ [ "$rc_nl" = "2" ] && [ "$rc_nl_use" = "2" ]; } && ok "interior newline rejected by both lang and use (rc 2)" || bad "newline rc mismatch: lang=$rc_nl use=$rc_nl_use"
{ [ "$tab_rc" = "2" ] && [ "$cr_rc" = "2" ]; } && ok "interior tab and CR rejected (rc 2)" || bad "tab/CR rc: tab=$tab_rc cr=$cr_rc"
printf '%s' "$msg_nl" | grep -qF "control characters" && ok "control-char rejection message names control characters" || bad "control-char message unexpected: '$msg_nl'"
# the rejected values must NOT have persisted; `factlog lang` stays exactly one line
out="$("$PYTHON" -m factlog lang)"
[ "$out" = "en" ] && ok "rejected control-char lang left the previous language intact" || bad "control-char lang mutated state: '$out'"
[ "$(printf '%s\n' "$out" | wc -l | tr -d ' ')" = "1" ] && ok "lang query stays a single line after adversarial input" || bad "lang query emitted multiple lines: $out"

# --- empty --lang clears the setting, identically for both entry points -------
msg_clear="$("$PYTHON" -m factlog lang "")"
printf '%s' "$msg_clear" | grep -qF "narration language cleared" && ok "lang \"\" prints 'narration language cleared'" || bad "lang \"\" wording: '$msg_clear'"
out="$("$PYTHON" -m factlog lang)"
[ -z "$out" ] && ok "lang \"\" clears the setting" || bad "lang \"\" did not clear: '$out'"
# restore, then clear via use --lang "" (same wording, same effect)
"$PYTHON" -m factlog use "$KB" --lang en >/dev/null
msg_clear2="$("$PYTHON" -m factlog use "$KB" --lang "")"
printf '%s' "$msg_clear2" | grep -qF "narration language cleared" && ok "use --lang \"\" prints 'narration language cleared'" || bad "use --lang \"\" wording: '$msg_clear2'"
out="$("$PYTHON" -m factlog lang)"
[ -z "$out" ] && ok "use --lang \"\" clears the setting" || bad "use --lang \"\" did not clear: '$out'"

# --- root-only config (pre-#269) is backward compatible -----------------------
mkdir -p "$XDG_CONFIG_HOME/factlog"
printf '{"root": "%s"}\n' "$(cd "$KB" && pwd -P)" > "$XDG_CONFIG_HOME/factlog/config.json"
out="$("$PYTHON" -m factlog lang)"
[ -z "$out" ] && ok "root-only config reads lang as empty (backward compat)" || bad "root-only config leaked lang: '$out'"
"$PYTHON" -m factlog where --porcelain | grep -qF "$(cd "$KB" && pwd -P)" && ok "root-only config still resolves the root (no regression)" || bad "root-only config regressed root resolution"
# `where` must NOT print a narration-language line when none is set.
"$PYTHON" -m factlog where | grep -qiF "narration language" && bad "where showed a language line when none set" || ok "where omits the language line when unset"

echo ""
echo "========================================"
echo "test_output_lang: $pass passed, $fail failed"
echo "========================================"
[ "$fail" -eq 0 ]
