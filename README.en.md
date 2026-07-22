# factlog

> 🌐 **English** | [한국어](README.md)

**facts + logic** — a tool that pulls factual claims out of your documents,
**attaches the file and section each one came from**, and automatically checks that
those facts do not contradict each other. An LLM does the extraction; a
**deterministic engine — one that returns the same result every time** — does the
verification. No LLM guesswork enters the verification step, so the same facts and
the same question always produce the same verdict. (The engine is built on
Datalog/wirelog.)

Supported formats include markdown and plain text, but also Word, PDF, HWP, and
PowerPoint — factlog converts non-text documents to text automatically before
processing them.

An extracted claim does not become a fact on its own. Anything that needs a
judgement call goes into a review queue, and a human has to approve it with
`factlog accept` before it becomes input to the verification engine — see
[candidate vs accepted — the trust boundary](docs/guide/concepts.en.md).

factlog is a [Claude Code](https://code.claude.com) plugin. Inside a session you use
it through `/factlog ...` slash commands; the steps where a human reviews and
approves you run yourself in the terminal through the Python CLI
(`python3 -m factlog ...`). Both entry points call the same verification engine —
slash command · Python CLI · verification engine are one tool.

![How factlog works: Claude proposes, the engine verifies, a human confirms](docs/how-it-works.svg)

## Example — what one line of a document turns into

Say a document you put in `sources/` contains this sentence:

```text
Claude Code is a command-line tool developed by Anthropic ...
```

`/factlog sync` extracts facts from it, with the source tracked down to the file and
section. Anything that needs a human judgement call lands in the review queue as
`needs_review`:

```csv
subject,relation,object,source,status,confidence,note
Anthropic,develops,Claude Code,sources/example.md#what-is-claude-code,needs_review,0.90,inferred from developed_by relation
```

Approve it in the terminal, and it becomes input to the verification engine:

```console
$ factlog accept "Anthropic" develops "Claude Code"    # or: python3 -m factlog accept ...
  Anthropic / develops / Claude Code  [needs_review → accepted]  ← sources/example.md#what-is-claude-code
factlog accept: 1 row(s) → accepted; accepted.dl recompiled
```

Approved facts are written to the engine input file `facts/accepted.dl` as Datalog
facts:

```datalog
relation("Anthropic", "develops", "Claude Code").
```

Now you can answer questions grounded in that fact, or check it against the others
for contradictions. To walk the whole flow yourself, see the
[quick-start tutorial](examples/sample-kb/README.md).

## What documents can you feed it

Not just markdown. Drop the originals into `sources/` and `/factlog sync` converts
whatever needs converting to text. Your original files are never modified.

| | Formats | What you need |
|---|---|---|
| **Read directly** | `.md` · `.txt` · `.csv` · `.rst` · `.org` · source code | nothing |
| **Auto-converted (built in)** | `.hwp` · `.hwpx` (Hangul) · `.pptx` (PowerPoint) | nothing — except `.hwp`, which needs `pyhwp` + pandoc |
| **Auto-converted (external tool)** | `.pdf` · `.docx` · `.odt` · `.html` · `.epub` · `.rtf` | pandoc (`.docx` `.odt` `.html` `.epub`) · poppler (`.pdf`) · textutil (`.rtf` — **macOS only**) |

`.xlsx` and images are not converted (export sheets to `.csv` instead). The table
omits extension aliases that share a chain (`.htm`, `.markdown`). For the per-format
converter chains and fallbacks, how to install them, and what happens when a
converter is missing, see [source file formats](docs/reference/sources.en.md).

## Install

factlog is a [Claude Code](https://code.claude.com) **plugin**. Before you start, you
need the following in place:

- Python **3.11+** (required by the engine dependency `pyrewire`)
- **pyrewire 1.0.3+** (`pip install -r requirements.txt`)
- Claude Code CLI
- **git** — the marketplace install uses `git clone` under the hood. On Windows, install **Git for Windows**.

Then install it from this repo's marketplace in a Claude Code session:

```
/plugin marketplace add https://github.com/semantic-reasoning/factlog
/plugin install factlog@semantic-reasoning
/reload-plugins
/factlog setup                     # one-shot: deps + doctor + init, in-session
```

Run these commands **one line at a time**. If you paste multiple plugin commands
at once, Claude Code may try to process the marketplace registration and install
out of order.

After a successful install, the new `/factlog ...` commands may not be loaded in
the current session yet. Run `/reload-plugins` after `/plugin install`, then run
`/factlog setup`.

For the local install (development), what `/factlog setup` does, PEP 668 venv
guidance, and the Windows Python executable, see the
[install guide](docs/guide/install.en.md).

## What is deterministic and what is not

Verification is deterministic; **extraction is not.** Pulling facts out of a document
is the LLM's job, so running it again may give you different facts. Conversely, the
same accepted facts and the same query always yield the same verdict. For what is and
is not guaranteed, see [determinism & limitations](docs/guide/determinism.en.md).

## Quick start

If you are new, start with the
[quick-start tutorial](examples/sample-kb/README.md) (Korean only), which walks
the whole flow through once without your own data.

## Documentation

The detailed documentation lives in [`docs/`](docs/README.en.md).

- [Concepts](docs/guide/concepts.en.md) — overview, KB folder layout, the candidate vs accepted trust boundary, commands at a glance
- [Install](docs/guide/install.en.md) — requirements, marketplace and local install, `/factlog setup`
- [Use cases](docs/guide/use-cases.en.md) — common workflows for reports, slides, papers, and wikis
- [Source file formats](docs/reference/sources.en.md) — supported formats, converter chains, behaviour when conversion fails
- [Determinism & limitations](docs/guide/determinism.en.md) — what is guaranteed and what is not
- [Slash command usage](docs/reference/slash-commands.en.md) · [Reviewing facts](docs/reference/review.en.md) — detailed reference

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
