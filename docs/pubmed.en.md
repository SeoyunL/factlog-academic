# PubMed import (`factlog pubmed-*`)

> 🌐 **English** | [한국어](pubmed.md)

You can search and import biomedical records from [PubMed](https://pubmed.ncbi.nlm.nih.gov)
(NCBI E-utilities) by PMID, re-check whether a record's metadata or retraction
status has drifted, and turn a paper's PubMed MeSH terms into vocabulary
proposals. Imported items are still **candidates** and pass the
`sync → review → accept` gate.

## Prerequisites

This needs the `pubmed` extra:

```bash
pip install 'factlog-academic[pubmed] @ git+https://github.com/SeoyunL/factlog-academic'
```

> The bare name `factlog` on PyPI belongs to an unrelated 2013 project ("File
> ACTivity LOGger"), which has no such extras — pip warns and exits 0, so you get
> a success message and none of the dependencies. Always install from the URL
> above (or from a clone: `pip install -e '.[pubmed]'`).

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

## Usage

```bash
factlog pubmed-search --query "immune checkpoint" --mesh "Neoplasms" --limit 25
factlog pubmed-import --pmid 16354850                # repeatable, up to 200 per run
factlog pubmed-refresh --older-than 30               # reports drift; --auto-update records identifier/journal
factlog pubmed-acknowledge-retraction --id 16354850
factlog pubmed-backfill-provenance --dry-run         # gives a ledger to front-matter-only papers
factlog pubmed-mesh --for <slug>                     # propose MeSH vocabulary from a paper's ledger PMID
```

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

PubMed specifics:

- `--auto-update` writes only the identifier and journal fields.
- On retractions PubMed is the **authority** — where OpenAlex's `is_retracted` has
  documented false positives (#51), a PubMed retraction is trusted. It still
  surfaces every run until `factlog pubmed-acknowledge-retraction` records that a
  human saw it.
- A deleted PMID is flagged (never silently dropped); a merged PMID is reported
  (never silently rewritten).
- MeSH terms from PubMed and OpenAlex coexist, each source-scoped; PubMed's carry
  the major/minor distinction OpenAlex cannot supply reliably before ~2022 (#53).

## API key

An API key is optional for NCBI but, for factlog's batched requests, effectively
required — without one E-utilities throttles hard. It is read from the
`NCBI_API_KEY` environment variable or `~/.config/factlog/pubmed.toml`
(there is no `--config` flag on the CLI), and **deliberately never from a KB `policy/` file** (a KB
is often its own committed repo, so the secrets boundary keeps the credential out
of it).

## Further reading

The Korean [PubMed 가져오기](pubmed.md) covers more than this page does:
per-command options and output, how `--show-query` and `--dry-run` differ, the
silent trap in a search query, why a `--year` search can record a year outside that
range (#387) or no year at all (#389), merged and deleted PMIDs (#170), the shape of the
generated source file and why there are two `mesh_terms` fields, the config file
keys, and the idempotency guarantees.
