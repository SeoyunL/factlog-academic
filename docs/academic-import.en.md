# Academic bibliography integrations

> 🌐 **English** | [한국어](academic-import.md)

factlog imports bibliographic records into `sources/` from four places — Zotero,
OpenAlex, arXiv, and PubMed. They speak to different APIs but share one contract.
This page covers what they share; each integration's own page covers the rest.

| Integration | Page |
| --- | --- |
| Zotero | [zotero-import.en.md](zotero-import.en.md) |
| OpenAlex | [openalex.en.md](openalex.en.md) |
| arXiv | [arxiv.en.md](arxiv.en.md) |
| PubMed | [pubmed.en.md](pubmed.en.md) |

## Which one to use

Use **Zotero** if you already curate literature there — it is the only integration
that also brings PDF full text and highlights. Use **OpenAlex** to search across
fields or to walk a citation graph. Use **arXiv** to pull preprints by id or to
track versions. Use **PubMed** for biomedical literature, where MeSH terms and
retraction status matter.

A paper that exists in several of them still becomes one file. An arXiv, OpenAlex,
or PubMed import that finds an existing source with the same DOI or PMID written by
a **different** integration writes no second file — it folds its own record into that
source's provenance ledger instead (`merged`). Colliding with a source the same
integration wrote is a plain duplicate rather than a cross-source merge, so it is
`skipped`: two arXiv deposits sharing a DOI never fold into each other. This is why `doi`, `arxiv_id`, and `pmid` are bare front matter keys
rather than prefixed ones: the cross-source index looks for those literal keys.

**Zotero is the exception.** It does not fold; a duplicate is `skipped` and leaves
no ledger trace, because a personal library is not an upstream database — it is
something you already curated by your own criteria.

## The shared contract

1. **What you import is a candidate, not a fact.** Every import produces one
   `sources/<slug>.md` and still passes the `sync → review → accept` gate (P1/P2).
   These four databases are inputs to factlog, not its fact store.
2. **Existing `sources/` files are never modified** (P4). Not by `--auto-update`,
   not by an acknowledge command, not by backfill — their writes are confined to the
   provenance ledger under `source-provenance/`, plus the check-log
   (`check-log/<name>.json`) for the refresh commands. Your Zotero library is
   read-only too.
3. **Imports are idempotent** (P3). Re-importing skips anything whose identity key
   (`zotero_key` / `openalex_id` / `arxiv_id` / `pmid`) is already present.
4. **Import has no ledger authority.** It creates records; it never rewrites the
   fields of one that exists. Going upstream to learn a new value is a *refresh*,
   not an import.
5. **Retraction and withdrawal signals are never absorbed automatically.** They are
   raised to a human on every run until someone closes them explicitly.

## The same job, four names

| Job | Zotero | OpenAlex | arXiv | PubMed |
| --- | --- | --- | --- | --- |
| Import by id | `zotero-import --items` | `openalex-import` | `arxiv-import` (≤100/run) | `pubmed-import` (≤200/run) |
| Import a set | `zotero-import --collection\|--tag` | — | — | — |
| Search, then import | — | `openalex-search` | `arxiv-search` | `pubmed-search` |
| Walk citations | — | `openalex-cite --for <slug>` | — | — |
| Re-check upstream | — | `openalex-refresh` | `arxiv-check-versions` | `pubmed-refresh` |
| Human closure | — | `openalex-acknowledge-retraction` | `arxiv-acknowledge-withdrawal` | `pubmed-acknowledge-retraction` |
| Backfill the ledger | — | `openalex-backfill-provenance` | `arxiv-backfill-provenance` | `pubmed-backfill-provenance` |
| Also | `--pdf` · `--annotations` | — | — | `pubmed-mesh --for <slug>` |

Zotero has no refresh, closure, or backfill command, and that is not an omission —
it is a local library you curate yourself, so nothing upstream changes behind your back.

## Installing and identifying yourself

Each integration is a separate extra; install only what you need.

```bash
pip install 'factlog-academic[zotero]   @ git+https://github.com/SeoyunL/factlog-academic'
pip install 'factlog-academic[openalex] @ git+https://github.com/SeoyunL/factlog-academic'
pip install 'factlog-academic[arxiv]    @ git+https://github.com/SeoyunL/factlog-academic'
pip install 'factlog-academic[pubmed]   @ git+https://github.com/SeoyunL/factlog-academic'
```

> The bare name `factlog` on PyPI belongs to an unrelated 2013 project, which has
> no such extras — pip warns and exits 0, so you get a success message and none of
> the dependencies. Always install from the URL above (or from a clone:
> `pip install -e '.[arxiv]'`).

None of the four APIs authenticates you, but they ask for identification
differently. OpenAlex and arXiv take an optional `email` — a courtesy, carried in
the query string and the User-Agent respectively. PubMed **requires** a contact
email at request time and strongly wants an API key. Zotero needs no credential at
all, only the Zotero 7 desktop app running with **Settings → Advanced → "Allow
other applications on this computer to communicate with Zotero"** checked.

Request cost is a different kind of thing in each. OpenAlex meters **credits** —
about 1000/day, where a search costs 10 regardless of how many results it returns,
a single lookup costs 0, and each `openalex-cite` request costs 1 — so a small
`--limit` on a search saves you nothing. PubMed meters
nothing but **blocks your IP** if you exceed 3 requests/second (10 with a key), so
factlog paces and serialises requests itself; without a key the same batch takes
over three times as long. arXiv enforces nothing at all — factlog keeps to the
recommended 3-second delay on its own. Zotero is local.

### The credential boundary

A KB is often a version-controlled repository of its own, so real credentials are
deliberately not read from `<KB>/policy/*.toml`.

| Value | Read from KB policy? | Why |
| --- | --- | --- |
| OpenAlex `email` | Yes | Identification, not authentication |
| arXiv `email` | Yes | Identification, not authentication |
| PubMed `api_key` | **No** | A real credential — use `NCBI_API_KEY` or a user-level file |
| Zotero `web_api_key` | **No** | A real credential |

`NCBI_API_KEY` beats any file, matching how CI runners pass secrets without writing
them to disk, and the key never leaves `eutils.ncbi.nlm.nih.gov` — no third party,
model provider included, ever receives it. A non-string `email` is a hard failure in
OpenAlex, arXiv, and PubMed (other bad types fall back to defaults): it rides on every
request, and a typo must not silently degrade you to anonymous traffic. Zotero has no
`email` setting at all — it talks to a local API.

Settings resolve as: an explicitly passed path > `<KB>/policy/<name>-config.toml` >
`${XDG_CONFIG_HOME:-~/.config}/factlog/<name>.toml` > built-in defaults.

The `[import]` section is **not** shared. OpenAlex and arXiv read all four of
`default_limit`, `max_limit`, `skip_duplicates`, and `include_abstract`; Zotero reads
only the latter two; PubMed does not read the section at all — its limits are fixed
at 25/200 and abstracts are always included. An `[import]` block in a PubMed config
is ignored silently; use `pubmed-search --limit` instead (`pubmed-import` takes
repeatable `--pmid`, up to 200 per run).

## Flags that mean the same thing everywhere

`--target <KB>` (defaults to the active KB), `--porcelain` (tab-separated output for
scripts), `--dry-run` (writes no files), `--all` (import every search result without
prompting), `--older-than DAYS` and `--auto-update` on the refresh commands,
`--only-flagged` on `pubmed-refresh`, `--yes` on the acknowledge commands, and
`--show-query` on the arXiv and PubMed searches.

Only `--target` is truly universal. The three acknowledge commands take neither
`--porcelain` nor `--dry-run`, and `openalex-refresh`, `arxiv-check-versions`, and
`pubmed-mesh` take no `--dry-run` either — passing one is an argparse error, not a
no-op. The Korean page has the full exception table.

Two things about `--dry-run` surprise people. First, **on a search it is not enough
on its own** — it turns interactive selection off, so nothing gets selected; pair it
with `--all` (or with `--auto-import` on `openalex-cite`) to see a plan. Second,
**whether it touches the network varies**: `arxiv-import` and `pubmed-import` still
fetch (they need titles, retraction signals, and slugs to predict the outcome),
`pubmed-refresh --dry-run` does not fetch at all, and the backfill commands never
touch the network in any mode. A preview also cannot report a write that would fail
— an unwritable `source-provenance/` shows up only on the real run.

## The everyday flow

```bash
factlog zotero-import --collection "neurosymbolic AI"    # or a *-search / *-import
/factlog sync                                            # extract candidate facts
factlog review && factlog accept <id>                    # the human gate
factlog export --bibtex > refs.bib                       # cite what survived

factlog openalex-refresh                                 # periodically, re-check upstream
factlog arxiv-check-versions
factlog pubmed-refresh
```

## Where a search query lies quietly

All three search APIs answer a wrong query with "0 results" rather than an error,
and an operator reads that as "no such literature exists". factlog blocks this
differently in each place, and **arXiv and PubMed need opposite treatment**.

arXiv does not read several words as a phrase — it matches them loosely, returning
many times more than the phrase does — so factlog wraps them into `all:"your words"`
and tells you it wrapped. PubMed is the reverse: quoting turns its Automatic Term
Mapping **off**, which can collapse a real query into a `QuotedPhraseNotFound` zero,
so factlog sends your words as-is and surfaces PubMed's own `<QueryTranslation>` so
you can see how they were read. Both validate field tags against a closed set before
sending, both offer `--show-query`, and PubMed additionally surfaces upstream
diagnostics like `PhraseNotFound`. OpenAlex has neither problem — its `search=`
takes free text and handles phrasing itself.

## "Retracted" means something different in each

The same word covers different procedures, so each **database** integration keeps its
own prefixed front matter key and its own closing command. Zotero only carries over a
tag you set, and has no closing command.

| | What it is | Key | Closed by |
| --- | --- | --- | --- |
| arXiv | An **author's or administrator's** withdrawal of a preprint | `arxiv_withdrawn`, `arxiv_withdrawn_by` | `arxiv-acknowledge-withdrawal` |
| PubMed | A **fact** from NLM's curation of journal retractions | `pubmed_retracted`, `pubmed_retraction_notice_pmid` | `pubmed-acknowledge-retraction` |
| OpenAlex | An automatically derived **opinion** | `openalex_is_retracted` | `openalex-acknowledge-retraction` |
| Zotero | A **tag you put on the item** (any tag containing `retract`) | `retracted` — the only retraction key without a prefix | nothing — see below |

Zotero's key carries no prefix precisely because it is not an upstream claim but your
own judgement recorded in your own library.

**In place of a closing command, this value simply never updates.** factlog does not
read Zotero again after the import — `zotero-import` is the only Zotero command — so
adding or removing the tag in your library does not propagate to the KB, and a
re-import ends as `skipped` on the `zotero_key` match without rewriting the `.md`. To
change it, edit the front matter in `sources/*.md` yourself, the same way you would
add a missing `arxiv_version` below.

When they disagree, trust **PubMed > a Zotero tag > OpenAlex**: OpenAlex marks the
Lancet Commission dementia report as retracted while PubMed has no retraction record
for it. But that ordering decides *whom to believe*, not *when to skip the human
gate* — all three stay source-scoped and keep resurfacing until someone closes them.

Each closing command takes exactly one `--id`; there is no `--all` and no wildcard.
Each checks the ledger *before* spending a request, so a paper with no ledger is
refused without any network call and pointed at the backfill command.

In **all three** closing commands, **`--yes` can record a retraction or withdrawal but
never clear one** (#106): upstream going quiet is not the same as upstream reversing itself. For
arXiv it may be a withdrawal sentence that could not be read; for PubMed, a marker not
yet emitted. OpenAlex's `is_retracted` is a structured boolean with no such reading
failure, but OpenAlex is a known false-positive source (it flags works PubMed does
not), so in every case there is something for a human to weigh in the note. Recording
wrongly is a nuisance; clearing wrongly means citing a retracted paper. Recording makes
noise; clearing creates silence, and silence needs a human at the prompt — so a clear
under `--yes` is refused and nothing is written. To clear, re-run in a terminal without
`--yes` and confirm.

## What `--auto-update` is allowed to write

The refresh commands report by default, writing nothing but a check-log timestamp.
Even with `--auto-update` they write a narrow set of fields to the ledger:
`doi` / `work_type` / `journal` for OpenAlex, `version` / `last_updated` / `comment`
for arXiv, `doi` / `journal` for PubMed. These are *transcription* facts — a DOI that
did not exist at import time, a journal abbreviation NLM has since normalised.
Upstream's answer corrects what the ledger copied down; it does not assert anything
about the world. `sources/*.md` front matter is read but never opened for writing, so bytes and `mtime_ns` are
unchanged (P4), and a run with nothing to change is a byte-level no-op.

**Identity changes are never followed.** If OpenAlex merges works and answers a
request for `W_a` with `W_b`, that is reported as `id superseded` and the ledger key
stays put. A merged PMID is proposed, not followed — a PMID is a cross-source join
key, so re-keying it changes what future imports merge into, which makes it a human
decision (P1). A deleted PMID is flagged, never silently dropped, and a network
failure is never mistaken for a deletion.

arXiv adds two states of its own: **`no-version`**, when there was no version to
compare (calling that `unchanged` would hide the paper from the very signal this
command exists for), and **`version-conflict`**, when two sources of one paper claim
different versions — picking one of them would be a guess, not a refresh, so it
resurfaces every run until a human reconciles the sources.

## Backfilling a ledger

Items imported before the ledger existed have front matter but no ledger, and
re-importing does not create one. Without a ledger there is nowhere to record a
decision, so **a retraction on such an item cannot be closed** — the acknowledge
command refuses it and points here.

```bash
factlog openalex-backfill-provenance --dry-run
factlog arxiv-backfill-provenance --dry-run
factlog pubmed-backfill-provenance --dry-run
```

Backfill makes no new claim; it moves where an existing belief is stored. That is
why it has no confirmation prompt, no `--yes`, and no TTY gate — and why it never
touches the network, which would turn it into a refresh that absorbs retractions
appearing after the import.

It refuses what it cannot honestly reproduce: anything missing `imported_at`; an
arXiv paper whose `arxiv_version` cannot be read (`version` is an *identifying*
field, and writing it absent would make a later real import see a fake divergence);
and an OpenAlex or PubMed record whose retraction flag is not a YAML boolean (`1`,
`yes`, `on`). That last one is never repaired by guessing, because both guesses lie
— dropping the value claims upstream did not flag the item, and reading `1` as true
claims a retraction no source made.

One case no command can fix: an arXiv paper with no ledger *and* no `arxiv_version`.
Import skips it as already imported, backfill refuses it. A human adds
`arxiv_version: <N>` to its front matter by hand — read `<N>` from
`https://arxiv.org/abs/<id>`; factlog will not fetch it for you — and then backfill
can build the ledger.

## What only Zotero has

Because Zotero is a library a person already curated, it carries two things the
search APIs cannot. `--pdf` downloads stored PDF attachments and runs them through
the existing `ingest` pipeline into `runs/sources/` (needs `pdftotext` from poppler;
**add `*.pdf` to `.gitignore` if your KB is version-controlled**). `--annotations`
imports highlights and notes into `sources/<stem>-notes.md`, rewriting only when
Zotero's state actually changed and never overwriting a file you wrote yourself (P4).

Both respect the P1 boundary: highlights and notes enter as *source text*, not as
candidates. Candidates still come from `sync` and still need your `accept`.

## Exporting citations reads all four

```bash
factlog export --bibtex > refs.bib
factlog export --csl -o refs.json
```

Each integration stores its entry type under a different key, so the exporter takes
the first one that answers: `item_type` (Zotero), then `type` but only when
`imported_from: openalex`, then `preprint: true` (arXiv), and only if no key declared
a type at all, the presence of `journal` (PubMed). That last step never overrides a
declared type — an arXiv deposit stays a preprint even when `journal` records where
it was later published.

## Further reading

The Korean [학술 서지 연동 통합 사용법](academic-import.md) is the fuller version of
this page, and the per-integration Korean pages ([Zotero](zotero-import.md) ·
[OpenAlex](openalex.md) · [arXiv](arxiv.md) · [PubMed](pubmed.md)) go further still:
per-command options and output, the generated source file layout, config file keys,
and the reasoning behind each determinism boundary.
