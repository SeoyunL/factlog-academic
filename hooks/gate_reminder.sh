#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# factlog deterministic gate — SCAFFOLD (non-blocking).
#
# Fires after Write/Edit. If an engine input was just edited by the model, it
# reminds the session to run the deterministic logic check and show the report
# verbatim before concluding — the core factlog rule ("the agent does not draw
# conclusions; the CLI returns the verifiable report").
#
# This is a nudge only. The enforcing version (block conclusions until
# run_logic_check.py has run and logic_report.txt is shown) is authored in the
# delivery plan (T3) using a PreToolUse deny on the relevant action. A Stop hook
# cannot block, so enforcement must sit on a tool action, not on completion.

payload="$(</dev/stdin)"

if printf '%s' "$payload" | grep -Eq 'facts/(query\.dl|candidates\.csv|accepted\.dl)|policy/logic-policy\.dl'; then
  echo "[factlog] An engine input was edited. Run the logic check before concluding:" >&2
  echo "          \"\${CLAUDE_PLUGIN_ROOT}\"/tools/factlog_python.sh \"\${CLAUDE_PLUGIN_ROOT}\"/tools/run_logic_check.py" >&2
  echo "          then show facts/logic_report.txt verbatim. Candidates are not engine input until confirmed." >&2
fi

exit 0
