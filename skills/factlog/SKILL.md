---
name: factlog
description: >-
  Keep a markdown knowledge base honest: extract source-backed candidate facts
  from sources/, compile confirmed facts, run a deterministic Datalog/wirelog
  logic check, and attempt gated self-correction. Use when the user asks to
  "sync facts", "check the wiki", "run factlog", "verify facts", or update a
  knowledge base from its source documents.
allowed-tools: Bash(python3 *) Read Edit Write Grep Glob
# disable-model-invocation: true   # (under review) restrict to explicit /factlog calls for demo safety
---

# factlog — Agent Bridge

> SCAFFOLD. Full step instructions are authored in a later milestone (plan T3).
> This stub fixes the contract so the skill loads and is callable.

**One rule:** you do not draw conclusions. You produce files and call the bundled
CLI. The CLI returns the verifiable report. Anything you produce is a *candidate*
until the engine and a human confirm it.

## Deterministic gate (do not skip)

1. Treat every fact/query you generate as `candidate`/draft — never promote it to
   engine input yourself.
2. Always run `python3 ${CLAUDE_SKILL_DIR}/tools/run_logic_check.py` and show the
   resulting `facts/logic_report.txt` **verbatim** before stating any conclusion.
3. If the report shows `errors > 0`, return to the human instead of concluding.
   Surface `Policy Findings`, `warnings`, and `review_required` under a separate
   "needs review" section.
4. Only edit `facts/query.dl` during self-correction when the repaired query passes
   schema and engine re-validation; otherwise keep the original and log the attempt
   to `decisions/correction_trace.md`.

## Commands (to be implemented)

- `/factlog sync` — read `sources/`, extract candidate facts, update `pages/` and `decisions/`.
- `/factlog check` — compile accepted facts, run the logic check, show the report.
- `/factlog repair` — gated self-correction of `review_required` queries.

## Extraction & translation criteria

See bundled references (authored in T4):

- `references/text-to-fact.md`
- `references/text-to-datalog.md`
- `references/natural-language-to-policy.md`
- `references/self-correct.md`
