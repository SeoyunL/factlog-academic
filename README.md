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

Install the engine dependency and verify the environment:

```bash
pip install -r ~/git/semantic-reasoning/factlog/requirements.txt   # pyrewire>=1.0.1,<2.0
python3 -m factlog doctor          # checks Python 3.10+ and pyrewire
```

### Marketplace install (future)

Once published to the Claude Code marketplace:

```
/plugin marketplace add semantic-reasoning/factlog
/plugin install factlog@semantic-reasoning
```

## Usage

In a Claude Code session inside your knowledge base (the plugin is active in every session):

```
/factlog sync      # read sources/, extract candidate facts, update pages & decisions
/factlog check     # compile accepted facts, run the logic check, show the report
/factlog repair    # attempt gated self-correction of review_required queries
```

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

MIT — see [LICENSE](LICENSE).
