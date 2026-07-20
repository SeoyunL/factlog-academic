# Eject and conversion attribution — edge cases

The reference page [Removing a source](reference/ignore-eject.en.md#removing-a-source-factlog-eject--the-inverse-of-ingest)
(한국어: [소스 제거](reference/ignore-eject.md#소스-제거-factlog-eject--ingest-의-역연산))
covers everyday `factlog eject`. This document holds the attribution edge cases and
the flat-layout migration — details that matter only when a `runs/sources/`
conversion cannot be tied back to the original that produced it.

## Conversion attribution

A `runs/sources/` conversion is tied to the original that produced it via the
ingest provenance header, so even when two originals share a stem,
`eject report.docx` never disturbs `report.pptx`'s conversion.

Ejecting by **path** (`eject sub/report.docx`) matches the conversion whose
recorded origin *is that path*. A conversion made **before** conversions mirrored
their original's subdirectory records only a basename, so it cannot be attributed
to a path — and that state is indistinguishable from a conversion made from a file
outside `sources/` (the `factlog ingest report.docx --target ~/wiki` form) or from
an original since deleted.

`eject` will **not** guess — whether you give a path (`eject sub/report.html`) OR a
bare name (`eject report.html`): it leaves such a conversion in place and names it
on stderr, including when nothing else matched. (A bare name still ejects an
original that bears it, and an *attributable* conversion — one whose origin can be
tied to a real source — like any other; only the un-attributable flat conversion is
left.) Remove it by naming the conversion directly — `eject runs/sources/report.md`.

While such a conversion is still on disk and the request matched no conversion of
its own, `--delete-original` **refuses outright** (exit 1, nothing deleted):
deleting the original while leaving a conversion we could not attribute would
strand that conversion's facts with no source file.

## Flat-layout migration

Two layout changes made older conversion filenames "flat" relative to today's.

**Filename convention (#213).** Conversions used to be named by the original's
**stem** (`report.pdf` → `runs/sources/report.md`); now they keep the original's
**full name + the converter's extension** (`report.pdf` → `runs/sources/report.pdf.txt`,
`report.docx` → `runs/sources/report.docx.md`), so `report.hwpx` and `report.pptx`
in one folder no longer collide.

**Subdirectory mirroring.** A nested original now mirrors its subdirectory under
`runs/sources/`, rather than producing a flat name.

An old-stem or flat conversion (`runs/sources/report.md`) is still recognised by
`factlog sources` / `coverage` / `status` through a stem-based fallback, so it is
not silently lost. To move to the new layout, re-run `factlog ingest --scan
--force`. That **adds** a new-name conversion beside the old flat one — it does not
migrate the old one — so delete the stale flat conversion by naming it directly:

```bash
factlog eject runs/sources/report.md
```

(`--orphans` will not clean it: the original still exists, so it is not an orphan.)
Top-level, single-stem sources are unaffected; a KB whose stems used to collide
must be re-ingested to recover the originals that the old flat naming had dropped.
