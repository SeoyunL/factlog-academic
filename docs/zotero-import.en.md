# Zotero import (`factlog zotero-import`)

> 🌐 **English** | [한국어](zotero-import.md)

If you already manage your literature in Zotero, you can migrate collections,
tags, or individual items into factlog's `sources/` in one pass. factlog does not
replace Zotero — it is a verification layer on top of it: keep using Zotero, and
let factlog handle fact extraction, provenance tracing, and logic checking.
Migrated items are still **candidates** and pass the `sync → review → accept`
gate. The Zotero originals are never modified (read-only).

## Prerequisites

This needs Zotero 7's **Local API** (enable it under Settings → Advanced; it
listens on port 23119) and the `zotero` extra:

```bash
pip install 'factlog-academic[zotero] @ git+https://github.com/SeoyunL/factlog-academic'
```

> The bare name `factlog` on PyPI belongs to an unrelated 2013 project ("File
> ACTivity LOGger"). Asking pip for that name installs **that** package — and since
> it has no such extras, pip merely warns and exits 0, so you get a success message,
> a package you never wanted, and none of the dependencies. Always install from the
> URL above (or from a clone: `pip install -e '.[zotero]'`).

## Usage

```bash
factlog zotero-import --collection "Systematic Review"   # migrate a collection
factlog zotero-import --collection "Systematic Review" --dry-run   # preview the plan only
factlog zotero-import --tag "to-review"                  # by tag
factlog zotero-import --items "KH78JUPE,64DA4TQJ"        # individual items
```

Without `--target` the migration goes to the active KB, and `--porcelain` emits
machine-readable output for scripts. `--pdf` also fetches each item's PDF
attachments and converts their full text with the existing `ingest` path (the
original PDF is stored in `sources/`, so `.gitignore` your `*.pdf` if you
version-control the KB). `--annotations` migrates highlights and notes into
`sources/<stem>-notes.md` (still just sources — the candidates go through `sync`
plus a human gate). After migrating, run `/factlog sync` to extract candidate
facts.

```bash
factlog zotero-import --collection "Systematic Review" --pdf            # bibliography + PDF full text
factlog zotero-import --collection "Systematic Review" --annotations    # + highlights & notes
```

## Citation export

`factlog export --bibtex|--csl` emits one entry per source. Each integration
records the work type under a different front-matter key, so the exporters take
the **first key that answers** (#384):

| Order | Key | Written by | Example |
|---|---|---|---|
| 1 | `item_type` | Zotero | `journalArticle` → `@article` / `article-journal` |
| 2 | `type` (only when `imported_from: openalex`) | OpenAlex | `conference-paper` → `@inproceedings` / `paper-conference` |
| 3 | `preprint: true` | arXiv | → `@misc` / `article` |
| 4 | presence of `journal`, *only if no key above answered* | PubMed | → `@article` / `article-journal` |

Step 4 never overrides a declared type: Zotero copies `publicationTitle` into
`journal` for every item type (so magazine and newspaper articles would be
mistyped as journal articles), and an arXiv deposit stays a preprint even once
`journal` records where it was published (#60). A declared type with no mapping
keeps the default (`@misc` / `document`) rather than being guessed at.

### Where the venue goes

Standard BibTeX scopes venue fields per entry type — `journal` belongs to
`@article` alone — so the exporters first decide *what the* `journal` *value is*
and both follow that one judgement:

| Venue is | Types | BibTeX field | CSL variable |
|---|---|---|---|
| a periodical | journal/magazine/newspaper article | `journal` | `container-title` |
| a containing work | conference paper, book chapter, dictionary/encyclopedia entry | `booktitle` | `container-title` |
| an issuing body | report | `institution` | `publisher` |
| a degree-granting school | thesis | `school` | `publisher` |
| informal | preprint, dataset, software, unmapped | `howpublished` | `publisher` |
| absent | a whole book | (omitted) | (omitted) |

`@inproceedings`/`@incollection` require `booktitle`, so the previous `journal`
placement both misfiled the venue and triggered `Warning--empty booktitle`.

**Why informal venues use CSL `publisher`.** This is the one cell that looks
wrong on paper: the value is a periodical name, yet it goes to `publisher` and
not `container-title`. The grounds are measured. An informal record is CSL-typed
`article` (a preprint — #60 forbids retyping a deposit once `journal` records
where it landed), and for a standalone `article` the styles disagree about
`container-title` while agreeing about `publisher`. One preprint carrying
`Nature 585, 357 (2020)`, rendered with `pandoc --citeproc`:

| Style | `container-title` | `publisher` |
|---|---|---|
| ieee | **venue dropped entirely** | `2020, Nature 585, 357 (2020).` |
| apa | `In Nature 585, 357 (2020).` | `Nature 585, 357 (2020).` |
| chicago | `In Nature 585… Preprint.` | `Preprint, Nature 585, 357 (2020).` |
| ama, nature | renders | renders |

So `container-title` reproduces, under IEEE, the very venue loss #384 set out to
fix, and asserts a containment ("In") the record does not claim. `publisher`
survives every style and is phrased as "Preprint at" / "Preprint posted online",
which is what the record means. That pandoc reads BibTeX `howpublished` back as
`publisher` — making the two exports agree on a round trip — corroborates the
choice but is not the reason for it.

A BibTeX-side tradeoff remains: classic styles (plain/unsrt/alpha) silently drop
`journal` but render `howpublished`, so those improve; pandoc read `journal` as
`container-title` and reads `howpublished` as `publisher`. Per the table above,
that `publisher` renders better for a preprint, so this path does not lose either.

### Effect on existing Zotero KBs

Re-running the export is enough; no migration command. Measured by exporting 13
Zotero item types on `main` and on this branch and diffing: **12 change in
BibTeX, 10 in CSL** (`journalArticle` is unchanged in both).

| itemType | BibTeX before → after | CSL before → after |
|---|---|---|
| `journalArticle` | `@article` / `journal` (unchanged) | `article-journal` / `container-title` (unchanged) |
| `magazineArticle` | `@misc` / `journal` → `@article` / `journal` | `document` → `article-magazine` |
| `newspaperArticle` | `@misc` / `journal` → `@article` / `journal` | `document` → `article-newspaper` |
| `encyclopediaArticle` | `@misc` / `journal` → `@incollection` / `booktitle` | `document` → `entry-encyclopedia` |
| `dictionaryEntry` | `@misc` / `journal` → `@incollection` / `booktitle` | `document` → `entry-dictionary` |
| `conferencePaper` | `journal` → `booktitle` | unchanged (`container-title`) |
| `bookSection` | `journal` → `booktitle` | unchanged (`container-title`) |
| `report` | `journal` → `institution` | `container-title` → `publisher` |
| `thesis` | `journal` → `school` | `container-title` → `publisher` |
| `book` | `journal` → omitted | `container-title` → omitted |
| `preprint` | `journal` → `howpublished` | `container-title` → `publisher` |
| `blogPost`, `webpage` | `journal` → `howpublished` | `container-title` → `publisher` |

The four type changes fill mappings that previously fell back to the default.
The field moves correct entries that carried a field their own type does not
define — `main` emitted `journal` on `@book`/`@incollection`/`@inproceedings`/
`@techreport`/`@phdthesis` as well.

## Further reading

The Korean [Zotero 가져오기](zotero-import.md) covers more than this page does:
the Local API setup walkthrough, the output format, the shape of the generated
source file, the `--pdf` and `--annotations` flows in detail, the optional
config file, and what is not supported yet.
