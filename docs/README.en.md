# factlog documentation

> 🌐 **English** | [한국어](README.md)

Detailed documentation for factlog. For the project introduction, see the
[repository README](../README.en.md).

## Reading order

If you are new, read these in order.

1. [Concepts](guide/concepts.en.md) — what factlog is, what a KB folder looks like, how candidate and accepted differ
2. [Install](guide/install.en.md) — requirements, plugin install, `/factlog setup`
3. [Use cases](guide/use-cases.en.md) — reports, slides, papers, and wikis as real command flows
4. [Slash command usage](reference/slash-commands.en.md) — `/factlog sync` · `query` · `check` · `repair` · `ask`
5. [Reviewing facts](reference/review.en.md) — the gate where a human confirms candidates
6. [Determinism & limitations](guide/determinism.en.md) — what is guaranteed and what is not

To walk the whole flow through once without your own data, start with the
[quick-start tutorial](../examples/sample-kb/README.md) (Korean only).

## Guides

| Page | Contents |
|--------|------|
| [Concepts](guide/concepts.en.md) | Overview, KB folder layout, the candidate vs accepted trust boundary, commands at a glance, how-it-works diagram |
| [Install](guide/install.en.md) | Requirements, marketplace install, local install (development), what `/factlog setup` does |
| [Use cases](guide/use-cases.en.md) | Common workflows for reports, slides, papers, wikis, provenance tracing, and candidate cleanup |
| [Determinism & limitations](guide/determinism.en.md) | Limits of skill discipline, AC4 stale-edit guard, scale & performance |

## Reference

| Page | Contents |
|--------|------|
| [Slash command usage](reference/slash-commands.en.md) | `/factlog sync` · `query` · `check` · `repair` · `ask` |
| [Source file formats](reference/sources.en.md) | Supported format table, `factlog ingest`, conversion naming, upgrade note |
| [Active KB](reference/active-kb.en.md) | `factlog use`/`where`, target the KB from anywhere, resolution precedence |
| [Reviewing facts](reference/review.en.md) | `factlog review` · `accept` · `reject` · `amend`, durability of human decisions |
| [Vocabulary · search · provenance](reference/search-provenance.en.md) | `factlog vocab` · `search` · `provenance` |
| [Typed relations](reference/typed-relations.en.md) | `policy/typed-relations.md`, date · ordinal · amount · number |
| [Excluding and removing sources](reference/ignore-eject.en.md) | `factlog ignore` (exclude from sync), `factlog eject` (undo an ingest), `--fact` |
| [Windows](reference/windows.en.md) | Windows Python executable, Git Bash, PEP 668 venv guidance |
