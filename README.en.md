# factlog

> 🌐 **English** | [한국어](README.md)

> facts + logic — a tool that turns markdown sources into **verifiable, source-backed facts**.
> The LLM extracts; a deterministic Datalog/wirelog engine verifies.
>
> factlog is a [Claude Code](https://code.claude.com) **plugin**. Inside a session you
> use it through `/factlog ...` slash commands; human gates like review and approval you
> run yourself in the terminal through the Python CLI (`python3 -m factlog ...`). Both
> entry points call the same deterministic engine — slash command · Python CLI ·
> verification engine are one tool.

![How factlog works: Claude proposes, the engine verifies, a human confirms](docs/how-it-works.svg)

## Requirements

- Python **3.11+** (required by the engine dependency `pyrewire`)
- **pyrewire 1.0.3+** (`pip install -r requirements.txt`)
- Claude Code CLI
- **git** — the marketplace install uses `git clone` under the hood. On Windows, install **Git for Windows**.

## Install

factlog is a **Claude Code plugin**. Install it from this repo's marketplace in a Claude Code session:

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

## Quick start

If you are new, start with the
[quick-start tutorial](examples/sample-kb/README.md) (Korean only), which walks
the whole flow through once without your own data.

## Documentation

The detailed documentation lives in [`docs/`](docs/README.en.md).

- [Concepts](docs/guide/concepts.en.md) — overview, KB folder layout, the candidate vs accepted trust boundary, commands at a glance
- [Install](docs/guide/install.en.md) — requirements, marketplace and local install, `/factlog setup`
- [Use cases](docs/guide/use-cases.en.md) — common workflows for reports, slides, papers, and wikis
- [Determinism & limitations](docs/guide/determinism.en.md) — what is guaranteed and what is not
- [Slash command usage](docs/reference/slash-commands.en.md) · [Source file formats](docs/reference/sources.en.md) · [Reviewing facts](docs/reference/review.en.md) — detailed reference

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
