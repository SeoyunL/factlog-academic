# Source file formats

> 🌐 **English** | [한국어](sources.md)

`/factlog sync` extracts facts by reading each file under `sources/` **as text,
in-session**. The bundled engine (`merge_candidates.py`) tracks every file as a
source *path* but never parses contents — so a file is only ingested if its text
can be read during extraction. A binary original (e.g. `.docx`) therefore yields
no facts on its own.

| Format | Status | Notes |
|--------|--------|-------|
| `.md`, `.markdown`, `.txt` | **Directly supported** | UTF-8 text, read verbatim. This is what every extraction reference assumes. |
| Other UTF-8 text (`.rst`, `.org`, `.csv`, source code) | Supported as plain text | No special parsing; treated as raw text. |
| `.docx`, binary `.pdf`, `.odt`, `.epub`, `.html`, `.rtf` | **Auto-converted** | `factlog ingest` converts these to text via pandoc / textutil / pdftotext. |
| `.hwpx` (Hancom OWPML) | **Auto-converted** | Built-in extractor (no external tool) — reads the zip's `Contents/section*.xml` text. |
| `.hwp` (legacy Hancom, HWP 5.x) | **Auto-converted** | Via `hwp5html` (pyhwp) → pandoc → markdown, tables preserved. Needs `pip install pyhwp` + pandoc; if absent, reported with a hint. |
| `.pptx` (PowerPoint) | **Auto-converted** | Built-in extractor (no external tool) — reads on-slide text from the zip's `ppt/slides/slideN.xml`, slides in order, one block per slide. Speaker notes are excluded; table cells flatten to one line per cell (row/column grouping not preserved). |
| `.xlsx`, images | **Not converted** | No bundled converter — reported with a hint; convert by hand. |

### Per-format converters and prerequisites

What "auto-converted" above actually requires differs per format. Each extension
has a **converter chain**, and factlog walks that chain in order and picks the
**first available** converter (a built-in is always available; an external tool
only when it is on `PATH`).

| Format | Converter chain (in order) | Needs installing | Output |
|--------|----------------------------|------------------|--------|
| `.docx`, `.odt` | pandoc → textutil | pandoc, or macOS's textutil | `.md` (pandoc) / `.txt` (textutil) |
| `.html`, `.htm` | pandoc → textutil | pandoc, or macOS's textutil | `.md` / `.txt` |
| `.epub` | pandoc | pandoc | `.md` |
| `.rtf` | textutil | **macOS only** — textutil ships with macOS | `.txt` |
| `.pdf` | pdftotext | poppler (`pdftotext`) | `.txt` |
| `.hwpx` | `factlog-hwpx` (built-in) | nothing — standard library only | `.md` |
| `.pptx` | `factlog-pptx` (built-in) | nothing — standard library only | `.md` |
| `.hwp` | `factlog-hwp` (built-in, orchestrates external tools) | **both** `hwp5html` (`pip install pyhwp`) **and** pandoc | `.md` |
| `.xlsx`, `.png`, `.jpg`, `.jpeg` | none | — | not converted |

The install hints are given per tool — pandoc as `brew install pandoc` (or
<https://pandoc.org>), pdftotext as poppler (`brew install poppler`), and textutil
ships with macOS.

The **fallback chain** on `.docx`/`.odt`/`.html`/`.htm` matters. With no pandoc,
macOS still converts them via textutil (but the result lands as plain `.txt`
rather than markdown, so table structure is not preserved). `.epub`/`.rtf`/`.pdf`,
by contrast, have a single-tool chain — without that tool they are not converted.

### What happens when no converter is available

Whether `ingest` treats a missing converter as a failure depends on **how the file
was named**.

| Situation | `--scan` (auto-discovered) | Explicit `ingest <file>` |
|-----------|----------------------------|--------------------------|
| no tool from the chain on `PATH` (e.g. `.docx` without pandoc) | counted `skipped`, **run succeeds** (exit code 0) | counted `failed`, **exit code 1** |
| format with no converter at all (`.xlsx`, images) | `skipped`, exit code 0 | `failed`, exit code 1 |
| built-in cannot find its external tool (`.hwp` without pyhwp/pandoc) | `skipped`, exit code 0 | `failed`, exit code 1 |

Either way **the reason is always printed to stderr**, so nothing passes silently.

```text
factlog ingest: no converter on PATH for .pdf (tried: pdftotext). install poppler (e.g. `brew install poppler`)
factlog ingest: skip y.xlsx (.xlsx): no built-in converter; export sheets to .csv and place those in sources/
factlog ingest: 0 converted, 2 skipped, 0 failed
```

The asymmetry is deliberate. `--scan` runs as the pre-step of `/factlog sync`, so
one unconvertible file must not fail the whole sync. Conversely, if you named a
file yourself that is a request to process it — so not processing it is a failure.

`--scan` additionally counts and surfaces two more classes separately — a file
whose **extension is a conversion target but whose content is not binary** (e.g. a
plaintext `.hwpx`) and a **0-byte file**. Neither is converted; both are reported
as `ignored` (the former is read directly as text by sync if it is a valid source).

`factlog ingest` writes the converted text into the KB's **`runs/sources/`**
directory (alongside the other generated run artifacts) — **never into
`sources/`**, which stays the user's originals. A conversion is named by the
original's **full filename (extension included) + the converter's suffix**
(`report.hwpx` → `runs/sources/report.hwpx.md`, `report.pptx` →
`runs/sources/report.pptx.md`), so two originals in one folder that share a stem
and differ only in extension never **collide on one conversion and lose the
loser**. A nested original mirrors its subdirectory
(`sources/sub/report.pdf` → `runs/sources/sub/report.pdf.md`), so same-name
files in different folders never collide either. The original is left
untouched and the conversion carries a provenance header (source, converter,
date). Both `sources/` and `runs/sources/` are valid source roots that
extraction reads.

> **Upgrading (#213):** the conversion filename rule changed. It used to use the
> original's **stem** only (`report.pdf` → `runs/sources/report.md`); it now uses
> the original's **full name** (`runs/sources/report.pdf.md`), so `report.hwpx`
> and `report.pptx` in one folder are each preserved as their own conversion.
> A legacy stem-named conversion (`runs/sources/report.md`) in an already-ingested
> KB is **still paired with its original through a stem-based fallback** by
> `factlog sources` / `coverage` / `status`, so it is not silently lost. To move
> to the new layout, re-run `factlog ingest --scan --force` (then clean up any
> leftover legacy conversions with `factlog eject --orphans`). A KB whose stems
> collided in particular must be re-ingested to restore the originals that were
> being lost.

> **Upgrading:** subdirectory mirroring is newer than the original flat layout.
> A KB ingested earlier has flat conversions (`runs/sources/report.md`) for
> nested originals; those no longer pair, so a nested binary may reappear as a
> coverage/`factlog sources` gap. Re-run `factlog ingest --scan --force` to move
> conversions to their mirrored paths (then delete any stale flat conversions).
> Top-level (non-nested) sources are unaffected.

```bash
factlog ingest report.docx --target ~/wiki   # → ~/wiki/runs/sources/report.docx.md (pandoc)
factlog ingest --scan --target ~/wiki        # auto-convert every binary under sources/
```

`/factlog sync` runs `factlog ingest --scan` as its first step, so binaries you
drop in `sources/` are converted automatically (idempotently — unchanged files
are skipped). If a binary has no `runs/sources/` conversion, `merge_candidates.py`
warns so the silent non-ingestion is visible.
