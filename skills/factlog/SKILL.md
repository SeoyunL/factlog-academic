---
name: factlog
description: >-
  Keep a markdown knowledge base honest: extract source-backed candidate facts
  from sources/, compile confirmed facts, run a deterministic Datalog/wirelog
  logic check, and attempt gated self-correction. Use when the user asks to
  "sync facts", "check the wiki", "run factlog", "verify facts", or update a
  knowledge base from its source documents.
argument-hint: "setup | add | sync | query | check | repair | ask | ingest | zotero-import | openalex-* | arxiv-* | pubmed-* | review | accept | reject | amend | provenance | vocab | search | sources | status | export | eject | ignore | use | lang | where"
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
`/plugin install factlog@seoyunl`.

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
3. Scaffolds `--target` (`sources/`, `facts/`, `policy/`, etc.) **and makes it the
   active KB** — replacing the previous one if a different KB was active.
4. Re-runs `doctor` and prints a concise summary of what was done and what (if
   anything) the user must do next.

`setup` is idempotent when re-run on the **same** `--target`. Re-running it on a
**different** `--target` is not a no-op: it moves the active KB, and every later
`ingest`/`sync`/`accept`/`reject`/`amend` follows it. Note `--target` defaults to
`~/wiki`, so a bare `factlog setup` in a session where the user works in another
KB will retarget them.

**Active-KB contract (do not skip).** The active KB decides which KB every
mutating command writes to, so a silent move is a correctness bug, not a cosmetic
one (#210):

- If `setup`'s summary contains **`CHANGED active KB: <old> -> <new>`**, you MUST
  relay that line to the user verbatim. It is also printed to stderr. Do not bury
  it in a "setup complete" message — the user's `accept`/`reject`/`sync` now go
  somewhere else than before.
- `init` does **not** move an existing active KB. It scaffolds the target and
  leaves the active KB alone (saying so on stdout and stderr). To switch, run
  `factlog use <kb>` — or `init --activate`, which adopts the target and likewise
  announces what it displaced.
- Never create a scratch or example KB in a user's session without checking
  `factlog where` afterwards. Scaffolding a KB must not silently retarget their
  work.

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
is unavailable (or `< 1.0.3`) the logic check is skipped with a note; facts are
still merged and compiled, but the engine verification did **not** run, so
`finalize` **exits 3** — distinct from a verified pass (`0`) so automation never
mistakes an unverified compile for a checked one. Install `pyrewire>=1.0.3` and
run `/factlog check` to verify, or pass `--allow-unverified` to accept the
unverified compile and keep the exit code `0`. That flag tolerates the **engine
being absent, not a policy defect**: if `policy/logic-policy.md` defines rules
that did not compile, the policy is silently not applied, so `finalize` exits
non-zero (`3`) regardless of `--allow-unverified` — fix the policy rather than
suppress the signal.

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
first-class entity. The guarantee is about the RELATION: no edge is drawn ALONG an
attribute relation, so no path reaches a value by way of one. A value that appears
nowhere else is therefore kept OUT of the entity set — not listed, not a path node,
not a `count` subject — and the query translator won't mistake a date for an entity.
The value is still an ENTITY if it appears as a subject anywhere, so it can be named in
a query — but being an entity is not the same as being on a path. A path may START at
it only if it is the subject of a NON-attribute relation (its only source of an
outgoing edge), may END at it only if a NON-attribute relation has it as its object
(its only source of an incoming edge), and RUNS THROUGH it only when both hold. They remain fully verifiable as relation
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
form. A **bare `2030`** is not one: with no separator and no `date(…)` wrapper it
is indistinguishable from a number, so it stays untyped. When you only know the
year, write the compound term `date(2030)` — it parses to `20300101`, the same
year-start default as `2030.1`. Typed source objects may also be emitted as
compact compound terms when that preserves structure better than prose strings:
`date(2030)`, `date(2030,1)`, `date(2030,1,15)`, `number(2.5)`, `ordinal(3)`, or
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
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" "${CLAUDE_PLUGIN_ROOT}/tools/source_coverage.py" --wiki "$FACTLOG_ROOT"
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

### Output order — evidence first (applies to Steps 3a and 3b)

The engine's answer exists the moment `render`/`wiki` returns (sub-second);
do not hold it hostage to your own prose. As soon as the command returns:

1. **First**, output the verbatim block (`VERIFIED — engine` /
   `UNVERIFIED — wiki exploration`, including any trailing `note:` /
   `WARNING:` lines) as user-facing text — before any further analysis,
   synthesis, or tool call.
2. **Then**, add your synthesis as a separate paragraph *after* the block: a
   short gloss of 2–4 sentences in the chosen output language (see "Output
   language" above). The verbatim block is the evidence; the gloss only
   explains what it means. Do not restate, reformat, translate, or summarise
   the block's rows.

Never buffer the block behind your full analysis into one final message — the
user must see the engine's output at the moment it exists, with the
comprehensive judgment following it.

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

## Academic source imports — zotero / openalex / arxiv / pubmed

**Purpose:** pull bibliographic records from Zotero, OpenAlex, arXiv, and PubMed
into `sources/` so the normal pipeline can extract facts from them. These four
integrations are **input adapters, not fact stores.**

**What an import produces:** one `sources/<slug>.md` original per record, plus a
provenance ledger under `<kb>/source-provenance/`. That original is a *source*,
not a fact — it still has to go through `sync → review → accept` before anything
it says reaches `facts/accepted.dl`. Never report an imported record as a
verified fact.

**Two invariants hold across all four** (they are why these commands are safe to
re-run):

1. **A user's own file is never clobbered, and an imported `sources/<slug>.md`
   bibliographic record is never rewritten.** Note the scope: this is not a claim
   that nothing is ever written under `sources/`. `zotero-import --pdf` places PDF
   attachments there, and `--annotations` writes `sources/<stem>-notes.md` and
   **overwrites it on a later run when the highlights changed** — but only when
   the file is tool-owned (marked `source_kind: annotations` in its front matter);
   a file that is not ours is skipped, never clobbered.
2. **Import is idempotent**, so re-importing an identity key already in the KB
   (`zotero_key` / `openalex_id` / `arxiv_id` / `pmid`) is skipped rather than
   duplicated.

The stronger promise — *every* write lands in the provenance ledger or the
check-log, and `sources/` is not touched at all — belongs specifically to the
refresh, acknowledge, and backfill families described below, not to the import
commands.

Every one of these commands accepts `--target <kb>` (defaults to the active KB —
resolve it as described in "Resolve the active KB root first") and most accept
`--dry-run` (report what would be imported, write nothing) and `--porcelain`
(tab-separated machine-readable output).

Long-form documentation lives in the repo, not in this skill: `docs/academic-import.md`
for the shared contract, and `docs/zotero-import.md` / `docs/openalex.md` /
`docs/arxiv.md` / `docs/pubmed.md` per integration.

### Selectors and required arguments

**Every entry-point command below takes a flag selector. None of them accept a
bare positional string.** Where a row says *exactly one of*, the flags are an
argparse mutually-exclusive **required** group: omitting all of them is an error
(`error: one of the arguments --collection --tag --items is required`), and
passing two is also an error.

| Command | Required selector | Notes |
| --- | --- | --- |
| `zotero-import` | **exactly one of** `--collection NAME` \| `--tag TAG` \| `--items KEY,KEY` | `--pdf` also fetches PDF attachments; `--annotations` also imports highlights/notes into `sources/<stem>-notes.md` |
| `openalex-search` | `--query TEXT` | costs 10 credits per search; `--year`, `--type`, `--limit` (default 25, max 200), `--all` |
| `openalex-import` | **exactly one of** `--work-id W…` \| `--doi 10.…` | free |
| `openalex-cite` | `--for <source-slug>` | `--direction citing\|cited\|both` (default `citing`), `--limit`, `--auto-import` |
| `openalex-refresh` | *(none — whole KB)* | scope with `--older-than DAYS` (default 30; `0` re-checks everything); `--auto-update` |
| `openalex-acknowledge-retraction` | `--id W…` | one id only, no `--all`, no wildcard; `--yes` required off a terminal |
| `openalex-backfill-provenance` | *(none — whole KB)* | no network; `--dry-run` previews |
| `arxiv-import` | `--id 2311.09277` | repeatable, up to 100 ids per run; pin a version inline (`2311.09277v2`) — there is no `--version` flag |
| `arxiv-search` | `--query TEXT` | `--category` (repeatable), `--year`, `--limit`, `--sort submitted\|updated\|relevance`, `--all`, `--show-query` |
| `arxiv-check-versions` | *(none — whole KB)* | `--older-than DAYS`, `--auto-update` |
| `arxiv-acknowledge-withdrawal` | `--id 2311.09277` | **base id only** — a version pin is rejected, since identity is the base id; `--yes` off a terminal |
| `arxiv-backfill-provenance` | *(none — whole KB)* | no network; `--dry-run` previews |
| `pubmed-import` | `--pmid 32738937` | repeatable, up to 200 ids per run; a `pmid:` prefix is accepted |
| `pubmed-search` | `--query TEXT` | PubMed syntax; `--mesh` (repeatable), `--year`, `--limit`, `--all`, `--show-query` |
| `pubmed-mesh` | `--for <source-slug>` | proposal-only — writes nothing |
| `pubmed-refresh` | *(none — whole KB)* | `--older-than DAYS`, `--only-flagged`, `--auto-update`, `--dry-run` |
| `pubmed-acknowledge-retraction` | `--id 32738937` | one PMID only; `--yes` off a terminal |
| `pubmed-backfill-provenance` | *(none — whole KB)* | no network, no NCBI email needed; `--dry-run` previews |

`--for` takes a **factlog source slug** — the KB's own name for an already-imported
source, not a paper title and not an upstream id. Get it from `factlog sources`,
**but do not paste that output verbatim.** `factlog sources` prints a KB-relative
path (`sources/demo.md`) while the resolver assembles `<kb>/sources/<slug>.md`, so
passing `--for sources/demo.md` looks for `<kb>/sources/sources/demo.md` and fails
with `no source sources/demo.md in <kb>/sources`. **The slug is the bare file stem:
strip the `sources/` prefix.** The `.md` suffix is optional — both `--for demo` and
`--for demo.md` resolve. This holds identically for `openalex-cite` and
`pubmed-mesh`.

### Handling a bare natural-language argument (do not guess)

A human types `/factlog zotero-import "protein folding"`. That string carries no
selector, and **you cannot tell from the string alone** whether it names a Zotero
collection, a tag, or a search phrase. The three read different item sets, so a
guess silently imports the wrong library slice — and because import is idempotent
and non-destructive, nothing downstream will flag the mistake.

**Rule: never infer the selector. Search first, then ask the human.**

1. **Check deterministically what the string could be.** For Zotero, run the
   collection reading under `--dry-run`:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" -m factlog zotero-import --collection "protein folding" --dry-run --target "$FACTLOG_ROOT"
   ```

   If it is not a collection the client fails (exit 1) with
   `collection 'protein folding' not found. Available collections: …`, and that
   error **names the collections it knows** — the closest thing to a lookup table
   the CLI offers. Read the names it prints; do not assume the list is complete,
   and never assume a name absent from it does not exist.

   **All three selectors are validated the same way (#453): a value the library
   does not hold is a hard error (exit 1), not a silent import of nothing.**

   - `--collection` — an unknown name exits 1 and names the collections that do
     exist (up to 20, then `... (N more)`). Two further failures list nothing:
     `collection 'x' is ambiguous (N matches)` when several collections share the
     name, and `... is ambiguous by case` when they differ only in case. Neither
     tells you which to pick, so there is no deterministic next step — take the
     ambiguity to the human and let them name the collection.
   - `--tag` — an unknown tag exits 1 and names the available tags under the same
     20-name cap. Under `--porcelain` a hard failure writes nothing to stdout and
     still exits 1, so a typo can never read back as a successful "imported 0".
   - `--items` — an unknown key exits 1, naming every key that did not resolve.
     `--items` is all-or-nothing: one bad key stops the whole batch, so the valid
     keys alongside it are not imported either.

   Read a *successful* run carefully, though. A `--tag` that exits 0 with 0 items
   means the tag exists and is currently empty — a real answer, not a lookup
   failure; do not report it as "tag not found". An `--items` key naming a PDF
   attachment or a note likewise resolves to 0 items at exit 0: the key exists but
   is not a bibliographic item (`1 requested` alongside `0 item(s)`). Neither is
   an error.

   A selector that resolves proves only that *that* selector matches. A
   successful `--collection` dry-run never proves the string was not *also* a tag.
   Resolving is not disambiguating, **which is precisely why the next step is a
   question and not a guess.**

2. **Ask the human which selector they meant**, showing what you found — the
   collection names the CLI reported, and whether the string matched one. One
   focused question; do not offer to "try both".

3. **Only then run the real import**, with the selector the human named.

The same rule generalises to the other three:

- `openalex-import` — a string shaped like `W2741809807` is a `--work-id`; one
  shaped like `10.1007/s10462-023-10448-w` is a `--doi`. **Anything else is not
  an identifier at all**, so it is a search phrase, not an import target: run
  `openalex-search --query "<text>" --dry-run` and ask which result to import.
  Do not silently upgrade a search into an import. **This is the one
  disambiguation step that costs something:** OpenAlex has no `--show-query`
  equivalent, so the only way to look is to search, and a search costs 10 credits
  whether or not you import (`--dry-run` does not make it free). Confirm with the
  human before probing OpenAlex on a string that may not even be a query.
- `arxiv-import` — `--id` takes arXiv ids (`2311.09277`, optionally `vN`). Free
  text is not an id: use `arxiv-search --query "<text>" --show-query` (prints the
  exact query without spending a request) or `--dry-run`, then ask.
- `pubmed-import` — `--pmid` takes numeric PMIDs. Free text is not a PMID: use
  `pubmed-search --query "<text>" --show-query` / `--dry-run`, then ask.
- `openalex-cite --for` / `pubmed-mesh --for` — a paper *title* is not a slug.
  Resolve it against `factlog sources` and ask if more than one source matches.

**Never supply a bulk or confirmation flag the human did not ask for.** `--all`
(search: import every result without prompting), `--auto-import` (`openalex-cite`),
`--auto-update` (refresh family), and `--yes` (acknowledge family) all exist to
serve non-interactive runs, not to save the human a question. Adding one on your
own initiative converts "show me what's there" into a write.

### Refresh, acknowledgement, and backfill

`openalex-refresh` / `arxiv-check-versions` / `pubmed-refresh` re-query upstream
and **report** drift (a changed DOI, journal, work type, or arXiv version). With
`--auto-update` they record the changed metadata in the provenance ledger. They
never touch `sources/*.md`.

**A retraction or withdrawal is never absorbed automatically** — under both modes
it is surfaced for human review, and it re-appears on every run until a human
closes it with `openalex-acknowledge-retraction` / `arxiv-acknowledge-withdrawal`
/ `pubmed-acknowledge-retraction`. Those commands take **one** `--id`, by design:
the blast radius is a single record chosen by a person. They may *record* a
retraction non-interactively with `--yes`, but **clearing** one silences a
recorded signal and is refused unless a human confirms it at a terminal. Relay
the retraction/withdrawal finding to the human; do not acknowledge on their
behalf.

The `*-backfill-provenance` commands give ledger-less records (imported before
ledgers existed) the ledger their front matter implies, so a retraction can be
acknowledged at all. They use no network and never touch `sources/*.md`;
`--dry-run` names the ids that would be written and the ids refused.

### Credentials and configuration

Each integration is an optional extra, and settings resolve per integration:

> **explicit path  >  `<kb>/policy/<name>-config.toml`  >  `${XDG_CONFIG_HOME:-~/.config}/factlog/<name>.toml`  >  defaults**

| Integration | Extra (install this) | Dependency it pulls | Auth | Human prerequisite |
| --- | --- | --- | --- | --- |
| Zotero | `zotero` | `pyzotero` | none (Local API) | Zotero 7 running, Settings → Advanced → "Allow other applications…" enabled |
| OpenAlex | `openalex` | `httpx` | none | `email` optional (courtesy identification) |
| arXiv | `arxiv` | `httpx`, `feedparser` | none | `email` optional |
| PubMed | `pubmed` | `httpx` | none | **`email` required**, `api_key` recommended |

The **extra** is what goes in brackets — `pip install 'factlog-academic[zotero]'`.
The dependency column is what that extra installs, not an installable extra name:
`pip install 'factlog-academic[pyzotero]'` fails.

**Secrets never come from the KB.** A KB is often its own version-controlled
repo, so Zotero's `web_api_key` and PubMed's `api_key` are read only from the
user-level XDG file or an explicitly passed path — an `api_key` in
`<kb>/policy/*-config.toml` is ignored. `NCBI_API_KEY` in the environment
overrides the key from any file. OpenAlex's `email` is not a credential and is
safe in a KB policy file.

---

## KB inspection and curation commands

These read or edit the KB directly. None of them run the engine's logic check,
so none of them produce verified answers on their own — `/factlog check` and
`/factlog ask` remain the only sources of engine-backed evidence. All accept
`--target <kb>` (default: the active KB).

### Inspect

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" -m factlog status --target "$FACTLOG_ROOT"
```

- `factlog status` — one-screen KB summary: engine version, fact counts,
  vocabulary sizes, source counts, conflicts, and whether `logic_report.txt` is
  present. Run this first when you need to orient in an unfamiliar KB.
- `factlog sources` — every registered source with its original, its conversion
  (if any), and its fact count. **This is where source slugs come from** for
  `openalex-cite --for` and `pubmed-mesh --for`.
- `factlog search <term>` — case-insensitive substring match across
  subject/relation/object. Quote a term containing spaces. Use it to find the
  exact spelling of an entity before writing a query.
- `factlog vocab [--entities] [--relations] [--all]` — entity and relation names
  with counts. It reports **engine facts** by default; `--all` widens it to
  non-engine names (`candidate`/`needs_review`/`superseded`). When a
  `/factlog query` draft fails validation on an unknown token, this is the
  authoritative list of what the engine will accept.
- `factlog provenance <SUBJECT> [RELATION] [OBJECT]` (alias `factlog trace`) —
  trace a fact to its source paths, status, confidence, note, and staleness.
  `-` wildcards a position. Unlike `/factlog ask`, it covers **all statuses**,
  not just engine input, so use it for out-of-band audits of a candidate.

### Curate the pending queue

The review commands are the supported alternative to hand-editing
`facts/candidates.csv` — they update the backing rows and recompile
`facts/accepted.dl`, and their decisions survive a re-merge.

```bash
"${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh" -m factlog review --target "$FACTLOG_ROOT"
```

- `factlog review [--status candidate|needs_review]` — list facts awaiting a
  human decision (both statuses by default). Show this queue to the human; do
  not decide it for them.
- `factlog accept <SUBJECT> [RELATION [OBJECT]]` — set matching *pending* rows to
  `accepted`. `-` wildcards a position, `--dry-run` prints the plan.
- `factlog reject <SUBJECT> [RELATION [OBJECT]]` — set matching pending rows to
  `superseded` (same wildcard and `--dry-run` support).
- `factlog amend <subject> <relation> <object> [--set-subject X] [--set-relation Y]
  [--set-object Z] [--set-note TEXT] [--accept] [--dry-run]` — correct a fact's
  value durably: it rewrites `facts/candidates.csv` **and** the backing
  `runs/*.json`, so a re-merge does not resurrect the old value.

Because `accept`/`reject` match a *prefix* with `-` wildcards, a two-term
invocation can match far more rows than intended. Run `--dry-run` first and show
the human the planned changes whenever the triple is not fully specified.

### Remove, exclude, export

- `factlog eject (sources... | --fact S R O | --orphans)` — the inverse of
  `ingest`. **At least one of the three is required**: with none of them the
  command refuses (`factlog eject: nothing to eject (give a source, --orphans, or
  --fact S R O)`, exit 2). Naming a source removes it and its facts; `--fact`
  retires one fact by triple (repeatable) and leaves the source in place;
  `--orphans` auto-detects
  every orphaned source (a conversion whose original is gone, or a cited source
  with no file). By default retired rows are marked `superseded` and the user's
  original under `sources/` is left alone — **`--purge` deletes the rows outright
  and `--delete-original` deletes the user's file**, so use `--dry-run` and get
  explicit human agreement before either.
- `factlog ignore [patterns...] [--remove]` — manage `policy/sync-ignore.md`, the
  glob list `/factlog sync` skips during extraction. With no arguments it lists
  the current patterns.
- `factlog export (--bibtex | --csl) [-o FILE]` — emit source provenance as
  BibTeX or CSL-JSON (for Pandoc/Zotero/Word). **Exactly one of the two formats
  is required** — there is no default, and omitting both fails with
  `factlog export: specify exactly one format (--bibtex or --csl)` (exit 2).
  Writes to stdout unless `--output` names a file.
- `factlog use <kb> [--lang CODE]` — move the active KB (and optionally set the
  narration language in the same step). This is the explicit way to switch KBs;
  it is what the Active-KB contract above tells you to prefer over re-running
  `setup` on a different `--target`.

---

## Extraction & translation criteria (references)

All four reference documents are authoritative constraints — read them before
any LLM extraction or query-translation step:

- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-fact.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/text-to-datalog.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/natural-language-to-policy.md`
- `${CLAUDE_PLUGIN_ROOT}/skills/factlog/references/self-correct.md`
