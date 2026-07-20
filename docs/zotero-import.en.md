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

## Further reading

The Korean [Zotero 가져오기](zotero-import.md) covers more than this page does:
the Local API setup walkthrough, the output format, the shape of the generated
source file, the `--pdf` and `--annotations` flows in detail, BibTeX export
(`factlog export --bibtex`), the optional config file, and what is not supported
yet.
