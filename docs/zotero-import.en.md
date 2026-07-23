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

All three selectors reject a value the library does not hold — an unknown
collection, tag, or item key is an error (exit 1) — so a typo cannot pass CI as a
successful import of nothing. What the error says differs by selector: an unknown
collection or tag lists the names that *are* available (up to 20, with the rest
summarised as `... (N more)`), while `--items` lists only the keys the library
does not hold, since reciting every key in a library would help nobody.

A selector that exists but matches nothing is still a success (exit 0, 0 items).
The distinction is *absent* versus *empty*. A tag carrying no bibliographic items
is one such case; so is an `--items` key naming a PDF attachment or a note. That
key does exist, so it is not an error — it is filtered out for not being a
bibliographic item, which is what `1 requested` alongside `0 item(s)` means. You
get this by copying an attachment's key out of the Zotero UI.

`--items` is all-or-nothing: if one key is missing, the valid keys alongside it
are not imported either. For a script feeding keys in batches, one typo now stops
the whole batch — split the keys across calls if you would rather import whatever
resolves.

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
| informal | preprint, dataset, software, unmapped | `howpublished` | `container-title` |
| a series | a whole book | `series` | `collection-title` |

`@inproceedings`/`@incollection` require `booktitle`, so the previous `journal`
placement both misfiled the venue and triggered `Warning--empty booktitle`.

**No role discards the value.** All six *move* the venue to a differently-named
field; none deletes it. A whole book has no containing venue, but a value in
that position names the series the book belongs to, so it goes to
`series`/`collection-title`. A misfiled value can be recovered by hand; a
dropped one cannot.

**Why informal venues use CSL `container-title`.** Such a record is CSL-typed
`article` (a preprint — CSL 1.0.2 has no `preprint` type, and #60 forbids
retyping a deposit once `journal` records where it landed). Rendering was
measured first, to see whether it forces the choice. It does not. One preprint
carrying `Nature 585, 357 (2020)`, rendered with `pandoc 3.10 --citeproc`,
venue present (Y) or lost (N):

| Variable | chicago | apa | ieee | nature | ama | Total |
|---|---|---|---|---|---|---|
| `container-title` | Y | Y | **N** | Y | Y | 4/5 |
| `publisher` | Y | Y | Y | **N** | Y | 4/5 |

**An exact tie.** IEEE drops `container-title`; Nature drops `publisher` (its
`type="article"` branch never references it). Preprint status also ties at 4/5
either way once `genre` is emitted.

Rendering therefore cannot decide it, and the remaining criterion is what the
value *is*: an arXiv `journal_ref` or a Zotero preprint's `publicationTitle` is
**a periodical's name, not a publisher's**. `publisher` would be a false
statement that happens to print; `container-title` is a true one that IEEE
happens to ignore. It is also what `main` already emitted, so CSL output moves
less.

**Both remaining losses, stated.** Having chosen `container-title`, **a
preprint's venue does not render under IEEE.** Had we chosen `publisher`, it
would have vanished under Nature instead. Neither option is 5/5.

On the BibTeX side `howpublished` is the only venue field `@misc` defines, so it
stays. The two formats thus use deliberately different field names for this role
(as they already do for dataset/software). A pandoc BibTeX->CSL round trip
consequently yields `publisher`, disagreeing with the CSL we emit directly; the
export we emit is the accurate one, and a lossy third-party conversion is not a
reason to make it wrong. Classic BibTeX styles (plain/unsrt/alpha) silently drop
`journal` but render `howpublished`, so the BibTeX side is an improvement.

**`genre: "Preprint"`.** With no CSL `preprint` type, the status travels in
`genre`. Measured: APA gains `[Preprint]` (omitted without it) and the other
styles are unchanged — a pure gain. Datasets, software and unmapped types share
the venue role but are not preprints, so they get no `genre`.

### Effect on existing Zotero KBs

Re-running the export is enough; no migration command. Measured by exporting 13
Zotero item types on `main` and on this branch and diffing: **12 change in
BibTeX, 8 in CSL** (`journalArticle` is unchanged in both).

| itemType | BibTeX before → after | CSL before → after |
|---|---|---|
| `journalArticle` | unchanged (`@article` / `journal`) | unchanged (`article-journal` / `container-title`) |
| `magazineArticle` | `@misc` / `journal` → `@article` / `journal` | `document` → `article-magazine` |
| `newspaperArticle` | `@misc` / `journal` → `@article` / `journal` | `document` → `article-newspaper` |
| `encyclopediaArticle` | `@misc` / `journal` → `@incollection` / `booktitle` | `document` → `entry-encyclopedia` |
| `dictionaryEntry` | `@misc` / `journal` → `@incollection` / `booktitle` | `document` → `entry-dictionary` |
| `conferencePaper` | `journal` → `booktitle` | unchanged (`container-title`) |
| `bookSection` | `journal` → `booktitle` | unchanged (`container-title`) |
| `report` | `journal` → `institution` | `container-title` → `publisher` |
| `thesis` | `journal` → `school` | `container-title` → `publisher` |
| `book` | `journal` → `series` | `container-title` → `collection-title` |
| `preprint` | `journal` → `howpublished` | `container-title` kept, `genre` added |
| `blogPost`, `webpage` | `journal` → `howpublished` | unchanged (`container-title`) |

The four type changes fill mappings that previously fell back to the default.
The field moves correct entries that carried a field their own type does not
define — `main` emitted `journal` on `@book`/`@incollection`/`@inproceedings`/
`@techreport`/`@phdthesis` as well.

## Further reading

The Korean [Zotero 가져오기](zotero-import.md) covers more than this page does:
the Local API setup walkthrough, the output format, the shape of the generated
source file, the `--pdf` and `--annotations` flows in detail, the optional
config file, and what is not supported yet.
