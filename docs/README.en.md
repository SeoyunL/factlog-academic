# factlog documentation

> 🌐 **English** | [한국어](README.md)

Detailed documentation for factlog. For the project introduction, see the
[repository README](../README.en.md).

## Reading order

If you are new, read these in order.

1. [Install](guide/install.en.md) — requirements, plugin install, `/factlog setup`
2. [Slash command usage](reference/slash-commands.en.md) — `/factlog sync` · `query` · `check` · `repair` · `ask`
3. [Reviewing facts](reference/review.en.md) — the gate where a human confirms candidates
4. [Determinism & limitations](guide/determinism.en.md) — what is guaranteed and what is not

## Guides

| Page | Contents |
|--------|------|
| [Install](guide/install.en.md) | Requirements, marketplace install, local install (development), what `/factlog setup` does |
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
| [Windows](reference/windows.en.md) | Windows Python executable, PEP 668 venv guidance |
