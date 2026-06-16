# factlog

> facts + logic — a Claude Code skill that turns markdown sources into **verifiable, source-backed facts**.
> The LLM extracts; a deterministic Datalog/wirelog engine verifies.

**Status:** stable release (`v0.1.0`).

> Not affiliated with the unrelated `tkf/factlog` Emacs file-access logger.

## What it is

factlog is a [Claude Code](https://code.claude.com) **skill** for keeping a markdown knowledge base honest. It follows one rule:

> The agent does not draw conclusions. The agent produces files and calls a CLI. The CLI returns a verifiable report.

- **The LLM (Claude, in-session) extracts** candidate facts from your `sources/`, drafts Datalog queries from natural-language questions, and attempts limited self-correction.
- **A deterministic engine (wirelog via [pyrewire](https://github.com/semantic-reasoning/PyreWire)) verifies** them — compiling confirmed facts, running the logic check, and surfacing policy findings, conflicts, and `review_required` items.

Anything the model produces is a *candidate* until the engine and a human confirm it.

## How it works

```
sources/        →  Claude extracts        →  facts/candidates.csv, pages/, decisions/
candidates       →  human review           →  confirmed facts
confirmed        →  compile (deterministic) →  facts/accepted.dl
questions        →  Claude drafts query     →  facts/query.dl
accepted + query →  wirelog logic check     →  facts/logic_report.txt   ← the verifiable report
review_required  →  Claude repairs (gated)  →  decisions/correction_trace.md
```

## Source file formats

`/factlog sync` extracts facts by reading each file under `sources/` **as text,
in-session**. The bundled engine (`merge_candidates.py`) tracks every file in
`sources/` as a source *path*, but it never parses file contents — so a file is
only ingested if its text can be read during extraction. When a non-text file is
found, `merge_candidates.py` prints a warning so the silent non-ingestion is
visible.

| Format | Status | Notes |
|--------|--------|-------|
| `.md`, `.markdown`, `.txt` | **Directly supported** | UTF-8 text, read verbatim. This is what every extraction reference assumes. |
| Other UTF-8 text (`.rst`, `.org`, `.csv`, source code) | Supported as plain text | No special parsing; treated as raw text. |
| `.docx`, `.pptx`, `.xlsx`, `.hwp`, binary `.pdf`, images | **Not directly ingested** | Convert to Markdown/text first, then place the converted file in `sources/`. |

A binary file left in `sources/` is registered as a source path but yields **no
facts** (silent non-ingestion). Convert it first, e.g.:

```bash
pandoc report.docx -t gfm -o sources/report.md      # Office → Markdown
textutil -convert txt report.docx -output report.txt # macOS built-in
```

## Requirements

- Python **3.10+**
- **pyrewire 1.0.1+** (`pip install -r requirements.txt`)
- Claude Code CLI

## Install

factlog is a **Claude Code plugin**.

### Local install (supported)

Assuming the repo is already at `~/git/semantic-reasoning/factlog`, register it as a local plugin in a Claude Code session:

```
/plugin marketplace add ~/git/semantic-reasoning/factlog
```

Then run the one-shot bootstrap from inside a Claude Code session:

```
/factlog setup
```

`setup` runs `doctor`, installs the engine dependency, scaffolds the KB, and re-checks the environment — in one command.

### Marketplace install (future)

Once published to the Claude Code marketplace:

```
/plugin marketplace add semantic-reasoning/factlog
/plugin install factlog@semantic-reasoning
/factlog setup                     # one-shot: deps + doctor + init, in-session
```

### What `/factlog setup` does

`setup` collapses the previously-separate post-install steps into a single command. Equivalently, by hand:

```bash
pip install -r ~/git/semantic-reasoning/factlog/requirements.txt   # pyrewire>=1.0.1,<2.0
python3 -m factlog doctor          # checks Python 3.10+ and pyrewire
python3 -m factlog init --target ~/wiki   # scaffold the KB layout
```

If your Python is externally managed (PEP 668), pip will refuse to install into it; `setup` prints venv guidance instead of forcing the install. Create and activate a venv, then re-run `setup`:

```bash
python3 -m venv ~/.factlog-venv && source ~/.factlog-venv/bin/activate
python3 -m factlog setup --target ~/wiki
```

## Usage

In a Claude Code session inside your knowledge base (the plugin is active in every session):

```
/factlog sync      # read sources/, extract candidate facts, update pages & decisions
/factlog query     # translate policy/questions.md into facts/query.dl (Datalog query draft)
/factlog check     # compile accepted facts, run the logic check over accepted + query, show the report
/factlog repair    # attempt gated self-correction of review_required queries
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
| **Hook-enforced** | `PreToolUse` hook denies any `Write`/`Edit` to `facts/accepted.dl` or `facts/query.dl` when `facts/logic_report.txt` is missing or older than those files (run `/factlog check` → `run_logic_check.py` to refresh) | The engine's compiled inputs cannot be overwritten when the logic report is stale — the hook blocks the tool call before the file is touched |
| **SKILL discipline (best-effort)** | `SKILL.md` instructs Claude to run `run_logic_check.py` and show `facts/logic_report.txt` verbatim before stating any conclusion | The model is *guided* to surface the engine report; it cannot be *forced* (R10: "cannot fully guarantee") — human review of the raw report is the final verification step |

These two levels are complementary: the hook closes the deterministic gap; the SKILL discipline covers the narration layer where engineering enforcement is not possible.

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
