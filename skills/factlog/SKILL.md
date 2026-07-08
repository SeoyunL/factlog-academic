---
name: factlog
description: >-
  Keep a markdown knowledge base honest: extract source-backed candidate facts
  from sources/, compile confirmed facts, run a deterministic Datalog/wirelog
  logic check, and attempt gated self-correction. Use when the user asks to
  "sync facts", "check the wiki", "run factlog", "verify facts", or update a
  knowledge base from its source documents.
argument-hint: "setup | add | sync | query | check | repair | ask"
allowed-tools: Bash(*factlog_python.sh *) Bash(python3 *) Bash(python *) Bash(py *) Read Edit Write Grep Glob
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
2. Always run `"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/run_logic_check.py"` and show
   the resulting `facts/logic_report.txt` **verbatim** before stating any
   conclusion.
3. If the report shows `errors > 0`, return to the human instead of concluding.
   Surface `Policy Findings`, `warnings`, and `review_required` under a
   separate "needs review" section.
4. Only edit `facts/query.dl` during self-correction when the repaired query
   passes schema and engine re-validation; otherwise keep the original and log
   the attempt to `decisions/correction_trace.md`.

## Resolve the active KB root first (every flow except setup)

Before any LLM read/write in a flow (`sync`, `query`, `check`, `repair`, `add`,
`ask`), determine the active KB root **deterministically** — do not assume
`$FACTLOG_ROOT` is already exported. Run this **once** at the start of the flow
and export it, so every later sub-command and the PreToolUse gate hook inherit
the *same* value instead of each re-resolving it:

```bash
export FACTLOG_ROOT="$("${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" -m factlog where --porcelain)"
```

`factlog where --porcelain` prints **only** the resolved KB root (one absolute
path, no label) — parse-free and stable, so do not scrape the human-readable
`factlog where` output. Both use the exact same precedence the engine and CLI
tools use (`factlog/config.py` `resolve_root`):

> **`--wiki`/`--target` flag  >  `$FACTLOG_ROOT`  >  active-KB config file  >  cwd**

Exporting once turns the hook↔tool agreement from a "same-env assumption" into an
enforced invariant: every later command and the gate hook read this exact root.

For diagnostics, the plain `factlog where` (no flag) additionally prints where the
root was resolved from and the config file path — use it to debug, but always
machine-read the root via `--porcelain`.

Note: `factlog where` observes `$FACTLOG_ROOT`, the config, and cwd — it does
**not** see a flow's `--target`/`--wiki`. Slash flows normally rely on the active
KB with no flag, so this matches. If a flow does pass an explicit
`--target`/`--wiki`, that value wins over what `factlog where` reports. When you
export `FACTLOG_ROOT` as above, resolution is idempotent (an exported root
re-resolves to itself).

Use that resolved path as the single KB root for the whole flow:

- Read sources from `<kb-root>/sources/` and write extracted candidates to
  `<kb-root>/runs/` at that path (the docs below write these as
  `$FACTLOG_ROOT/sources/` and `$FACTLOG_ROOT/runs/`; treat `$FACTLOG_ROOT` as
  that resolved root).
- Pass that same root to every command's `--target`/`--wiki`.
- If `$FACTLOG_ROOT` is already exported, it wins over the config file (matching
  the precedence above), so honour it as-is.

**Fallback (first-time users):** if the diagnostic `factlog where` (no flag)
reports the root came from `cwd` — i.e. no `--wiki`/`--target` flag, no
`$FACTLOG_ROOT`, and no active-KB config — operate in the current working
directory. This is the tutorial path
where you copy `examples/sample-kb` and run `factlog use` (see
`examples/sample-kb/README.md`).

**When an active KB IS configured, never fall back to cwd or the bundled
`examples/sample-kb`.** Running a slash command from inside the factlog source
repo must still target the configured active KB, so the LLM extraction step and
the engine step operate on the *same* KB.

## Output language (assistant prose only)

At the **start of every flow**, decide the language for your **human-facing
narration, summaries, and "needs review" framing**. Check the configured
language deterministically:

```bash
factlog lang   # prints the configured code (e.g. ko) on one line, or an empty line
```

Decide with a **two-step precedence** (explicit setting wins):

1. **If `factlog lang` prints a code → narrate in that language.** This is the
   reliable signal: it works even when the user only typed a slash command.
2. **If it prints an empty line → narrate in the user's conversation language**
   (best effort). When there is no conversational signal — e.g. the user only ran
   a command with no natural-language sentence — there is nothing to detect from,
   so keep the previous default (the model's default language). Never guess the
   UI language from the language of the KB's facts; data language ≠ UI language.

Set it with `factlog lang <code>` (or `factlog use <kb> --lang <code>` /
`factlog setup --lang <code>`); an empty value (`factlog lang ""`) clears it and
returns to step 2. `factlog lang` with no argument is a porcelain contract —
parse exactly that one line, do not scrape prose.

**Boundary — this changes ONLY your own prose. It does NOT change evidence:**

- **Engine reports (`facts/logic_report.txt`) and CLI stdout stay verbatim** —
  show them exactly as produced (Deterministic gate rule #2). **Do not translate
  them.** Instead, add a **short gloss** in the chosen language *beside* the
  verbatim block to explain what it means.
- **Fact data (subject / relation / object) stays in its source language** — never
  translate a fact's values; the KB records them as extracted.

## Canonical source value for fact extraction

When writing extracted fact rows to `$FACTLOG_ROOT/runs/*.json`, the `source`
field MUST be a path relative to the KB root, prefixed with `sources/` (the
user's originals) or `runs/sources/` (text conversions of binary originals
produced by `factlog ingest`).

Examples:
- `"sources/my-doc.md"`
- `"sources/subdir/notes.md#section-heading"`
- `"runs/sources/report.pdf.md"`  (a converted `.docx`/`.pdf` original — the conversion keeps the original's full name, extension included, so same-stem originals never collide; #213)

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
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" -m factlog setup --target <kb>
```

In order, `setup`:

1. Runs the `doctor` checks and reports Python / pyrewire status.
2. If pyrewire is missing or `< 1.0.3`, installs it via
   `"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" -m pip install -r <requirements.txt>` (located via
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
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" -m venv ~/.factlog-venv
source ~/.factlog-venv/bin/activate
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" -m factlog setup --target <kb>
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
  `"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" -m factlog ingest <path> --target "$FACTLOG_ROOT"` (or `--scan`)
  → it writes a text conversion into `runs/sources/`.
- Free text or a text file: place it under `sources/<name>` (text is read
  verbatim by extraction).

### Step 2 — Extract candidates (LLM, in-session)

Apply `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-fact.md` to the
new source and write candidate rows to `runs/<iso>-<slug>.json` — identical to
`/factlog sync` Step 1 (source is `sources/<name>` or `runs/sources/<name>`).

### Step 3 — Finalise deterministically (one command)

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/finalize.py" --target "$FACTLOG_ROOT"
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
non-zero. Resolve it non-destructively **with a command, not by hand-editing
`candidates.csv`**: retire the outdated fact so it drops out of `accepted.dl`
and the conflict clears, while the row stays for audit (status `superseded`).

- If the outdated fact is still pending (candidate/needs_review):
  `factlog reject <subject> <relation> <object>` (`-` wildcards a position).
- If it has already been accepted:
  `factlog eject --fact <subject> <relation> <object>` (marks it `superseded` by
  default, leaving the source in place; `--purge` removes the row entirely).

This resolution is **durable**: a re-`merge` preserves the `superseded` status
even when a run re-asserts the retired fact. This keeps the KB free of the
silently-accumulated contradictions a plain notes wiki cannot prevent.

**Entity vs literal typing.** Relations you list in `policy/attribute-relations.md`
(same one-name-per-line format as `single-valued.md`) are treated as
*literal-valued*: their object is a value (a date, number, ordinal, ...), not a
first-class entity. Such objects are kept OUT of the entity set, so they do not
appear as entities, path nodes, or `count` subjects, and the query translator
won't mistake a date for an entity. They remain fully verifiable as relation
objects — `relation("을서비스", "정식_운영", "2030.1")?` still resolves. The file is
optional; with no declarations the entity set is unchanged (every object is an
entity). Run `tools/entity_audit.py` to find candidates (objects that look like
literals under a relation you haven't declared).

**Typed comparison predicates (hand-authored).** A relation declared in
`policy/typed-relations.md` with a type tag and an ASCII alias —
e.g. `- `정식_운영` : date as launch_date` — is projected each run into a typed
side-relation `launch_date(subject: symbol, v: int64)` keyed on the subject. To
*ask a comparison over it* ("which subjects launched on/after 2030?"), write the
rule yourself in the optional file **`policy/logic-policy.extra.dl`** (NOT
`logic-policy.dl`, which is regenerated from `logic-policy.md` and byte-checked
by `generate_logic_policy.py --check`; a hand-authored rule there is flagged
stale). `load_logic_policy()` concatenates `logic-policy.extra.dl` onto the
generated program when it exists and `--check` never touches it. The
comparison-predicate head **must be arity-2 `(entity: symbol, reason: symbol)`
with a quoted reason string** — the same shape as a `requires_review` finding —
and the scalar value stays in the **body**, never the head:

```
.decl after2030(entity: symbol, reason: symbol)
after2030(S, "launch_after_2030") :- launch_date(S, D), D >= 20300101.
```

(A subject-only arity-1 head like `after2030(S)` crashes the report's
findings path and is rejected by query classification; a bare scalar in the
head is also mis-decoded as an interned symbol. The quoted reason is pre-interned
and safe.) The threshold is the *question*, not a property of the relation, so
you supply it: `D >= 20300101` is inclusive of the boundary day (2030-01-01 is
included). For `date`, the value is a sortable `yyyymmdd` int64 — a source object
`2030.1` normalises to `20300101` (missing parts default to `01`), so a comparison
threshold is also written `yyyymmdd`. The source object must be in a parseable
form (`2030.1`, not a bare `2030`). Typed source objects may also be emitted as
compact compound terms when that preserves structure better than prose strings:
`date(2030,1)`, `date(2030,1,15)`, `number(2.5)`, `ordinal(3)`, or
`amount(100,"억")`. The flat `relation/3` fact still stores that term as the
object string, while the typed side-relation projects its comparable scalar.
`ordinal` compares on **rank only**: the ordinal-class unit (호/위/번/차/등/째, and
English st/nd/rd/th) is dropped at normalization, so `제3호` and `3위` are the *same* value (rank 3) to both
the engine and the conflict checker. If a rank and a house number are genuinely
different domains, model them as **separate relations** rather than one ordinal
relation (contrast `amount`, where 억↔조 equivalence is intended).
The predicate's rows surface in
`logic_report.txt` under `Policy Findings:` (`after2030: 을서비스 (launch_after_2030)`)
via the existing policy-findings path, because its `.decl` name is auto-discovered
by `policy_predicates()`. With no `typed-relations.md` and no
`logic-policy.extra.dl`, behaviour is byte-identical to a KB without the feature.

**Relation aliases and canonical/3 (two authoring lanes).** `policy/relation-aliases.md`
declares surface-to-canonical predicate mappings (one `` `raw` -> `canonical` `` bullet per
line). At compile time, `compile_facts.py` emits a `canonical/3` EDB block in `facts/accepted.dl`
for every alias-participating fact, so a logic-policy rule whose body references `canonical/3`
fires over any surface variant without naming it explicitly. There are two ways to author such a
rule.

**Lane A — declare it in `logic-policy.md` with a `{canonical}` prefix (preferred, #243).**
Prefix the bullet text (the part after the `[id]` tag) with a literal, lowercase `{canonical}`
token, anchored at the very start:

```
- [retracted_conclusion] {canonical} 문서가 `결론` 이면서 `철회상태` 이면 철회로 본다.
```

`generate_logic_policy.py` then emits `canonical(X, "결론", _)` bodies instead of
`relation(X, "결론", _)`, and the result is byte-checked by `generate_logic_policy.py --check`
like every other generated rule. Use Lane A for rules the `.md` DSL already expresses:
`predicate(X, "reason") :- canonical(X, "rel", _), … .` — an arity-2 `(entity, reason)` head, a
single `X` variable, and a body that is a pure conjunction of `canonical/3` atoms. The marker is
**literal-lowercase and anchored-prefix only**: a mid-sentence or prose `{canonical}` (e.g.
`이 규칙은 {canonical} 방식을 쓴다`) is NOT a marker and produces an ordinary `relation/3` rule.

**Lane B — hand-author in `policy/logic-policy.extra.dl` (for what the DSL can't express).**
Use `extra.dl` for canonical rules the `.md` DSL cannot represent: mixed relation+canonical
bodies, negation, typed comparisons (#120), `path/2`, or a head/body variable other than `X`:

```
// policy/logic-policy.extra.dl
.decl conflict(entity: symbol, reason: symbol)
conflict(X, "retracted_conclusion") :-
  canonical(X, "결론", _),
  canonical(X, "철회상태", _).
```

`extra.dl` is the same channel used for typed-comparison predicates (#120); it is NOT regenerated
or byte-checked by `generate_logic_policy.py --check`, so authors may edit it directly. A canonical
rule placed in `logic-policy.dl` (the generated file) instead would be flagged STALE and can be
regenerated away — use Lane A or `extra.dl`, never the generated file.

**Rule of thumb:** if the rule is a pure conjunction of `canonical/3` atoms with an `X`-headed
arity-2 finding, use Lane A (`{canonical}` in `logic-policy.md`); otherwise use Lane B (`extra.dl`).

Two invariants hold for both lanes:

1. **`policy/relation-aliases.md`** declares the surface→canonical mappings; `/factlog ask`
   resolves canonical queries across all variants (Slice-1, already shipped). With no aliases,
   `canonical/3` is empty and a canonical-bodied rule simply no-ops (backward compatible).
2. **`canonical` is a reserved engine EDB predicate** — populated automatically from
   `relation-aliases.md` into `accepted.dl`. Use it freely in rule *bodies* (right of `:-`),
   but **never as a rule head or bare fact** in `logic-policy(.extra).dl`. A head occurrence
   makes pyrewire treat `canonical` as IDB and silently drops all compile-emitted EDB atoms
   (wrong answers, rc=0). The engine rejects such policy text with a loud `FactlogError`.
   The predicate shape for the head must be arity-2 `(entity: symbol, reason: symbol)` with a
   quoted reason string — the same shape as typed-comparison and `requires_review` findings.

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
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" -m factlog ingest --scan --target "$FACTLOG_ROOT"
```

`--scan` auto-discovers every binary file under `sources/` and writes a text
conversion (with a provenance header) into `runs/sources/` — never into
`sources/`, mirroring the original's subdirectory and keeping the original's
full name so same-stem originals never collide (`sources/sub/x.pdf` →
`runs/sources/sub/x.pdf.md`; #213). It is idempotent (unchanged files are skipped).
Sources matching `policy/sync-ignore.md` are skipped. Then extract from **both**
`sources/` (native text) and `runs/sources/` (conversions).

### Step 1 — Native fact extraction (LLM, in-session)

**Sync-ignore:** first read `policy/sync-ignore.md` (if present) and SKIP any
source whose path matches one of its glob patterns — by full ref (`sources/...`
or `runs/sources/...`) or by the path within the source root (so `drafts/*.md`
matches `sources/drafts/x.md`). These sources are excluded from re-extraction on
purpose; their already-merged facts are left as-is. (Manage the list with
`factlog ignore`.)

For each *non-ignored* file under `sources/<name>` **and** `runs/sources/<name>`
in the KB root:

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
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/merge_candidates.py" --wiki "$FACTLOG_ROOT"
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
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/compile_facts.py"
```

Reads `facts/candidates.csv`, filters rows with `status` in
`{confirmed, accepted}`, and writes `facts/accepted.dl`. Show the stdout.

To promote `candidate`/`needs_review` rows into engine input (or retire them)
without hand-editing `candidates.csv`, use the review CLI: `factlog review`
lists the pending queue, `factlog accept <subject> <relation> <object>` sets
matching pending rows to `accepted`, and `factlog reject ...` sets them to
`superseded` (both recompile `accepted.dl`; `-` wildcards a position). To
correct a fact's value, `factlog amend <subject> <relation> <object>
--set-object ... [--set-subject/--set-relation/--set-note] [--accept]` rewrites
it durably (updates both `candidates.csv` and the backing `runs/*.json`). These
human decisions are preserved across re-merge.

### Step 2 — Run the logic check

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/run_logic_check.py"
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
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/coverage.py" --wiki "$FACTLOG_ROOT"
```

It reports, per source file under `sources/` and `runs/sources/`, how many
**engine-input** facts (status `confirmed`/`accepted`) cite it, and flags the
gaps deterministically. Counting uses engine facts only — a source backed solely
by `superseded` or `needs_review` rows contributes nothing to `accepted.dl`, so
it is correctly reported as a gap, not "covered":

- **text gap** — a text source with 0 facts: an extraction gap; re-run
  `/factlog sync` (or investigate why nothing was extracted).
- **binary gap** — a binary source under `sources/` with **no conversion** at
  all: it needs conversion first via `factlog ingest`. A binary that already has
  a `runs/sources/<original-name>.md` conversion is **not** a gap — facts attach to the
  conversion, so the original is reported as *covered via conversion* (it counts
  toward "covered", with a `(N via conversion)` note in the summary). A binary
  under `runs/sources/` is instead flagged as an anomaly (that directory holds
  ingest *output*, which should already be text).
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
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/run_logic_check.py"
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
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/ask_router.py" validate "<draft>" --target "$FACTLOG_ROOT"
```

This prints JSON `{ok, code, reason, route, negative, predicate,
policy_uncompiled}`. **Branch on `route`/`code`, never on `reason` text:**

- `route == "engine"` → Step 3a (the engine answer; `negative=true` is a
  *verified negative*, a real answer — never treat it as "no answer").
- `route == "wiki"` → Step 3b.

`policy_uncompiled == true` means the author wrote rules in
`policy/logic-policy.md` but never compiled `policy/logic-policy.dl`, so ask is
answering with **no policy applied** — the same condition `/factlog check` fails
loud on (#193). Ask stays graceful — it warns, it does not suppress the answer —
but *which command* prints the one-line `WARNING: policy is uncompiled …` depends
on the route: on the **engine** route `render` appends the warning text itself; on
the **wiki** route `render` only forwards the `policy_uncompiled` flag in its JSON
directive (no text), and the warning text is appended by the `wiki` command. Either
way you show the warning verbatim (below). Tell the user to run
`tools/generate_logic_policy.py` (or `/factlog add`) to compile the policy.

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
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/ask_router.py" render "<draft>" --target "$FACTLOG_ROOT"
```

Show the `VERIFIED — engine` block verbatim (positive rows, or `rows: 0` /
"no such fact (verified negative)"). This is engine-backed evidence. The engine
verdict is **binary** — a row is verified or it is not; the annotations describe
the row's *evidentiary basis*, never the certainty of the verdict. A relation
row backed by an extracted candidate is annotated `(sources: N, extraction conf:
C)` — distinct-source count (a multi-source trust signal; `tools/corroboration.py`
reports the full view) and the LLM's source→fact **extraction** confidence (a
candidate-stage trust signal, NOT a probability on the verification) — plus
`[stale: source missing]` when a backing source has vanished and the fact should
be re-verified. Each backing source path is listed beneath the row (`    ←
<source>`). A relation row with **no** extraction backing is marked `[no
extraction backing]` — today `accepted.dl` is a 1:1 projection of the candidate
table and no rule derives relation atoms, so this only arises when the two are
out of sync (recompile via `/factlog check`); it would also cover a future
rule-derived relation. Non-relation predicates (path/count/policy) are computed
and carry no extraction confidence by construction. The verdict stays binary in
every case. For an out-of-band trace (any fact, full or partial triple, all
statuses), use `factlog provenance <subject> [relation] [object]`.

A verified-negative relation query may additionally carry an informational
`note: ... (possible predicate mismatch): ...` line (#189). It appears **only**
when the queried subject is an accepted entity that has **no** fact under the
queried relation yet **does** carry fact(s) under other relations — so a user can
tell a *predicate mismatch* ("I asked the wrong relation") from an *honest
absence* ("there really is no such fact"). It is purely observational: the
verdict, routing, storage, and provenance are unchanged, and no hint is emitted
for a genuine absence (subject has zero facts) or an object mismatch (subject has
the relation, just not that object). Show it verbatim beneath the verdict block.

### Step 3b — Wiki exploration (UNVERIFIED)

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/ask_router.py" wiki "<question>" --reason "<why>" --target "$FACTLOG_ROOT"
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
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/ask_router.py" note "<question>" --target "$FACTLOG_ROOT"
```

---

## Extraction & translation criteria (references)

All four reference documents are authoritative constraints — read them before
any LLM extraction or query-translation step:

- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-fact.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-datalog.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/natural-language-to-policy.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/self-correct.md`
