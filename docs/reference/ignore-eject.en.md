# Excluding and removing sources (`ignore` · `eject`)

> 🌐 **English** | [한국어](ignore-eject.md)

## Excluding sources from sync (`factlog ignore`)

`/factlog sync` re-extracts **every** source on each run. To keep specific
sources out of that — a draft, a work-in-progress, an external doc — add them to
the per-KB **sync-ignore list** (`policy/sync-ignore.md`). Ignored sources are
skipped by `/factlog sync`, `factlog ingest --scan`, coverage gap reporting, and
the wiki-exploration evidence in `/factlog ask`, **even when modified**. Their
already-merged facts are kept untouched (use `factlog eject` to actually remove
a fact).

```bash
factlog ignore drafts/*.md sources/wip-notes.md   # add pattern(s)
factlog ignore                                     # list patterns + what they match
factlog ignore --remove drafts/*.md               # remove a pattern
```

`policy/sync-ignore.md` is one glob per line (same lenient format as the other
policy files — `#` comments, `-` bullets, backtick-quoted entries; quote a
pattern that starts with `#` in backticks). A pattern matches a source by its
full ref (`sources/...` / `runs/sources/...`) or by its path within the source
root. Glob semantics: `*` and `?` stay within one path segment (do **not** cross
`/`), `**` crosses segments, and a trailing `/` means the whole subtree:

| Pattern | Matches |
|---------|---------|
| `drafts/*.md` | `sources/drafts/x.md` — but not `sources/drafts/sub/x.md` |
| `drafts/**` (or `drafts/`) | everything under `sources/drafts/` |
| `**/*.md` | any `.md` at any depth |

`factlog sources` marks ignored sources `[ignored]` and coverage reports them as
`excluded` rather than gaps.

## Removing a source (`factlog eject`) — the inverse of `ingest`

`factlog eject <source>` undoes an ingest: it deletes the `runs/sources/`
conversion, strips the source's extracted rows from `runs/*.json`, and retires
the facts that cite it. Name a source by filename, stem, or KB-relative path —
naming the binary original (e.g. `report.pdf`) also matches its
`runs/sources/<stem>` conversion; a bare stem matches every source with that
stem.

```bash
factlog eject report.pdf                 # delete conversion; mark citing facts superseded (kept for audit)
factlog eject report.pdf --purge         # delete the citing candidate rows instead of superseding them
factlog eject report.pdf --delete-original  # also delete the user's original under sources/
factlog eject report.pdf --dry-run       # show the planned changes, modify nothing
```

### How a source is named — filenames are wide, paths are narrow

| What you name | What it matches |
|---------|---------|
| stem `report` | **every** source with that stem, and their conversions |
| filename `report.html` | **every** source with that filename in any directory, and their conversions |
| path `sub/report.html`, `./report.html` | only the conversion made from the original at that path under `sources/` |
| KB-relative ref `sources/sub/report.html` | that original + the conversion made from it |
| absolute path `/kb/sources/sub/report.html` | the same (it reduces to the KB-relative ref) |

**A bare filename is not a path.** `factlog eject report.html` matches widely by
design, so it also catches `sources/sub/report.html`. To take only the top-level
one, name a path — `./report.html` for its conversion alone, or the KB-relative
ref `sources/report.html` to include the original itself (which is what makes
`--delete-original` actually delete it). The two table rows above are that
difference.

Given a path, only the conversion made from *that* path matches — conversions
mirror the original's subdirectory under `runs/sources/`, so
`factlog eject sub/report.html` deletes `runs/sources/sub/report.html.md` and
leaves `runs/sources/report.html.md`, made from a same-name original in another
directory, alone.

A **relative** path is compared **as written**: no `..` folding and no case
folding, so `sub/../report.html` and `SUB/report.html` match nothing and exit 1 —
even on a case-insensitive filesystem. A relative path is not resolved through
symlinks either, so one spelled through a symlinked directory name
(`link/report.html`) matches nothing — use the real path (`real/report.html`) or
an absolute path.

An **absolute** path, by contrast, is reduced to a KB-relative ref by asking the
filesystem directly: it walks up the path's ancestors looking for one that *is*
the same directory as `sources/` or the KB root. Because this compares
directories rather than strings, both of these work:

- a KB whose `sources/` is a symlink — naming the file through its real resolved
  path reduces to the same ref;
- a `--target` spelled in a different case from the argument on a
  case-insensitive filesystem. `Path.resolve()` does not canonicalise case, but
  the filesystem still reports one directory.

This test is **stricter than the one `ingest` uses.** `ingest` compares resolved
strings with `relative_to()`, so a case-different `--target` makes it treat a
file inside `sources/` as outside and write a **flat** conversion whose header
records only a filename. `eject` resolves that same argument into `sources/`, so
it cannot pair such a conversion — the header says nothing about which directory
the original was in. Deleting the original with `--delete-original` would orphan
it, so `eject` names the conversion that will be left behind and points at
`factlog eject --orphans` instead of guessing.

To delete the original too (`--delete-original`), name it by its KB-relative ref
(`sources/sub/report.html`) or by absolute path. A sources-relative path
(`sub/report.html`) names the **conversion** made from it — in that case
`--delete-original` reports 0 originals *and* prints the spelling that would
include one.

An original ingested from outside `sources/` (e.g.
`factlog ingest /elsewhere/report.html`) has no subtree to mirror, so its
conversion is written flat under `runs/sources/`. Naming that original by path
matches the flat conversion only: a conversion in a subdirectory cannot have come
from it.

**Unless `sources/` already holds an original of the same name, at any depth —
then nothing matches and it exits 1.** `ingest` records only the filename for an
original outside `sources/`, so a flat conversion cannot say *which* file of that
name it came from. If the KB holds one itself, that is the answer; `eject`
deletes files without a confirmation prompt, so it refuses the ambiguous request
instead. A competing original in a subdirectory (`sources/sub/report.html`)
counts just as much as a top-level one — this is the same test `--orphans` uses
to decide whether a flat conversion is paired.

### Removing a single fact (`--fact`)

When a source is fine but one extracted fact is wrong, retire just that fact —
the source's conversion and original stay in place:

```bash
factlog eject --fact "을서비스" "정식_운영" "2030.1"      # retire one fact (mark superseded)
factlog eject --fact "갑봇" "통합" "을서비스" --fact "값가" "대체" "값나"   # several at once
factlog eject --fact "을서비스" "정식_운영" "2030.1" --purge   # delete the candidate row instead
```

A fact is matched by its `(subject, relation, object)` triple across **all**
sources. The default `superseded` keeps `runs/*.json` untouched, so the
retirement is durable — a later `/factlog sync` re-asserts the fact from its
source but `merge_candidates` keeps it superseded. `--purge` instead deletes the
row and strips it from `runs/*.json`; if the source still asserts it, a re-sync
re-extracts it, so use the default to retire a fact for good. Fact mode and
source mode are mutually exclusive, and `--delete-original` is not valid with
`--fact`.

By default the retired facts are marked `superseded` (kept in
`facts/candidates.csv` for audit) and the original under `sources/` is **kept** —
so it would be re-converted on the next `/factlog sync`; pass `--delete-original`
to remove it too. `accepted.dl` is recompiled so the engine input drops the
retired facts immediately.

A `runs/sources/` conversion is tied to the original that produced it via the
ingest provenance header, so even when two originals share a stem,
`eject report.docx` never disturbs `report.pptx`'s conversion. `pages/` are not
regenerated by `eject` — run `/factlog sync` to reconcile them. The default
`superseded` mark is a current-state retire: if you keep a **text** original
under `sources/`, the next `/factlog sync` re-extracts and re-asserts its facts —
to remove a source for good, pass `--purge` and/or `--delete-original`.
