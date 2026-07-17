# Vocabulary · search · provenance

> 🌐 **English** | [한국어](search-provenance.md)

## Discovering the vocabulary (`factlog vocab`)

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

## Finding facts (`factlog search`)

When you don't know the exact name, `factlog search <term>` does a
case-insensitive substring match across subject / relation / object and lists
the matching facts (with status and source count). `vocab` lists names,
`search` finds facts by a fragment, `provenance` traces an exact triple.

```bash
factlog search fastapi   # case-insensitive; matches 'FastAPI'
factlog search acme      # partial — every fact mentioning the fragment
```

## Tracing a fact to its source (`factlog provenance`)

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
