# factlog

> facts + logic — a Claude Code skill that turns markdown sources into **verifiable, source-backed facts**.
> The LLM extracts; a deterministic Datalog/wirelog engine verifies.

**Status:** pre-release (`v0.1.0.dev`). Scaffolding in progress.

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

## Requirements

- Python **3.10+**
- **pyrewire 1.0.1+** (`pip install -r requirements.txt`)
- Claude Code CLI

## Install

```bash
git clone https://github.com/semantic-reasoning/factlog
cd factlog
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python3 -m factlog install --target ~/wiki      # installs the skill + bundled engine into a target KB
```

## Usage

In a Claude Code session inside your knowledge base:

```
/factlog sync      # read sources/, extract candidate facts, update pages & decisions
/factlog check     # compile accepted facts, run the logic check, show the report
/factlog repair    # attempt gated self-correction of review_required queries
```

## Determinism & limitations

A skill is a prompt, so the model is *guided*, not *forced*, to run each step. factlog keeps every step that must be reliable — fact compilation, the wirelog logic check, policy compilation, validation — as **bundled scripts the skill is instructed to run and trust**, never as model judgment. The logic check report is always produced by the engine, never narrated by the model.

## License

MIT — see [LICENSE](LICENSE).
