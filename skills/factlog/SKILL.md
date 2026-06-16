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
field MUST be a path relative to the KB root, prefixed with `sources/` (the
user's originals) or `runs/sources/` (text conversions of binary originals
produced by `factlog ingest`).

Examples:
- `"sources/my-doc.md"`
- `"sources/subdir/notes.md#section-heading"`
- `"runs/sources/report.md"`  (a converted `.docx`/`.pdf` original)

Bare filenames (e.g. `"my-doc.md"`) are NOT valid and will be silently dropped
by `merge_candidates.py`. Always include the `sources/` or `runs/sources/` prefix.

---

## `/factlog setup` — one-shot post-install bootstrap (run this FIRST)

**Purpose:** Collapse the post-`/plugin install` steps (dependency install,
environment check, KB init) into a single command. Run this **before** any of
the four operating commands below — it is the first thing to do after
`/plugin install factlog@semantic-reasoning`.

**How it runs:** in-session, by Claude executing the bundled CLI — NOT in a
separate terminal:

```bash
python3 -m factlog setup --target <kb>
```

In order, `setup`:

1. Runs the `doctor` checks and reports Python / pyrewire status.
2. If pyrewire is missing or `< 1.0.1`, installs it via
   `python3 -m pip install -r <requirements.txt>` (located via
   `$CLAUDE_PLUGIN_ROOT` if set, else the package root). If pyrewire already
   satisfies the floor, the install is skipped.
3. Runs the KB `init` for `--target` (scaffolds `sources/`, `facts/`,
   `policy/`, etc.).
4. Re-runs `doctor` and prints a concise summary of what was done and what (if
   anything) the user must do next.

`setup` is idempotent and safe to re-run.

**venv fallback (PEP 668):** if the active Python is externally managed, pip
will refuse to install into it. `setup` does **not** override this with
`--break-system-packages`; instead it prints venv guidance and exits non-zero.
Create and activate a virtual environment, then re-run:

```bash
python3 -m venv ~/.factlog-venv
source ~/.factlog-venv/bin/activate
python3 -m factlog setup --target <kb>
```

After `setup` succeeds, use the four operating commands — `/factlog sync`,
`/factlog query`, `/factlog check`, `/factlog repair` — in that order.

---

## `/factlog add` — one-shot capture (low friction)

**Purpose:** Add one piece of knowledge (a file or free text) and finalise the
KB in a single pass — so capturing is as light as a plain notes wiki, but you
still get the verification tier. It composes the existing steps; the only LLM
step is extraction.

**Execution order:**

### Step 1 — Place the source

- A binary/office file (`.docx`, `.pdf`, ...): run
  `python3 -m factlog ingest <path> --target "$FACTLOG_ROOT"` (or `--scan`)
  → it writes a text conversion into `runs/sources/`.
- Free text or a text file: place it under `sources/<name>` (text is read
  verbatim by extraction).

### Step 2 — Extract candidates (LLM, in-session)

Apply `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-fact.md` to the
new source and write candidate rows to `runs/<iso>-<slug>.json` — identical to
`/factlog sync` Step 1 (source is `sources/<name>` or `runs/sources/<name>`).

### Step 3 — Finalise deterministically (one command)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/finalize.py" --target "$FACTLOG_ROOT"
```

`finalize.py` chains the deterministic engine steps — `merge_candidates` →
ensure `policy/logic-policy.dl` → `compile_facts` → **contradiction check** →
`run_logic_check` — and prints a summary (candidates merged, engine facts,
conflicts, the logic report). It is idempotent and read-only with respect to
hand-edited inputs (only the engine scripts touch their outputs). If `pyrewire`
is unavailable the logic check is skipped with a note; facts are still merged
and compiled.

**Contradiction detection.** Relations you list in `policy/single-valued.md`
(one relation name per line) are treated as *functional* — at most one object
per subject. If two distinct objects are asserted for the same
(subject, single-valued relation), `finalize` reports a `CONFLICT` and exits
non-zero. Resolve non-destructively by marking the outdated row's status as
`superseded` in `facts/candidates.csv` (it stays for audit, drops out of
`accepted.dl`, and the conflict clears). This resolution is **durable**: a
re-`merge` preserves rows you marked `superseded` in `candidates.csv` even when
a run re-asserts the retired fact. This keeps the KB free of the silently-
accumulated contradictions a plain notes wiki cannot prevent.

Use `/factlog add` for quick capture; use the explicit `sync → query → check →
repair` sequence when you need the full question→query workflow.

---

## `/factlog sync` — extract candidates and merge into KB

**Purpose:** Read every file under `sources/`, extract candidate facts in
native Claude in-session (no subprocess), write them as `runs/*.json`, then
delegate merging and page generation to the deterministic engine.

**Execution order:**

### Step 0 — Convert binary sources (deterministic, run first)

Extraction reads `sources/` files as text, so binary/office originals
(`.docx`, `.pdf`, ...) yield no facts on their own. Run the bundled converter
first:

```bash
python3 -m factlog ingest --scan --target "$FACTLOG_ROOT"
```

`--scan` auto-discovers every binary file under `sources/` and writes a text
conversion (with a provenance header) into `runs/sources/` — never into
`sources/`. It is idempotent (unchanged files are skipped). Then extract from
**both** `sources/` (native text) and `runs/sources/` (conversions).

### Step 1 — Native fact extraction (LLM, in-session)

For each file under `sources/<name>` **and** `runs/sources/<name>` in the KB root:

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

**Concept-page layout (`templates/pages.md`).** The markdown layout of each
regenerated `pages/<entity>.md` comes from `<kb>/templates/pages.md` (scaffolded
by `factlog init`). Edit that file to change the page layout per KB — no plugin
code change needed. Placeholders: `{{ENTITY}}`, `{{SOURCES}}`, `{{RELATIONS}}`,
`{{REVIEW}}` (each block falls back to a "없습니다" line when empty). If the file
is absent, a built-in default identical to the scaffolded seed is used. The
`<!-- generated-by-factlog -->` marker is always guaranteed in the output (auto-
prepended if a custom template omits it) — this is what keeps regeneration
non-destructive, so hand-authored pages without the marker are never touched.

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

**Precondition:** `/factlog query` is the documented predecessor of this step —
the intended order is **sync → query → check → repair**. Run `/factlog query`
first so `facts/query.dl` exists for the logic check to evaluate.

An absent `facts/query.dl` is tolerated by the engine only as *graceful
degradation*, not as a supported shortcut: the report still compiles accepted
facts and prints `no facts/query.dl found` under "Query evaluation", but this
means the question→query step (`/factlog query`) was skipped and the AC3
contract artifact is missing. Do not treat the query step as optional — run it
before `/factlog check`.

### Step 3 — Show the report verbatim

Read `facts/logic_report.txt` and output its **full text** with no omissions.
Never paraphrase or summarise the report. The literal text is the evidence.

Surface any `Policy Findings`, `Errors`, and `Warnings` sections under a
"needs review" heading so the human can act on them without searching.

**Gate:** If `errors > 0` in the report, stop here. Do not proceed to `/factlog
repair` without explicit human instruction. Do not state any conclusion about
the KB until errors reach 0.

### Step 4 — Coverage critic (silent-omission guard)

A free-text wiki cannot tell you what it *failed* to capture. Run the coverage
critic to surface sources the KB has not extracted any facts from:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/coverage.py" --wiki "$FACTLOG_ROOT"
```

It reports, per source file under `sources/` and `runs/sources/`, how many
**engine-input** facts (status `confirmed`/`accepted`) cite it, and flags the
gaps deterministically. Counting uses engine facts only — a source backed solely
by `superseded` or `needs_review` rows contributes nothing to `accepted.dl`, so
it is correctly reported as a gap, not "covered":

- **text gap** — a text source with 0 facts: an extraction gap; re-run
  `/factlog sync` (or investigate why nothing was extracted).
- **binary gap** — a binary source under `sources/` with 0 facts: it needs
  conversion first via `factlog ingest`. A binary under `runs/sources/` is
  instead flagged as an anomaly (that directory holds ingest *output*, which
  should already be text).
- **orphan citation** — a fact cites a source path with no file on disk (a
  stale or typo'd reference); surfaced on stderr.

The script is the **deterministic half** (per-source fact counts, unreferenced
sources, orphan citations); it always exits 0 so it never blocks the pipeline —
including on a brand-new KB with no `candidates.csv` yet. Pass `--strict` to exit
non-zero when any *text* source is uncovered (useful in automation).
Judging **semantic** gaps — an entity mentioned in a source but with no relation
extracted — is the **in-session critic's** job: read the flagged sources and
decide whether the missing facts are real omissions worth a follow-up
`/factlog add`.

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

## `/factlog ask` — answer one question (engine facts vs wiki exploration)

**Purpose:** Answer a single natural-language question by **deterministically**
routing it to either the facts/rule **engine** (verified) or **wiki
exploration** (unverified). You draft a candidate query; a bundled script
decides the route and renders the answer. **You never decide whether an answer
is verified** — the script does, from a stable classification code.

`/factlog ask` is **read-only** with respect to engine inputs: it never writes
`facts/query.dl` or `facts/accepted.dl` (no PreToolUse-gate interaction).

### Step 1 — Draft a candidate query (LLM, in-session)

Render `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-datalog.md` for
the question (schema context from `facts/accepted.dl` + `policy/logic-policy.dl`)
and produce ONE candidate Datalog query line — exactly as in `/factlog query`,
including the `review_required("<verbatim question>")?` fallback.

### Step 2 — Classify deterministically

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/ask_router.py" validate "<draft>" --target "$FACTLOG_ROOT"
```

This prints JSON `{ok, code, reason, route, negative, predicate}`. **Branch on
`route`/`code`, never on `reason` text:**

- `route == "engine"` → Step 3a (the engine answer; `negative=true` is a
  *verified negative*, a real answer — never treat it as "no answer").
- `route == "wiki"` → Step 3b.

### Step 2′ — Multi-draft probe (reduce missed-engine)

A single draft can misname a canonical entity/relation and wrongly fall to wiki.
So retry **up to 3 drafts**, feeding the validator's `reason` (it names the
offending token) back into the next draft to self-correct vocabulary. Stop early
and go to wiki only when **every** attempt fails with a shape/vocabulary `code`
(`unknown_predicate`, `entity_not_accepted`, `relation_not_accepted`,
`bad_arity`, `malformed`, `unsupported`). A `fact_absent` code short-circuits
immediately to a **verified negative** (Step 3a) — the vocabulary is already
correct, so retrying is pointless.

### Step 3a — Engine answer (VERIFIED)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/ask_router.py" render "<draft>" --target "$FACTLOG_ROOT"
```

Show the `VERIFIED — engine` block verbatim (positive rows, or `rows: 0` /
"no such fact (verified negative)"). This is engine-backed evidence. Relation
rows are annotated `(sources: N, confidence: C)` — distinct-source count (a
multi-source trust signal; `tools/corroboration.py` reports the full view) and
max confidence — plus `[stale: source missing]` when a backing source has
vanished and the fact should be re-verified.

### Step 3b — Wiki exploration (UNVERIFIED)

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/ask_router.py" wiki "<question>" --reason "<why>" --target "$FACTLOG_ROOT"
```

Show the `UNVERIFIED — wiki exploration` block verbatim (cited `sources/` /
`runs/sources/` excerpts; `decisions/` is supplementary). When the question
mentions accepted entities, the block also carries a clearly-separated
`VERIFIED — engine (grounding: ...)` section listing the engine-verified facts
about those entities — verified anchors beside the unverified prose. The
unverified excerpts cite only source text, never `facts/accepted.dl`. Do NOT
present wiki excerpts as confirmed facts. Optionally record the unanswered
question for later review (a non-engine-input sink, never `facts/query.dl`):

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/tools/ask_router.py" note "<question>" --target "$FACTLOG_ROOT"
```

---

## Extraction & translation criteria (references)

All four reference documents are authoritative constraints — read them before
any LLM extraction or query-translation step:

- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-fact.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-datalog.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/natural-language-to-policy.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/self-correct.md`
