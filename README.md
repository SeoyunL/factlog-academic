# factlog-academic

> 🌐 **English** | [한국어](README.ko.md)

> facts + logic — a Claude Code plugin that turns markdown sources into **verifiable, source-backed facts**.
> The LLM extracts; a deterministic Datalog/wirelog engine verifies.

**factlog-academic** is the academic-research distribution of
[factlog](https://github.com/semantic-reasoning/factlog). It does everything factlog
does, and adds commands that pull scholarly bibliography directly into your knowledge
base: [Zotero](#importing-zotero-bibliography-factlog-zotero-import),
[OpenAlex](#importing-openalex-bibliography-factlog-openalex-),
[arXiv](#importing-arxiv-preprints-factlog-arxiv-), and
[PubMed](#importing-pubmed-records-factlog-pubmed-).

![License: Apache-2.0](https://img.shields.io/badge/license-Apache--2.0-blue)
![Python: 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)
![Claude Code plugin](https://img.shields.io/badge/Claude%20Code-plugin-8A2BE2)

## Contents

- [Why you'd want this](#why-youd-want-this)
- [What it is](#what-it-is)
- [How it works](#how-it-works)
- [Requirements](#requirements) · [Install](#install)
- [Quickstart](#quickstart)
- [Versioning your KB](#versioning-your-kb)
- [Concepts](#concepts) — statuses, the gate, ledger vs front matter, the active KB
- [Source file formats](#source-file-formats) and the full command/policy reference
- [Usage](#usage)
- [Determinism & limitations](#determinism--limitations)
- [License](#license)

## Why you'd want this

Ask an LLM to organize your literature and it does two things you won't notice:
it **invents** what isn't there (a plausible citation, a confident number), and it
**silently drops** what is (a paper that never comes back in the results). Both
failures are quiet — the answer looks complete either way.

factlog makes that silent loss *structurally* hard. The LLM only ever proposes; a
deterministic engine decides what is true; every accepted fact carries its source;
and a question with no verified answer says so instead of guessing. Two real cases
from actual knowledge bases:

- A query for `IL-10` returned **3 rows out of 4**. The fourth was filed as
  `기타(IL-10)` and hid behind a different string — a silent omission
  `tools/value_audit.py` now flags.
- A query for `observational` study designs returned **6 out of 14**. The other
  8 were filed as a subtype (e.g. `cohort`) the engine did not know counted as
  observational until `policy/value-hierarchy.md` declared it.

In a plain notes wiki, or a chat where you paste PDFs into an LLM, you would never
learn those rows were missing.

**Who it's for:** researchers running literature or systematic reviews — Zotero
users, grad students, anyone already in Claude Code who needs their document
claims to be *checkable*, not merely plausible.

### See it in 30 seconds

A verified answer names its source, and there is no answer without one:

```text
$ /factlog ask "who develops Claude Code?"
VERIFIED — engine
query: relation("Claude Code", "developed_by", D)?
rows: 1
  - Claude Code, developed_by, Anthropic (sources: 1, extraction conf: 0.99)
    ← sources/example.md#what-is-claude-code
```

Ask something the sources do not support and you get a *verified negative*, not a
confident guess — and it even points out where a real fact might be hiding under
a different relation:

```text
$ /factlog ask "who develops factlog?"
VERIFIED — engine
query: relation("factlog", "developed_by", D)?
rows: 0
no such fact (verified negative)
note: no verified 'developed_by' for 'factlog', but 'factlog' has 3 fact(s) under other relations (possible predicate mismatch): is_a, performs
```

Assert two contradictory values for a fact that can hold only one, and the KB
refuses to compile until a human resolves it — instead of letting both sit quietly
side by side:

```text
$ factlog status
  conflicts:  1 (over 1 single-valued relation(s))  ⚠ run tools/check_conflicts.py for the resolution steps

$ python3 tools/check_conflicts.py --wiki ~/wiki
check_conflicts: 1 conflict(s) found
  CONFLICT: single-valued 'developed_by' on 'Claude Code' has 2 values: Anthropic, OpenAI
  Resolve with the human gate, not by hand-editing facts/candidates.csv:
    factlog eject --fact SUBJECT RELATION OBJECT   retire an accepted row
    factlog amend SUBJECT RELATION OBJECT --set-object NEW   correct a value
  ...
```

### vs. just asking an LLM

|  | ChatGPT / NotebookLM / Elicit on your PDFs | factlog |
|---|---|---|
| **Invents a fact or citation** | can — fluently and confidently | won't — the LLM only proposes; a deterministic engine decides |
| **Silently drops a source** | can — a missing paper reads as "no result" | won't — a verified answer names its source, or says "verified negative" |
| **Contradictions** | coexist unnoticed | a single-valued conflict blocks compile until a human resolves it |
| **Human sign-off** | none | every fact passes an explicit `accept` gate before it becomes engine input |

Being honest about limits is part of the design, not an afterthought: this README
spells out what the audits *do not* catch (see
[Auditing the value vocabulary](#auditing-the-value-vocabulary-toolsvalue_auditpy))
and where the model is only *guided*, not forced
([Determinism & limitations](#determinism--limitations)). A clean report is
evidence, not a proof of completeness — and factlog says so out loud.

## What it is

factlog-academic is a [Claude Code](https://code.claude.com) **plugin** that installs the
`/factlog` **skill** — a prompt that keeps a markdown knowledge base honest. Throughout
this README, "the plugin" is what you install and "the skill" is what it runs. It follows
one rule:

> The agent does not draw conclusions. The agent produces files and calls a CLI. The CLI returns a verifiable report.

- **The LLM (Claude, in-session) extracts** candidate facts from your `sources/`, drafts Datalog queries from natural-language questions, and attempts limited self-correction.
- **A deterministic engine (wirelog via [pyrewire](https://github.com/semantic-reasoning/PyreWire)) verifies** them — compiling confirmed facts, running the logic check, and surfacing policy findings, conflicts, and `review_required` items.

Anything the model produces is a *candidate* until the engine and a human confirm it.

## How it works

![How factlog works: Claude proposes, the engine verifies, a human confirms](docs/how-it-works.svg)

<details>
<summary>Text version</summary>

```
sources/        →  Claude extracts        →  facts/candidates.csv, pages/, decisions/
candidates       →  human review           →  confirmed facts
confirmed        →  compile (deterministic) →  facts/accepted.dl
questions        →  Claude drafts query     →  facts/query.dl
accepted + query →  wirelog logic check     →  facts/logic_report.txt   ← the verifiable report
review_required  →  Claude repairs (gated)  →  decisions/correction_trace.md
```

</details>

## Requirements

- Python **3.11+** (required by the engine dependency `pyrewire`)
- **pyrewire 1.0.3+** (`pip install -r requirements.txt`)
- Claude Code CLI

## Install

factlog-academic is a **Claude Code plugin**. Install it from this repo's marketplace in a Claude Code session:

```
/plugin marketplace add https://github.com/SeoyunL/factlog-academic
/plugin install factlog@seoyunl
/reload-plugins
/factlog setup                     # one-shot: deps + doctor + init, in-session
```

> Install from **this** repo, not from upstream `semantic-reasoning/factlog`. The
> upstream plugin ships none of the bibliography commands — `factlog zotero-import`,
> `factlog openalex-*`, `factlog arxiv-*`, and `factlog pubmed-*` exist only here.

Run these commands **one line at a time**. If you paste multiple plugin commands
at once, Claude Code may try to process the marketplace registration and install
out of order.

After a successful install, the new `/factlog ...` commands may not be loaded in
the current session yet. Run `/reload-plugins` after `/plugin install`, then run
`/factlog setup`.


### Local install (development)

To develop against a local clone, register the working tree as the marketplace instead:

```
/plugin marketplace add ~/git/factlog-academic
/plugin install factlog@seoyunl
/reload-plugins
/factlog setup
```

### What `/factlog setup` does

`setup` collapses the previously-separate post-install steps into a single command. Equivalently, by hand:

```bash
pip install -r ~/git/factlog-academic/requirements.txt   # pyrewire>=1.0.3,<2.0
python3 -m factlog doctor          # checks Python 3.11+ and pyrewire
python3 -m factlog init --target ~/wiki   # scaffold the KB layout
python3 -m factlog use ~/wiki      # make it the active KB
```

The `use` line matters: `init` only adopts the new KB when you have no active KB
yet. If you already had one, skipping `use` leaves the old KB active and the new
one merely scaffolded. (`init --target ~/wiki --activate` does both in one step.)

### Windows Python executable

On Windows, the `python3` command can point to the Microsoft Store stub instead
of a real Python executable. In that state, `python` or `py` may work while the
plugin's bundled scripts fail.

Check these first:

```powershell
python3 --version
python --version
py -0p
```

If `python3 --version` only prints `Python`, fails, or opens Microsoft Store,
tell factlog which Python to use. For a venv:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e <path-to-factlog-repo>
$env:FACTLOG_PYTHON = (Resolve-Path .\.venv\Scripts\python.exe).Path
```

The plugin hooks and skill commands use
`${CLAUDE_PLUGIN_ROOT}/tools/factlog_python.sh`, which resolves Python 3.11+ in
this order: `$FACTLOG_PYTHON`, `python3`, `python`, then `py`.

If your Python is externally managed (PEP 668), pip will refuse to install into it; `setup` prints venv guidance instead of forcing the install. Create and activate a venv, then re-run `setup`:

```bash
python3 -m venv ~/.factlog-venv && source ~/.factlog-venv/bin/activate
python3 -m factlog setup --target ~/wiki
```

## Quickstart

From an empty folder to a verified, source-backed answer in one session. (Longer
walk-throughs: [examples/sample-kb](examples/sample-kb/README.md) and
[docs/getting-started-verify.md](docs/getting-started-verify.md).)

```bash
factlog init --target ~/wiki      # scaffold the KB (sources/, facts/, policy/, …)
cp notes.md ~/wiki/sources/       # drop documents in; binaries are converted on sync
```

Then, in a Claude Code session, extract candidate facts (this is the one LLM step):

```text
/factlog sync                     # Claude reads sources/ and writes candidate facts
```

Back in the terminal you are the gate — nothing becomes engine input until you accept it:

```text
$ factlog review
factlog review (KB: ~/wiki): 1 pending fact(s), 1 row(s)
  Acme / uses / FastAPI
    ← sources/notes.md  [candidate, conf 0.90]
  decide with: factlog accept <subject> <relation> <object>   (or: factlog reject ...)

$ factlog accept Acme uses FastAPI
factlog accept (KB: ~/wiki): 1 pending row(s) → accepted
  Acme / uses / FastAPI  [candidate → accepted]  ← sources/notes.md
factlog accept: 1 candidate row(s) → accepted, 1 runs/*.json row(s) updated; accepted.dl recompiled
factlog review: note — pages/ may be stale; run /factlog sync to regenerate them.
```

Now ask. The answer is the engine's, and it names its source:

```text
/factlog ask "what does Acme use?"
VERIFIED — engine
query: relation("Acme", "uses", O)?
rows: 1
  - Acme, uses, FastAPI (sources: 1, extraction conf: 0.90)
    ← sources/notes.md
```

That is the whole loop — **extract → review → accept → ask**. Everything below is
detail on each step.

## Versioning your KB

🔴 The single most important operational habit: **commit `runs/*.json` and
`facts/`.** Losing them loses data no re-run can recover.

A KB has **two** irreplaceable records, and they are not the same file:

- **`runs/*.json` holds the facts.** `facts/candidates.csv` is rebuilt from it on
  every merge, so losing `runs/` erases every fact that is not re-extracted.
- **`facts/candidates.csv` holds the human decisions.** `factlog accept` / `reject`
  write the `accepted` / `superseded` status **there and nowhere else**. Delete it
  and a re-merge quietly demotes every accepted fact back to `candidate`.

So if you version-control your KB:

| Path | Commit it? | Why |
|---|---|---|
| `sources/` | **yes** | your originals |
| `runs/*.json` | **yes** | the extracted facts themselves |
| `facts/` | **yes — never regenerate** | the human accept/reject decisions live only here |
| `policy/` | **yes** | the rules you wrote (logic policy, questions, typed relations, …) |
| `pages/`, `decisions/` | optional | regenerated on every merge |
| `runs/sources/` | no | text conversions, regenerable with `factlog ingest --scan` |
| `source-provenance/` | **yes** | per-source provenance sidecars from `zotero`/`openalex`/`arxiv`/`pubmed` import — a paper's ledger (DOI, retraction status, cross-ids) |
| `check-log/` | **yes** | when the tool last checked each arXiv/PubMed/OpenAlex paper; `--older-than` skips a paper based on this, so dropping it re-checks everything |
| `merge-candidates/` | **yes** | the pair ledger `merge_candidates` uses to preserve human decisions across a rebuild |

The last three appear only once you use the bibliography commands; a plain KB never
grows them.

`merge_candidates` now refuses any rebuild that would delete a fact a human has
ruled on, so a lost `runs/` can no longer erase a KB silently — but the data still
has to exist somewhere.

This also changes one habit: **deleting a `runs/*.json` to undo an extraction no
longer silently drops its accepted facts.** Retire them through the gate first —
`factlog eject --fact SUBJECT RELATION OBJECT` — or pass `--allow-delete` if the
loss is what you want. (`factlog reject` will not do it: it only retires rows that
are still pending.)

## Concepts

A few terms recur throughout. Definitions once, here.

**Fact statuses.** Every extracted fact carries a status. Only two are engine
input; the rest are stages before or after the human gate.

| Status | Meaning | Engine input? |
|---|---|---|
| `candidate` | extracted by the LLM, not yet reviewed | no |
| `needs_review` | extraction flagged it for a human decision | no |
| `accepted` | a human accepted it (`factlog accept`) | **yes** |
| `confirmed` | same tier as `accepted` — a pre-blessed fact (the diagram's word, and what a seeded KB uses) | **yes** |
| `superseded` | retired by a human (`factlog reject` / `eject`), kept for audit | no |

`accepted` and `confirmed` are **one tier** — the code treats both as engine input
(`ENGINE_STATUSES = {confirmed, accepted}`). `factlog accept` writes `accepted`;
`confirmed` is the equivalent spelling used by the how-it-works diagram and by
seeded sample data. Neither is more trusted than the other.

**The gate.** The human `accept`/`reject` step. `/factlog sync` only ever produces
*candidates*; a fact becomes engine input only after it passes the gate. This is
factlog's trust boundary — the model proposes, a human disposes.

**Active KB.** The KB that bare `factlog` subcommands target when you omit
`--target`, recorded by `factlog setup` / `factlog use`. See
[Active KB](#active-kb-target-the-set-up-kb-from-anywhere).

**Ledger vs front matter.** A bibliography import writes two things: the source's
YAML **front matter** (`sources/*.md`, human-editable) and a per-paper
**ledger** under `source-provenance/` (DOI, retraction status, cross-ids — the
record `--auto-update` may touch, never `sources/*.md`). The distinction matters
for the determinism boundary in each integration below.

**Callout levels.** Cautions below come in two levels, so the dangerous ones stand
apart from the merely good-to-know:

- 🔴 **Irreversible / data-loss** — can destroy data or is hard to undo: deleting
  `runs/*.json`, `factlog eject --purge`, `--delete-original`. Read before running.
- Plain **Note:** — a smaller caution or edge case (a suffixed citation key, a
  format that is not auto-converted). Good to know, not dangerous.

## Source file formats

`/factlog sync` extracts facts by reading each file under `sources/` **as text,
in-session**. The bundled engine (`merge_candidates.py`) tracks every file as a
source *path* but never parses contents — so a file is only ingested if its text
can be read during extraction. A binary original (e.g. `.docx`) therefore yields
no facts on its own.

| Format | Status | Notes |
|--------|--------|-------|
| `.md`, `.markdown`, `.txt` | **Directly supported** | UTF-8 text, read verbatim. This is what every extraction reference assumes. |
| Other UTF-8 text (`.rst`, `.org`, `.csv`, source code) | Supported as plain text | No special parsing; treated as raw text. |
| `.docx`, binary `.pdf`, `.odt`, `.epub` | **Auto-converted** | `factlog ingest` converts these to text via pandoc / textutil / pdftotext. |
| `.html`, `.htm`, `.xhtml`, `.rtf` | **Auto-converted** | Text-based containers: the bytes are text but the content is markup, so they are converted rather than read raw — the original is not an extraction input once a conversion exists. |
| `.hwpx` (Hancom OWPML) | **Auto-converted** | Built-in extractor (no external tool) — reads the zip's `Contents/section*.xml` text. |
| `.hwp` (legacy Hancom, HWP 5.x) | **Auto-converted** | Via `hwp5html` (pyhwp) → pandoc → markdown, tables preserved. Needs `pip install pyhwp` + pandoc; if absent, reported with a hint. |
| `.pptx` (PowerPoint) | **Auto-converted** | Built-in extractor (no external tool) — reads on-slide text from the zip's `ppt/slides/slideN.xml`, slides in order, one block per slide. Speaker notes are excluded; table cells flatten to one line per cell (row/column grouping not preserved). |
| `.xml`, `.svg`, `.xlsx`, images | **Not converted** | Markup or binary with no general converter — reported with a hint; extract the text you need by hand. `.xml`/`.svg` are still kept OUT of extraction so their tags are never read as prose. |

`factlog ingest` writes the converted text into the KB's **`runs/sources/`**
directory (alongside the other generated run artifacts) — **never into
`sources/`**, which stays the user's originals. A nested original mirrors its
subdirectory, and the conversion keeps the original's FULL name plus the
converter's extension: `sources/sub/report.pdf` → `runs/sources/sub/report.pdf.txt`
(pdftotext writes `.txt`; pandoc writes `.md`, e.g. `report.docx.md`). Same-stem
files in different folders never collide. The original is left
untouched and the conversion carries a provenance header (source, converter,
date). Both `sources/` and `runs/sources/` are valid source roots that
extraction reads.

> **Upgrading a pre-mirroring KB?** Flat conversions of nested originals may
> reappear as coverage gaps; re-run `factlog ingest --scan --force` (top-level
> sources are unaffected). See
> [docs/eject.md](docs/eject.md#flat-layout-migration).

```bash
factlog ingest report.docx --target ~/wiki   # → ~/wiki/runs/sources/report.docx.md (pandoc)
factlog ingest --scan --target ~/wiki        # auto-convert every binary under sources/
```

### Active KB (target the set-up KB from anywhere)

`factlog setup` (or `factlog use <kb>`) records the chosen KB as the **active
KB**, so the `factlog` subcommands (`ingest`/`sync`/`status`/…) target it from any
working directory — no `--target` needed. (The bundled `tools/*.py` scripts are split: some resolve the KB the same way, via
`--wiki`/`$FACTLOG_ROOT`/config, while several — the engine-compile/finalize/validate
family, e.g. `compile_facts.py`, `run_logic_check.py`, `finalize.py` — read only
`$FACTLOG_ROOT` or the cwd. A session exports `$FACTLOG_ROOT` once from
`factlog where --porcelain` so every tool agrees; the slash-command workflow does this
for you.)

`factlog init` (and `factlog setup`) scaffold at `--target` if given, else at
`$FACTLOG_ROOT`, else at `~/wiki` — it does **not** fall back to the active KB (re-scaffolding the KB you are
working in would be surprising), but it does respect a `$FACTLOG_ROOT` you set for the
session.

`factlog init` records the new KB as active too, but **only when no usable active KB is
set yet**. Once you have one, `init` scaffolds the new KB and leaves the active KB alone —
otherwise creating a scratch KB in another shell, a test, or an agent would
silently repoint `accept`/`reject`/`amend`/`sync` at it. Switch deliberately with
`factlog use <kb>`, or ask for it up front with `init --activate`.

```bash
factlog use ~/wiki        # make ~/wiki the active KB (recorded in config)
factlog where             # show the active KB and how it was resolved
factlog lang ko           # set the assistant narration language (engine reports/CLI output stay verbatim; omit CODE to print it)
factlog sources           # list registered sources (original, conversion, fact count)
factlog status            # KB state: facts by status, vocabulary, conflicts, logic freshness, engine
cd /anywhere && factlog ingest report.pdf   # → ~/wiki/runs/sources/report.pdf.txt
factlog eject report.pdf  # inverse of ingest: remove the conversion + retire its facts
factlog ignore drafts/*.md   # exclude sources from sync (re-extraction)
factlog provenance Acme uses FastAPI   # trace a fact to its source(s)
factlog export --bibtex   # cite your sources: BibTeX (or --csl for CSL-JSON)
```

`export` reads the YAML front matter of every `.md` under `sources/` **and** `runs/sources/`, at any depth — the same set `factlog sources` lists — and
emits one entry per source that carries a `title` or a `zotero_key`. A source it cannot
cite (no front matter, or front matter with neither field) is named on stderr rather
than dropped quietly — a citation list that silently loses a work is the failure this
KB exists to prevent. Two sources in different folders may share a filename; the second
gets a suffixed citation key, and that is reported too.

### Optional integration dependencies

Each bibliography integration needs one extra. Install it from **this repository**.

> The bare name `factlog` on PyPI belongs to an unrelated 2013 project ("File
> ACTivity LOGger"). Asking pip for that name installs **that** package — and since
> it has no such extras, pip merely warns and exits 0, so you get a success message,
> a package you never wanted, and none of the dependencies. Always install from the
> URL below (or from a clone).

```bash
# from a clone of this repo (what most people want)
pip install -e '.[zotero]'

# installed as a Claude Code plugin, with no clone (this variable is only set
# inside a Claude Code session; from a plain terminal, substitute the plugin path)
pip install -e "${CLAUDE_PLUGIN_ROOT}[zotero]"

# from anywhere, no checkout
pip install 'factlog-academic[zotero] @ git+https://github.com/SeoyunL/factlog-academic'
```

Swap `zotero` for `openalex`, `arxiv`, or `pubmed`. `pip install -e '.[zotero,arxiv]'`
installs several at once.

> **Upgrading from an earlier version?** The distribution used to be named
> `factlog`. Both names own the same `factlog` module *and* the same `factlog`
> command, so pip installs them side by side and uninstalling the old one deletes
> the shared command. Run `pip uninstall factlog` **and then reinstall** — or just
> run `factlog doctor`, which detects the state and says so.

### Importing Zotero bibliography (`factlog zotero-import`)

If you already manage your literature in Zotero, you can migrate collections,
tags, or individual items into factlog's `sources/` in one pass. factlog does not
replace Zotero — it is a verification layer on top of it: keep using Zotero, and
let factlog handle fact extraction, provenance tracing, and logic checking.
Migrated items are still **candidates** and pass the `sync → review → accept`
gate. The Zotero originals are never modified (read-only).

This needs Zotero 7's **Local API** (enable it under Settings → Advanced; it
listens on port 23119) and the `zotero` extra ([see above](#optional-integration-dependencies)).

```bash
factlog zotero-import --collection "Systematic Review"   # migrate a collection
factlog zotero-import --collection "Systematic Review" --dry-run   # preview the plan only
factlog zotero-import --tag "to-review"                  # by tag
factlog zotero-import --items "KH78JUPE,64DA4TQJ"        # individual items
```

Without `--target` the migration goes to the active KB, and `--porcelain` emits
machine-readable output for scripts. `--pdf` also fetches each item's PDF
attachments and converts their full text with the existing `ingest` path (the
original PDF is stored in `sources/`, so `.gitignore` your `*.pdf` if you
version-control the KB). `--annotations` migrates highlights and notes into
`sources/<stem>-notes.md` (still just sources — the candidates go through `sync`
plus a human gate). After migrating, run `/factlog sync` to extract candidate
facts.

```bash
factlog zotero-import --collection "Systematic Review" --pdf            # bibliography + PDF full text
factlog zotero-import --collection "Systematic Review" --annotations    # + highlights & notes
```

### The import determinism boundary (shared)

The three ledger-based imports — OpenAlex, arXiv, PubMed — obey the same three
rules. Each integration below lists only where it *differs*.

1. **Import has no ledger authority.** An imported record is a `candidate` until it
   passes the `sync → review → accept` gate; import never writes engine input.
2. **`--auto-update` touches only the ledger, never `sources/*.md`.** A `*-refresh`
   / `*-check-versions --auto-update` records a few metadata fields to the
   per-paper ledger under `source-provenance/`; your source files are never rewritten.
3. **A retraction or withdrawal is never absorbed automatically.** It is reported
   to a human on every run and closed only by an explicit `*-acknowledge-*`
   command — revoking a published result is the judgment a human must make, not
   the tool.

### Importing OpenAlex bibliography (`factlog openalex-*`)

You can search and import literature from the open bibliographic database
[OpenAlex](https://openalex.org), widen the citation graph by one hop, and
re-check the metadata of records you already imported. Imported items, like
Zotero's, are still **candidates** and pass the `sync → review → accept` gate.
This needs the `openalex` extra ([see above](#optional-integration-dependencies)), and OpenAlex is **unauthenticated**
— no API key or account.

```bash
factlog openalex-search --query "neurosymbolic AI" --year 2020-2025 --limit 50
factlog openalex-import --doi 10.1007/s10462-023-10448-w   # or --work-id W2741809807
factlog openalex-cite --for artur-d-avila-garcez-2023-neurosymbolic-ai-the-3rd-wave --direction citing
factlog openalex-refresh                                    # reports only; never writes the ledger
factlog openalex-acknowledge-retraction --id W2741809807
factlog openalex-backfill-provenance                        # gives a ledger to works that have only front matter
```

A search costs **10 credits** (out of roughly 1,000 a day), and it costs the same
no matter how many results you take back — being frugal with `--limit` saves
nothing, so ask for a generous count up front. A single-record fetch costs 0
credits, so `openalex-import` and `openalex-refresh` are effectively free.

**Determinism boundary — OpenAlex specifics** (the
[shared rules](#the-import-determinism-boundary-shared) apply):

- `--auto-update` writes only `doi` / `work_type` / `journal`.
- The retraction flag is **OpenAlex's opinion**, not a fact factlog asserts —
  OpenAlex flags some works PubMed does not, so the front-matter key is
  `openalex_is_retracted`, never a bare `retracted:`. Acknowledge with
  `factlog openalex-acknowledge-retraction --id <id>`.
- A front-matter-only work (imported before #84) has no ledger to close, so
  acknowledge refuses it and points to `factlog openalex-backfill-provenance`,
  which builds one from front matter (no network, never touches `sources/*.md`).
  Nothing is lost — every ledger field has a front-matter key — so no value has to
  be added by hand first.

Settings resolve in the order `<KB>/policy/openalex-config.toml` >
`~/.config/factlog/openalex.toml` > built-in defaults. Unlike Zotero, there is
**no secrets boundary**: `email` is not authentication but an identification
courtesy, so it is safe in a committed KB policy file (Zotero's `web_api_key` is
read only from a user-level file).

### Importing arXiv preprints (`factlog arxiv-*`)

You can import papers from the preprint repository [arXiv](https://arxiv.org) by
id, or search and import them, and check whether a record you imported is still
the latest version. Imported items are still **candidates** and pass the
`sync → review → accept` gate. This needs the `arxiv` extra ([see above](#optional-integration-dependencies)), and the
arXiv API is **unauthenticated** — no API key or account. There is no credit
budget either — instead factlog keeps to arXiv's recommended 3-second delay
between requests on its own (a courtesy that is not enforced).

```bash
factlog arxiv-import --id 2311.09277          # latest version
factlog arxiv-import --id 2311.09277v1        # a specific version, pinned inline in the id (there is no --version flag)
factlog arxiv-search --query "chain of thought" --category cs.CL --year 2020-2025
factlog arxiv-check-versions                  # reports only; --auto-update records to the ledger
factlog arxiv-acknowledge-withdrawal --id 2311.09277
factlog arxiv-backfill-provenance             # gives a ledger to papers that have only front matter
```

`--id` is repeatable, up to 100 per run.

**Where a search query lies quietly.** If you pass several words as-is, arXiv does
not read them as a phrase but matches them loosely — measured live, the unwrapped
query returns close to what its first word alone matches, many times more than the
phrase. So factlog wraps them for you and sends `all:"your words"`, and tells you
it wrapped. If you want loose matching, use a field prefix (`ti:`, `au:`, `abs:`)
or a boolean (`AND`/`OR`/`ANDNOT`) yourself. Your own double quotes also turn
wrapping off, but that is still a phrase search, not loose matching. A single-word
query is never wrapped. `--show-query` prints the exact query that would be sent,
without spending a request (`--dry-run` does search, but writes no files). arXiv
answers a nonexistent category, field, or year with `200 OK` and "0 results" —
which an operator reads as "no such literature exists" — so factlog validates the
values before sending the request.

**Determinism boundary — arXiv specifics** (the
[shared rules](#the-import-determinism-boundary-shared) apply):

- `--auto-update` writes only `version` / `last_updated` / `comment`.
- A **withdrawal** (`withdrawn_by`) is arXiv's own act, not a journal retraction
  (see the note below). Acknowledge with `factlog arxiv-acknowledge-withdrawal
  --id <id>`. `--yes` can *record* a withdrawal but never *clear* one — arXiv no
  longer reporting one is ambiguous (reversed? or the sentence could not be read?),
  so clearing needs a human at the prompt.
- Backfill needs the paper's real version in front matter. A front-matter-only
  paper is closable only if it carries `arxiv_version: <N>` — the one value with no
  ledger fallback, so you add it by hand (read `<N>` from
  `https://arxiv.org/abs/<id>`; factlog does not fetch it), after which
  `factlog arxiv-backfill-provenance` builds the ledger.
- A paper whose version cannot be compared is reported as **`no-version`**, not
  `unchanged` — nothing was compared, so it cannot be called unchanged. The fix
  depends on the cause, and sometimes there is none.

> arXiv's **withdrawal** is not the same as a journal's **retraction** (OpenAlex's
> `is_retracted`). The former is an act by the author or an arXiv administrator on
> a preprint; the latter is a journal revoking a published paper. The recorded
> agent is either `author` or `admin`.

Settings resolve in the order `<KB>/policy/arxiv-config.toml` >
`~/.config/factlog/arxiv.toml` > built-in defaults. Like OpenAlex, there is **no
secrets boundary**: `email` is not authentication but an identification courtesy
carried in the User-Agent.

### Importing PubMed records (`factlog pubmed-*`)

You can search and import biomedical records from [PubMed](https://pubmed.ncbi.nlm.nih.gov)
(NCBI E-utilities) by PMID, re-check whether a record's metadata or retraction
status has drifted, and turn a paper's PubMed MeSH terms into vocabulary
proposals. Imported items are still **candidates** and pass the
`sync → review → accept` gate. This needs the `pubmed` extra ([see above](#optional-integration-dependencies)).

**A contact email is required** — unlike OpenAlex and arXiv, any `pubmed-*` command
that sends an NCBI request (`pubmed-import`, `-search`, `-refresh`, `-mesh`,
`-acknowledge-retraction`) fails until you set `client.email`, because NCBI throttles or
blocks unidentified traffic. (`pubmed-backfill-provenance` sends nothing and needs no
email.) Put it in `~/.config/factlog/pubmed.toml` (or the KB's
`policy/pubmed-config.toml`):

```toml
[client]
email = "you@example.org"
```

```bash
factlog pubmed-search --query "immune checkpoint" --mesh "Neoplasms" --limit 25
factlog pubmed-import --pmid 16354850                # repeatable, up to 200 per run
factlog pubmed-refresh --older-than 30               # reports drift; --auto-update records identifier/journal
factlog pubmed-acknowledge-retraction --id 16354850
factlog pubmed-backfill-provenance --dry-run         # gives a ledger to front-matter-only papers
factlog pubmed-mesh --for <slug>                     # propose MeSH vocabulary from a paper's ledger PMID
```

**Determinism boundary — PubMed specifics** (the
[shared rules](#the-import-determinism-boundary-shared) apply):

- `--auto-update` writes only the identifier and journal fields.
- On retractions PubMed is the **authority** — where OpenAlex's `is_retracted` has
  documented false positives (#51), a PubMed retraction is trusted. It still
  surfaces every run until `factlog pubmed-acknowledge-retraction` records that a
  human saw it.
- A deleted PMID is flagged (never silently dropped); a merged PMID is reported
  (never silently rewritten).
- MeSH terms from PubMed and OpenAlex coexist, each source-scoped; PubMed's carry
  the major/minor distinction OpenAlex cannot supply reliably before ~2022 (#53).

An API key is optional for NCBI but, for factlog's batched requests, effectively
required — without one E-utilities throttles hard. It is read from the
`NCBI_API_KEY` environment variable or `~/.config/factlog/pubmed.toml`
(there is no `--config` flag on the CLI), and **deliberately never from a KB `policy/` file** (a KB
is often its own committed repo, so the secrets boundary keeps the credential out
of it). See `docs/pubmed.md` for the full walkthrough.

### Discovering the vocabulary (`factlog vocab`)

`ask` and `provenance` need exact entity/relation names. `factlog vocab` lists
them — the entity and relation names with usage counts — so you know what is
queryable:

```bash
factlog vocab              # entities + relations (engine facts)
factlog vocab --entities   # just entities
factlog vocab --relations  # just relations (tagged [attribute] / [single-valued] / [typed:<type>])
factlog vocab --all        # include non-engine names (candidate/needs_review/superseded)
```

Objects of declared attribute relations are literals, not entities, so they are
excluded from the entity list (same typing as `status`).

### Auditing the value vocabulary (`tools/value_audit.py`)

Relation names are curated by policy. **Values are not** — they arrive one
extraction at a time, and nothing notices when the same thing lands twice under
two strings. A real KB held both `IL-10` and `기타(IL-10)` as accepted facts, so
`relation(P, "염증지표", "IL-10")?` returned 3 rows out of 4. The fourth was
hiding behind a different string: a silent omission, which is the one failure
mode this KB exists to prevent.

```bash
python3 tools/value_audit.py --wiki ~/wiki           # report (always exits 0)
python3 tools/value_audit.py --wiki ~/wiki --strict  # exit non-zero on provable query leaks
```

It compares values only **within the same relation**, and every finding is a
rule rather than a similarity guess:

| Finding | Meaning |
|---|---|
| **split wrapper** | `기타(IL-10)` beside `IL-10` — one value filed twice. Queries are leaking now. |
| **wrapper value** | `기타(INFLA-score)` — not queryable by its own name. |
| **placeholder** | `기타`, `불명`, `N/A` — carries no information, hides what the source said. |
| **spelling duplicate** | Equal after folding case/space/punctuation (`IL-8` / `il 8`). A query leak — unless the relation is an **identity** (see below), where a collision across subjects means a possible duplicate *record* instead. |

**Identity relations (`policy/identity-relations.md`).** A title or a DOI names
exactly one paper; a publication year or a study type does not. Declare the former
here:

```markdown
# policy/identity-relations.md
제목
DOI
```

In an identity relation, two subjects sharing a folded value is probably two
records of one thing — a duplicate *record*, a different repair, and `--strict`
does not fail on it. Everywhere else, values are shared across subjects by design,
so a collision is one value split across two spellings: a query leak, which
`--strict` does fail on. With no declarations every relation is categorical, so a
collision is reported as a leak — noisy rather than silent, and the report tells
you which relation to declare.

Identity is declared, never inferred — and the audit does not guess which
relations belong here either. Deriving it from the data ("every value has one
subject") is self-defeating: one genuine duplicate record makes the relation
non-injective, which flips it to categorical, which makes duplicate records fail
the gate — the exact case the classification exists to spare. A two-row KB is also
injective by accident. Declare only relations whose value names exactly one
subject; **never a category many subjects share**, or you permanently exempt the
leaks this audit exists to catch.

`factlog init` scaffolds the file empty. An **existing KB has no such file**, so
every relation starts categorical and a title collision is reported as a leak —
create `policy/identity-relations.md` and declare your identity relations (for a
bibliography: the title and the DOI).

Nothing is merged automatically. Fix with `factlog amend <subject> <relation>
<object> --set-object <canonical>`, which rewrites the row durably (both
`candidates.csv` and the backing `runs/*.json`).

**What it does not catch.** The wrapper rules are deliberately narrow, so a clean
report is not a proof of completeness. Undetected forms include `others: X`,
`기타 X` (no parentheses), and `기타(X) 등`. Digits are never folded together
(`1.5` is not `15`), and `etc` is not treated as a wrapper word — `ETC (electron
transport chain)` is a real value.

`tools/entity_audit.py` is the neighbouring check: it looks for *entity*
fragmentation across the whole KB by a shared-token heuristic, so it is broader
and far noisier (2275 candidates on that same KB). Use `value_audit` when you
want precise, per-relation findings you can act on.

### Relation aliases (`policy/relation-aliases.md`)

Map a **surface** relation name to the **canonical** one, so facts written `게재연도` and
`발행년도` are treated as one relation `published_year`. Without this the engine sees two
unrelated relations and a query for one misses facts stored under the other (#213).

```
# policy/relation-aliases.md
- `게재연도` -> `published_year`
- `publication_year` -> `published_year`
```

One mapping per line, `raw` -> `canonical`. **Backticks are required around both names**
here — unlike the other policy files, where they are optional. A line with an arrow but
no backticks is a mapping you meant to make and mis-spelled, so it is reported as
malformed on stderr and skipped, not applied silently. The canonical name is the one you
declare in the other policy files; aliases are folded to it before those apply.

### Single-valued relations (`policy/single-valued.md`)

A relation listed here may hold **at most one object per subject**. This is what turns
a contradiction into an error rather than two facts sitting quietly side by side — the
thing a plain notes wiki cannot do for you.

```
# policy/single-valued.md
published_year
`연구 유형`
```

One relation name per line; `#` comments and `-` bullets are allowed; backtick-quote a
name containing spaces. A relation you do not list may hold many objects per subject,
which is the right default for `cites` or `mentions`.

If two distinct objects are asserted for the same (subject, single-valued relation) it
is reported as a `CONFLICT` and the KB refuses to compile until a human resolves it. You
see conflicts with `factlog status` (`conflicts: N`), with `tools/check_conflicts.py`
(which prints each one and the resolution steps), or with `/factlog check` inside Claude
Code. You resolve one with `factlog eject --fact SUBJECT RELATION OBJECT` to retire a
row, or `factlog amend SUBJECT RELATION OBJECT --set-object NEW` to correct one. Never by hand-editing
`facts/candidates.csv`: that bypasses the gate the KB is built around. And if the two
values are a supertype and its subtype, neither is wrong — see the next section.

### Value hierarchy (`policy/value-hierarchy.md`)

Two values of the same relation are unrelated strings unless you say otherwise.
A cohort study **is** an observational study, but without a declaration
`relation(P, "study_type", "observational")?` returns only the rows spelled
exactly `observational` and silently misses every row filed as `cohort` — a quiet
omission, which is the one failure mode this KB exists to prevent. In a real KB
that query returned 6 rows out of 14.

```markdown
# policy/value-hierarchy.md
- study_type: cohort ⊂ observational
- study_type: cross_sectional ⊂ observational
- target_disease: `emphysema` <: COPD
- 연구유형: 코호트연구 ⊂ 관찰연구          # non-ASCII names work the same
```

`<:` and `<` are ASCII spellings of `⊂`. Backtick-quote a value containing a
space, a `:` or a `<`. Ancestors are transitive (`a ⊂ b` and `b ⊂ c` means a
query for `c` also matches an `a` row). Names are matched after Unicode NFC
normalisation, so a policy file written on macOS still meets its facts.

**Contradiction detection.** A hierarchy also tells `check_conflicts` that a
supertype and its subtype are not a contradiction. With `연구유형` declared
single-valued, a paper carrying both `관찰연구` and `코호트연구` is being described at
two levels of precision, and both rows are true — so it is not reported, and
`finalize` does not stop. Genuine siblings still are: `관찰연구` and `실험연구` on one
paper remains a conflict, and so does a chain plus a sibling. Retiring a fact is a
human decision (`factlog eject --fact`), never something you should do to silence a
false alarm.

**Scope — where subsumption applies.** It is applied when a query's **object** is
matched, by all three of the gate, the evaluator and the logic report, so
`/factlog ask` and `/factlog check` cannot disagree about what a question means.
It does **not** rewrite facts: `accepted.dl` stays a 1:1 projection of the
accepted candidate rows, and every row keeps its own value and its own
provenance. It is **one-way** — asking for the narrow value never returns the
broad one. It does **not** apply to `factlog search`, `provenance`, `vocab`,
coverage, or conflict detection, which all still match values exactly.

The broad value does not need to appear in any fact — declaring it is enough for
it to become a queryable concept.

Mistakes are reported rather than left to do nothing quietly: a **cycle** is
dropped in full (keeping it would make subsumption mutual and break the one-way
contract), and a declaration naming a relation or value that no accepted fact
uses is surfaced in the logic report's warnings — a typo would otherwise leave
you believing the broad query now catches the narrow rows when it does not.

### Typed relations (`policy/typed-relations.md`)

Some relations carry a literal object that should be **compared**, not just
matched — so the deterministic engine can order it, threshold it, or range over
it (e.g. "launched after 2030", "rank <= 3"). Declare such relations in
`policy/typed-relations.md`. Because the object is a literal, the relation should
ALSO be declared in `policy/attribute-relations.md`.

One declaration per line:

```
- `relation name` : <type> as <ascii_alias>
```

`<ascii_alias>` names the engine side-relation that holds the comparable value.
It is an author-chosen ASCII identifier (`[A-Za-z_][A-Za-z0-9_]*`) so it stays a
legal engine name even when the relation name is non-ASCII. Quote a relation name
containing spaces in backticks.

The four types:

- `date` — `2030.1` / `2030-01-15` → sortable yyyymmdd. **Engine-projectable**
  (ordering / threshold / range).
- `ordinal` — `3rd` / `3위` / `제3호` → int rank. The value must START with the number: `rank 3` does not parse. **Engine-projectable**.
- `amount` — `100억` / `1,000원` → integer base unit. **Engine-projectable**.
  Needs a unit table (a built-in default, or one declared inline).
- `number` — `1,000` / `3.5` → numeric magnitude, scaled to a sortable int64.
  **Engine-projectable** — but a comparison threshold must be written in the same
  scaled units.

An extractor may also emit a typed object as a compact compound term
(`date(2030,1)`, `amount(100,"억")`, …). For the exact `number` ×1000 scaling and
`ROUND_HALF_UP` rule, the `amount` unit-table syntax, the compound-term form, and
how to author a comparison predicate, see
**[docs/typed-relations.md](docs/typed-relations.md)**.

`factlog vocab` shows declared typed relations with a `[typed:<type>]` tag (e.g.
`[attribute, typed:date]`).

### Finding facts (`factlog search`)

When you don't know the exact name, `factlog search <term>` does a
case-insensitive substring match across subject / relation / object and lists
the matching facts (with status and source count). `vocab` lists names,
`search` finds facts by a fragment, `provenance` traces an exact triple.

```bash
factlog search fastapi   # case-insensitive; matches 'FastAPI'
factlog search acme      # partial — every fact mentioning the fragment
```

### Tracing a fact to its source (`factlog provenance`)

Every fact records the source it was extracted from. `factlog provenance` (alias
`trace`) lists, for a matching fact, every backing row — **source path, status,
confidence, the note (extracted excerpt), and a `[stale]` marker** when the
source file is missing on disk. All statuses are shown (including
`superseded`/`needs_review`), so retired backing stays visible.

```bash
factlog provenance Acme uses FastAPI   # exact triple
factlog provenance Acme uses           # all objects for (subject, relation)
factlog provenance Acme                # all facts about a subject
factlog provenance - uses              # relation only ('-' wildcards a position)
factlog provenance - - FastAPI         # object only
```

Positional terms are a `(subject, relation, object)` prefix; a literal `-`
wildcards that position and omitted trailing positions are wildcards too (at
least one non-wildcard term is required). Quote a term that contains spaces.

`/factlog ask` also lists each backing source path (`← <source>`) beneath a
verified engine answer, so a fact found via a query can be traced inline.

### Reviewing facts (`factlog review` / `accept` / `reject`)

Extraction marks facts `candidate` or `needs_review`; only `confirmed`/`accepted`
facts become engine input. Promote or retire them without hand-editing
`facts/candidates.csv`:

```bash
factlog review                       # list the pending queue (candidate + needs_review)
factlog review --status needs_review # narrow to one pending status
factlog accept Acme uses FastAPI     # pending → accepted (compiled into accepted.dl)
factlog accept Acme                  # accept every pending fact about a subject ('-' wildcards a position)
factlog reject Acme uses Datadog     # pending → superseded (retired, kept for audit)
factlog accept Acme uses FastAPI --dry-run
```

`accept`/`reject` change **only pending rows**; a `confirmed`/`accepted`/
`superseded` match is reported and left untouched (use `factlog eject` to retire
a non-pending fact). Both recompile `accepted.dl`.

To **correct** a fact's value (not just its status), use `factlog amend`:

```bash
factlog amend Widget codename Draft --set-object Falcon --set-note "name finalized" --accept
factlog amend Acme uses FastApi --set-object FastAPI    # fix a typo
```

The positional triple identifies the fact (exact match); `--set-subject` /
`--set-relation` / `--set-object` / `--set-note` give the new values (at least
one, or `--accept`). amend updates **both** `candidates.csv` and the backing
`runs/*.json` so the edit survives `/factlog sync` (a fact's value lives in
`runs/*.json` — merge rebuilds `candidates.csv` from it). `--accept` also
promotes to `accepted`. Confidence is not editable. `--dry-run` previews.

> **Durability:** a human `accept` (and `amend --accept`) is preserved across
> re-merge the same way `reject`/`superseded` is — `/factlog sync` will not
> revert your decisions.

### Excluding sources from sync (`factlog ignore`)

`/factlog sync` re-extracts **every** source on each run. To keep specific
sources out of that — a draft, a work-in-progress, an external doc — add them to
the per-KB **sync-ignore list** (`policy/sync-ignore.md`). Ignored sources are
skipped by `/factlog sync`, `factlog ingest --scan`, and coverage gap reporting,
**even when modified**. Their already-merged facts are kept untouched (use
`factlog eject` to actually remove a fact).

```bash
factlog ignore drafts/*.md sources/wip-notes.md   # add pattern(s)
factlog ignore                                     # list patterns + what they match
factlog ignore --remove drafts/*.md               # remove a pattern
```

`policy/sync-ignore.md` is one glob per line (same lenient format as the other
policy files — `#` comments, `-` bullets, backtick-quoted entries; quote a
pattern that starts with `#` in backticks). A pattern matches a source by its
full ref (`sources/...` / `runs/sources/...`) or by its path within the source
root. Glob semantics: `*` and `?` stay within one path segment (do **not** cross
`/`), `**` crosses segments, and a trailing `/` means the whole subtree:

| Pattern | Matches |
|---------|---------|
| `drafts/*.md` | `sources/drafts/x.md` — but not `sources/drafts/sub/x.md` |
| `drafts/**` (or `drafts/`) | everything under `sources/drafts/` |
| `**/*.md` | any `.md` at any depth |

`factlog sources` marks ignored sources `[ignored]` and coverage reports them as
`excluded` rather than gaps.

### Removing a source (`factlog eject`) — the inverse of `ingest`

`factlog eject <source>` undoes an ingest: it deletes the `runs/sources/`
conversion, strips the source's extracted rows from `runs/*.json`, and retires
the facts that cite it. Name a source by filename, stem, or KB-relative path —
naming the binary original (e.g. `report.pdf`) also matches its
`runs/sources/<name>.<ext>` conversion; a bare stem matches every source with that
stem.

🔴 The default `eject` is reversible (facts become `superseded`, kept for audit),
but **`--purge` and `--delete-original` delete data** — the candidate rows and the
original file. Preview with `--dry-run` first.

```bash
factlog eject report.pdf                 # delete conversion; mark citing facts superseded (kept for audit)
factlog eject report.pdf --purge         # delete the citing candidate rows instead of superseding them
factlog eject report.pdf --delete-original  # also delete the user's original under sources/
factlog eject report.pdf --dry-run       # show the planned changes, modify nothing
```

#### Removing a single fact (`--fact`)

When a source is fine but one extracted fact is wrong, retire just that fact —
the source's conversion and original stay in place:

```bash
factlog eject --fact "을서비스" "정식_운영" "2030.1"      # retire one fact (mark superseded)
factlog eject --fact "갑봇" "통합" "을서비스" --fact "값가" "대체" "값나"   # several at once
factlog eject --fact "을서비스" "정식_운영" "2030.1" --purge   # delete the candidate row instead
```

A fact is matched by its `(subject, relation, object)` triple across **all**
sources. The default `superseded` keeps `runs/*.json` untouched, so the
retirement is durable — a later `/factlog sync` re-asserts the fact from its
source but `merge_candidates` keeps it superseded. `--purge` instead deletes the
row and strips it from `runs/*.json`; if the source still asserts it, a re-sync
re-extracts it, so use the default to retire a fact for good. Fact mode and
source mode are mutually exclusive, and `--delete-original` is not valid with
`--fact`.

By default the retired facts are marked `superseded` (kept in
`facts/candidates.csv` for audit) and the original under `sources/` is **kept** —
so it would be re-converted on the next `/factlog sync`; pass `--delete-original`
to remove it too. `accepted.dl` is recompiled so the engine input drops the
retired facts immediately.

A `runs/sources/` conversion is tied to the original that produced it via the
ingest provenance header, so even when two originals share a stem,
`eject report.docx` never disturbs `report.pptx`'s conversion. A conversion that
*cannot* be attributed to a source — an old flat-layout conversion, one made from a
file outside `sources/`, or one whose original was deleted — is never guessed at:
`eject` leaves it in place and names it on stderr, and `--delete-original` refuses
rather than strand its facts. See **[docs/eject.md](docs/eject.md)** for these
attribution edge cases and the flat-layout migration.

`pages/` are not
regenerated by `eject` — run `/factlog sync` to reconcile them. The default
`superseded` mark is a current-state retire: if you keep a **text** original
under `sources/`, the next `/factlog sync` re-extracts and re-asserts its facts —
to remove a source for good, pass `--purge` and/or `--delete-original`.

Resolution precedence: `--target` flag > `$FACTLOG_ROOT` > active-KB
config (`${XDG_CONFIG_HOME:-~/.config}/factlog/config.json`) > current directory.
With no config set, behavior is unchanged (uses the current directory).

`/factlog sync` runs `factlog ingest --scan` as its first step, so binaries you
drop in `sources/` are converted automatically (idempotently — unchanged files
are skipped). If a binary has no `runs/sources/` conversion, `merge_candidates.py`
warns so the silent non-ingestion is visible.

## Usage

In a Claude Code session inside your knowledge base (the plugin is active in every session):

```
/factlog sync      # read sources/, extract candidate facts, update pages & decisions
/factlog query     # translate policy/questions.md into facts/query.dl (Datalog query draft)
/factlog check     # compile accepted facts, run the logic check over accepted + query, show the report
/factlog repair    # attempt gated self-correction of review_required queries
/factlog ask       # answer one question: deterministically routed to the engine (verified) or wiki exploration (unverified)
```

Run `/factlog query` before `/factlog check`: the logic check evaluates the
query draft in `facts/query.dl`, which `/factlog query` produces from your
natural-language questions in `policy/questions.md`.

## Determinism & limitations

A skill is a prompt, so the model is *guided*, not *forced*, to run each step. factlog keeps every step that must be reliable — fact compilation, the wirelog logic check, policy compilation, validation — as **bundled scripts the skill is instructed to run and trust**, never as model judgment. The logic check report is always produced by the engine, never narrated by the model.

### AC4 — stale-edit guard (two levels)

factlog enforces freshness through two distinct mechanisms:

| Level | Mechanism | What it guarantees |
|-------|-----------|-------------------|
| **Hook-enforced** | `PreToolUse` hook denies a `Write`/`Edit` to `facts/accepted.dl` or `facts/query.dl` when `facts/logic_report.txt` is older than those files, or is missing AND the target already exists (run `/factlog check` → `run_logic_check.py` to refresh). The first creation of `query.dl` on a fresh KB — no report yet, target not yet on disk — is allowed, so bootstrap is not blocked | The engine's compiled inputs cannot be overwritten when the logic report is stale — the hook blocks the tool call before the file is touched |
| **SKILL discipline (best-effort)** | `SKILL.md` instructs Claude to run `run_logic_check.py` and show `facts/logic_report.txt` verbatim before stating any conclusion | The model is *guided* to surface the engine report; it cannot be *forced* (R10: "cannot fully guarantee") — human review of the raw report is the final verification step |

These two levels are complementary: the hook closes the deterministic gap; the SKILL discipline covers the narration layer where engineering enforcement is not possible.

### Scale & performance

**You don't need to empty the KB for performance.** The logic-check cost depends
less on the total number of facts than on the number of **entity-to-entity
relations** (edges where the object of A→B becomes a subject again), because the
engine computes reachability (paths). An attribute-heavy KB — where objects are
mostly literals — scales cheaply to tens or hundreds of thousands of facts, while
a dense entity graph (citation/dependency networks, etc.) can get heavy sooner.
So the metric to watch is not the total fact count but the **entity↔entity edge
count**.

If it does get heavy, the answer is not to "empty" it. Adjust the relation
modeling and manage recurring cost with `factlog ignore` (exclude from
re-extraction) and idempotent ingest. Correctness and de-duplication hold
regardless of scale.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
