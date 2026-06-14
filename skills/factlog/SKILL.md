---
name: factlog
description: >-
  Keep a markdown knowledge base honest: extract source-backed candidate facts
  from sources/, compile confirmed facts, run a deterministic Datalog/wirelog
  logic check, and attempt gated self-correction. Use when the user asks to
  "sync facts", "check the wiki", "run factlog", "verify facts", or update a
  knowledge base from its source documents.
allowed-tools: Bash(python3 *) Read Edit Write Grep Glob
---

# factlog — Agent Bridge

**One rule:** you do not draw conclusions. You produce files and call the
bundled CLI. The CLI returns the verifiable report. Anything you produce is a
*candidate* until the engine and a human confirm it.

Bundled scripts live under `${CLAUDE_PLUGIN_ROOT}/tools/`; criteria documents
under `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/`. The deterministic
gate is also backed by a plugin hook (`hooks/hooks.json`).

## Deterministic gate (do not skip)

1. Treat every fact/query you generate as `candidate`/draft — never promote it
   to engine input yourself.
2. Always run `python3 ${CLAUDE_PLUGIN_ROOT}/tools/run_logic_check.py` and show
   the resulting `facts/logic_report.txt` **verbatim** before stating any
   conclusion.
3. If the report shows `errors > 0`, return to the human instead of concluding.
   Surface `Policy Findings`, `warnings`, and `review_required` under a
   separate "needs review" section.
4. Only edit `facts/query.dl` during self-correction when the repaired query
   passes schema and engine re-validation; otherwise keep the original and log
   the attempt to `decisions/correction_trace.md`.

## Canonical source value for fact extraction

When writing extracted fact rows to `$FACTLOG_ROOT/runs/*.json`, the `source`
field MUST be a path relative to the KB root, prefixed with `sources/`.

Examples:
- `"sources/my-doc.md"`
- `"sources/subdir/notes.md#section-heading"`

Bare filenames (e.g. `"my-doc.md"`) are NOT valid and will be silently dropped
by `merge_candidates.py`. Always include the `sources/` prefix.

---

## `/factlog sync` — extract candidates and merge into KB

**Purpose:** Read every file under `sources/`, extract candidate facts in
native Claude in-session (no subprocess), write them as `runs/*.json`, then
delegate merging and page generation to the deterministic engine.

**Execution order:**

### Step 1 — Native fact extraction (LLM, in-session)

For each file `sources/<name>` in the KB root:

1. Read the file contents.
2. Apply the extraction criteria in
   `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-fact.md` to
   identify candidate fact triples AND to name relations and entities. This
   reference is the authoritative source for relation/entity naming during
   fact extraction. (Do NOT use `text-to-datalog.md` here — that document is a
   natural-language-question→Datalog-query converter, used only by the
   `/factlog query` step, not for naming fact relations.)
3. Produce a JSON array where every element is a JSON **object** (dict) with
   the following named keys matching `FACT_HEADER`:

   ```json
   {
     "subject":    "Entity A",
     "relation":   "relation_name",
     "object":     "Entity B",
     "source":     "sources/<name>",
     "status":     "candidate",
     "confidence": 0.90,
     "note":       "brief rationale or empty string"
   }
   ```

   Each `runs/*.json` file is a JSON **array of such objects** — do NOT write a
   flat 7-element array `[subject, relation, object, ...]`; array-shaped
   elements are silently skipped by `merge_candidates.py` (only `dict` items
   are accepted at line 157).

   Required non-empty fields (`FACT_HEADER[:4]`): `subject`, `relation`,
   `object`, `source`. Rows with any of these four empty are dropped.

   - `source` MUST be `"sources/<name>"` (sources/-prefixed, KB-root-relative).
   - `status` is `"candidate"` for uncertain rows, `"needs_review"` if a human
     must decide, `"confirmed"` only when a prior human has marked it.
   - `confidence` may be a JSON number (e.g. `0.90`) or a quoted string
     (e.g. `"0.90"`) — both are accepted because `merge_candidates.py`
     coerces the value via `str()` before normalisation.
   - `note` is a brief rationale string (may be empty string `""`).

4. Write the array to `$FACTLOG_ROOT/runs/<iso-timestamp>-<slug>.json`.
   One file per source document keeps the audit trail clean.

### Step 2 — Deterministic merge (engine script)

Run merge_candidates.py to normalise, deduplicate, write `facts/candidates.csv`,
regenerate `pages/`, and update `decisions/open-questions.md`:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/merge_candidates.py" --wiki "$FACTLOG_ROOT"
```

The script reads all `runs/*.json` files (see `--input` for a custom glob).
Rows whose `source` field is not a valid `sources/`-prefixed path are dropped
with a warning. Pass `--strict` to make any dropped row a hard failure.

**Do not edit `facts/candidates.csv` or `pages/` directly.** These are engine
outputs; the engine owns them. Only `runs/*.json` is the LLM write surface for
this step.

---

## `/factlog query` — translate questions into a Datalog query draft

**Purpose:** Read the natural-language research questions in `policy/questions.md`
and translate each one into a Datalog query draft, writing the result to
`facts/query.dl`. This is the question→query-draft contract artifact required by
AC3. It is performed natively by Claude in-session — do NOT spawn a `claude -p`
subprocess.

`facts/query.dl` is an engine input consumed by `/factlog check` (the wirelog
logic check runs over `facts/accepted.dl` **and** `facts/query.dl`). Run
`/factlog query` **before** `/factlog check` so a query draft exists to evaluate.

**Execution order:**

### Step 1 — Load questions and schema context

1. Read `policy/questions.md` and collect each natural-language question
   (one per bullet / list item).
2. Read `facts/accepted.dl` and `policy/logic-policy.dl` to build the schema
   context: the entity names and relations that actually exist as engine input,
   plus the allowed policy/query predicates. Only these may appear in a query.
   (On a fresh KB, `facts/accepted.dl` may be empty until `/factlog check`
   compiles it; in that case every question that cannot be safely expressed
   becomes a `review_required(...)` line — see below.)

### Step 2 — Native question→query translation (LLM, in-session)

For each question, apply the translation criteria in
`${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-datalog.md`,
substituting:
- `{{SCHEMA_CONTEXT}}` — the accepted entities/relations/predicates from Step 1.
- `{{QUESTION}}` — the natural-language question text.

The reference emits a single JSON object `{"query": "...", "note": "..."}`:
- `query` is a one-line Datalog query ending with `?`, using only entities and
  relations present in `facts/accepted.dl`; OR
- `review_required("<verbatim question>")?` when the question asks about a
  `needs_review`/`candidate` fact, or cannot be safely expressed. The original
  natural-language question text MUST appear verbatim inside `review_required`
  (never a `Q`-style placeholder).

### Step 3 — Write `facts/query.dl` (single batch)

Write all translated query lines to `facts/query.dl` in **one** Write call —
one query (or `review_required(...)`) line per source question. Do not write the
file incrementally line-by-line: a single batched write avoids a second-write
that the PreToolUse gate would deny once a report exists.

On a freshly initialised KB (no `facts/logic_report.txt` and no pre-existing
`facts/query.dl`), the PreToolUse gate allows this first creation (bootstrap).
After `/factlog check` produces a report, re-running `/factlog query` requires a
fresh report first — run `/factlog check` to refresh, then re-write.

---

## `/factlog check` — compile accepted facts and run the logic check

**Purpose:** Promote confirmed facts to engine input, run the wirelog logic
check, and display the full report verbatim.

**Execution order (must be sequential — each step depends on the previous):**

### Step 1 — Compile accepted facts

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/compile_facts.py"
```

Reads `facts/candidates.csv`, filters rows with `status` in
`{confirmed, accepted}`, and writes `facts/accepted.dl`. Show the stdout.

### Step 2 — Run the logic check

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/run_logic_check.py"
```

Runs the wirelog/pyrewire engine over `facts/accepted.dl`,
`policy/logic-policy.dl`, and the query draft `facts/query.dl` (produced by
`/factlog query`). Each query line in `facts/query.dl` is validated and
evaluated; `review_required(...)` lines are surfaced for human follow-up.
Writes and prints `facts/logic_report.txt`.

**Precondition:** run `/factlog query` first so `facts/query.dl` exists. If it
is absent, the engine simply evaluates no queries (the report still compiles
accepted facts), but the AC3 contract artifact will be missing.

### Step 3 — Show the report verbatim

Read `facts/logic_report.txt` and output its **full text** with no omissions.
Never paraphrase or summarise the report. The literal text is the evidence.

Surface any `Policy Findings`, `Errors`, and `Warnings` sections under a
"needs review" heading so the human can act on them without searching.

**Gate:** If `errors > 0` in the report, stop here. Do not proceed to `/factlog
repair` without explicit human instruction. Do not state any conclusion about
the KB until errors reach 0.

---

## `/factlog repair` — gated self-correction of `review_required` queries

**Purpose:** Attempt to repair `review_required(...)` entries in
`facts/query.dl` using the self-correct criteria, then re-validate each repair
before writing it back.

**Precondition:** `facts/logic_report.txt` must exist and must be fresh (i.e.,
`/factlog check` must have been run after the last edit to `facts/accepted.dl`
or `facts/query.dl`). The PreToolUse hook enforces this: it will deny any
attempt to write or edit `facts/accepted.dl` or `facts/query.dl` when
`facts/logic_report.txt` is absent or stale.

**Execution order:**

### Step 1 — Identify repair targets

Read `facts/query.dl`. Collect all lines that start with `review_required(`.
These are the draft queries awaiting repair.

### Step 2 — Load schema context

Read `facts/accepted.dl` and `policy/logic-policy.dl` to build the schema
context for the self-correct prompt (accepted entity names, allowed relations,
allowed policy predicates).

### Step 3 — Native self-correct (LLM, in-session)

For each `review_required(...)` line:

1. Render the self-correct prompt from
   `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/self-correct.md`,
   substituting:
   - `{{SCHEMA_CONTEXT}}` — accepted predicates/entities from step 2.
   - `{{LOGIC_REPORT}}` — verbatim text of `facts/logic_report.txt`.
   - `{{DRAFT_QUERY}}` — the `review_required(...)` line.

2. Produce a single JSON object `{"query": "...", "note": "..."}`.
   - `query` must be a one-line Datalog query ending with `?`.
   - If confident repair is impossible, return the original
     `review_required("original question")?` unchanged.

### Step 4 — Re-validate each proposed repair (deterministic)

Before writing any repaired query back to `facts/query.dl`, call
`common.validate_candidate_query` (from `${CLAUDE_PLUGIN_ROOT}/tools/common.py`)
to confirm the query passes schema and engine re-validation:

```python
from common import validate_candidate_query, load_accepted_facts
facts = load_accepted_facts()
ok, reason = validate_candidate_query(proposed_query_line, facts)
```

- If `ok` is `True`: stage the repair.
- If `ok` is `False`: keep the original `review_required(...)` line and record
  the failed attempt.

### Step 5 — Write results

- If any repairs passed validation, write the updated `facts/query.dl`
  (original lines with repaired queries substituted in place).
- Append a correction trace to `decisions/correction_trace.md`:

  ```
  ## <ISO timestamp> repair run
  - repaired: <count>
  - kept (not repairable): <count>
  - kept (validation failed): <count>
  [one line per attempt: query | result | reason]
  ```

- If zero repairs succeeded, do NOT write `facts/query.dl`. Log the trace only.

### Step 6 — Re-run the logic check

After any write to `facts/query.dl`, immediately run:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/run_logic_check.py"
```

Show the new `facts/logic_report.txt` verbatim. This is the final evidence for
the repair session.

---

## Extraction & translation criteria (references)

All four reference documents are authoritative constraints — read them before
any LLM extraction or query-translation step:

- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-fact.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-datalog.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/natural-language-to-policy.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/self-correct.md`
