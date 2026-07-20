# OpenAlex import (`factlog openalex-*`)

> 🌐 **English** | [한국어](openalex.md)

You can search and import literature from the open bibliographic database
[OpenAlex](https://openalex.org), widen the citation graph by one hop, and
re-check the metadata of records you already imported. Imported items, like
Zotero's, are still **candidates** and pass the `sync → review → accept` gate.

## Prerequisites

This needs the `openalex` extra, and OpenAlex is **unauthenticated** — no API key
or account.

```bash
pip install 'factlog-academic[openalex] @ git+https://github.com/SeoyunL/factlog-academic'
```

> The bare name `factlog` on PyPI belongs to an unrelated 2013 project ("File
> ACTivity LOGger"), which has no such extras — pip warns and exits 0, so you get
> a success message and none of the dependencies. Always install from the URL
> above (or from a clone: `pip install -e '.[openalex]'`).

## Usage

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

## Determinism boundary

The three ledger-based imports — OpenAlex, arXiv, PubMed — obey the same three
rules.

1. **Import has no ledger authority.** An imported record is a `candidate` until it
   passes the `sync → review → accept` gate; import never writes engine input.
2. **`--auto-update` touches only the ledger, never `sources/*.md`.** A `*-refresh`
   / `*-check-versions --auto-update` records a few metadata fields to the
   per-paper ledger under `source-provenance/`; your source files are never rewritten.
3. **A retraction or withdrawal is never absorbed automatically.** It is reported
   to a human on every run and closed only by an explicit `*-acknowledge-*`
   command — revoking a published result is the judgment a human must make, not
   the tool.

OpenAlex specifics:

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

## Configuration

Settings resolve in the order `<KB>/policy/openalex-config.toml` >
`~/.config/factlog/openalex.toml` > built-in defaults. Unlike Zotero, there is
**no secrets boundary**: `email` is not authentication but an identification
courtesy, so it is safe in a committed KB policy file (Zotero's `web_api_key` is
read only from a user-level file).

## Further reading

The Korean [OpenAlex 가져오기](openalex.md) covers more than this page does:
per-command options and output, the credit budget in detail, the shape of the
generated source file and its provenance front matter, the config file keys, and
the idempotency guarantees.
