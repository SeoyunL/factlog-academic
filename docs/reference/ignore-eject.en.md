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

`--purge` is **refused** (exit 1, nothing changed) when a run row it would strip
belongs to a fact `facts/candidates.csv` carries no row for — the fact's last
copy, which `--purge` would remove with no tombstone. Fact mode reaches such a row
because it matches the triple across every source. The refusal names the way out:
merge first if the source is still on disk (the table is merely stale), or
`factlog eject --orphans` if it is gone (that retires it as a tombstone), then
re-run the purge. There is no `--force`: the point of the refusal is that the fact
becomes visible in the table once, before anyone deletes it.

By default the retired facts are marked `superseded` (kept in
`facts/candidates.csv` for audit) and the original under `sources/` is **kept** —
so it would be re-converted on the next `/factlog sync`; pass `--delete-original`
to remove it too. `accepted.dl` is recompiled so the engine input drops the
retired facts immediately.

#### Exit codes

`eject`'s exit 1 covers **several states, two of which are opposites** — "nothing
was destroyed" (held back) and "a fact's last copy was destroyed" (no tombstone
possible). Read this table before chaining on it in a script. It can also accompany
a successful cleanup: one held-back fact makes the run exit 1 even though a
tombstone was written and run rows were stripped.

| rc | State | What happened to the KB |
|---|---|---|
| 0 | Completed (including `no orphaned sources found`) | Cleaned as asked; a last copy is kept as a `superseded` tombstone |
| 1 | `nothing to eject` / `no candidate fact matches ...` | **Nothing changed** |
| 1 | `refusing --purge` (a last copy) | **Nothing changed** |
| 1 | `refusing --delete-original` (unattributable conversions) | **Nothing changed** |
| 1 | `NOT stripping the run row(s) ...` (held back, case (4)) | The rest was cleaned; the **held-back run rows are intact** — restore the source and the fact comes back |
| 1 | `... stripped with no tombstone.` (case (3)) | Cleaned, and **that fact left the KB** — not recoverable |
| 1 | `compile_facts failed` | The table changed but `accepted.dl` is stale — re-run |
| 2 | Usage error (mode mixing, no selector, `--delete-original` in fact mode) | **Nothing changed** |

A `runs/sources/` conversion is tied to the original that produced it via the
ingest provenance header, so even when two originals share a stem,
`eject report.docx` never disturbs `report.pptx`'s conversion. `pages/` are not
regenerated by `eject` — run `/factlog sync` to reconcile them. The default
`superseded` mark is a current-state retire: if you keep a **text** original
under `sources/`, the next `/factlog sync` re-extracts and re-asserts its facts —
to remove a source for good, pass `--purge` and/or `--delete-original`.

### If you deleted a source file directly, without `eject`

Deleting a file under `sources/` from a file manager (or with `rm`) leaves the
rows citing it in `runs/*.json`. Every later merge drops those rows before
writing `facts/candidates.csv`, and says so on stderr:

```
  skip row: source 'sources/doomed.md' not found in sources/ ... (3 rows)
```

That warning is gone the moment the terminal scrolls, and `facts/candidates.csv`
is the state AFTER the drop — so the rows are not caught as orphan citations (a
fact citing a file that is not on disk) either. The coverage report therefore
reads `runs/*.json` directly and reports this state on its own:

```
coverage: 12 source(s); 11 covered, 1 text gap(s), 0 binary needing conversion, 0 orphan citation(s), 1 run-cited source(s) missing
  RUN ROWS cite a missing source (dropped at merge, 3 row(s)): sources/doomed.md
```

The field is omitted from the summary line when there is nothing to report, and
it never affects the exit code (`--strict` still fires on text gaps only).

#### How you clean it up depends on which eject route retires the ref

The report says which of the four each source is, so follow the line it prints.

**(1) The row is still in `candidates.csv`.** A row a human has ruled on
(`confirmed`/`accepted`/`needs_review`) makes the
[#218](https://github.com/SeoyunL/factlog-academic/issues/218) ratchet refuse the
rebuild, so the ghost row stays in the table; a `superseded` ghost is always in
this class too, being retained for audit. `factlog eject --orphans` sees that
source and retires it in one command.

```
  RUN ROWS cite a missing source (1 row(s); candidates.csv still carries rows for it): sources/doomed.md
  run rows cite 1 missing source(s) (1 row(s) total) that candidates.csv still carries; retire them with `factlog eject --orphans`
```

> The automatic scan SKIPS a ref it has nothing left to do for — no file on disk
> to delete, no COMPLETE `runs/*.json` row (one with subject, relation, object
> and source all filled in, the kind merge actually writes), and citing rows that
> are all `superseded`. So re-running the command ends with `no orphaned sources
> found`, which is how you confirm the KB is clean. Case (1) above still has run
> rows, so it does not fall here. A ref whose only run rows are INCOMPLETE is
> skipped too — merge discards those rows (`skip incomplete row in ...`), so
> nothing can come back from them. To strip those rows from `runs/*.json` anyway,
> or to remove the tombstones as well, pass `--purge` (`--orphans --purge` works)
> or name the ref directly — an explicitly named ref always matches.

**(2) The row is already gone.** An unruled (`candidate`) row is usually rebuilt
away silently. `eject` builds its cited set from `facts/candidates.csv` **and**
`runs/*.json`, so `--orphans` cleans this source too. Because the table holds no
row for it, the run row IS the fact's last copy: eject writes a `superseded`
tombstone into `candidates.csv` BEFORE stripping that row. The row you find in the
table afterwards is one this command created, not one that survived.

> What decides the class is whether the ROW SURVIVED, not the status itself. The
> ratchet refuses the WHOLE rebuild rather than a row, so one ruled-on ghost in
> the same merge keeps the unruled ghosts too, and they are reported as (1). The
> report judges each source by its actual state — follow the line it prints
> instead of predicting it from the status.

```
  RUN ROWS cite a missing source (dropped at merge, 3 row(s)): sources/doomed.md
  run rows cite 1 missing source(s) (3 row(s) total) whose only copy is in runs/*.json; `factlog eject --orphans` retires them, writing a `superseded` tombstone into candidates.csv first
```

> Because this is the last copy, **`--purge` is refused** (exit 1, nothing
> changed): it is the one route that would delete the fact leaving not even a
> tombstone. To remove it from the table as well, go in two passes — the fact
> being visible in `candidates.csv` in between is the point of the refusal.
>
> ```
> factlog eject --orphans --target <kb>            # writes superseded tombstones
> factlog eject --orphans --purge --target <kb>    # removes them
> ```
>
> Restoring the deleted file under `sources/` and re-running the merge still works
> as a recovery path — and it is the only one that brings the fact back, so try it
> first if the deletion was a mistake.

**(3) The ref is outside the two source roots.** A path under neither `sources/`
nor `runs/sources/` (a malformed citation such as `ghosty.md` or `/etc/passwd`) is
never auto-selected by `--orphans`. That rule stays: a command nobody aimed must
not delete a file nobody named. Naming the ref cleans it.

```
  RUN ROWS cite a missing source (dropped at merge, 2 row(s); outside the source roots): ghosty.md
  run rows cite 1 missing source(s) (2 row(s) total) that --orphans will not auto-select (the ref is outside sources/ and runs/sources/); name each one: `factlog eject <ref>` — which strips those rows with NO tombstone, since candidates.csv cannot hold such a source, and exits 1 to say the fact is gone
```

> This class is stripped with **no tombstone**, and the command **exits 1**: a
> `candidates.csv` source has to start with one of the two roots or `validate`
> rejects the row, so there is no row to leave behind and the fact is simply gone.
> eject says so on stderr and in the exit code. An INCOMPLETE run row (one of
> subject, relation, object, source empty) is stripped without a tombstone for the
> same reason — merge discards such a row too.

**(4) `candidates.csv` holds the ref under a whitespace-differing `source`.**
eject's `candidates.csv` matcher does not strip that value, while merge and eject's
own runs matcher do. On a row where those two disagree, eject neither retires the
row (no match) nor writes a tombstone (the table does hold the fact) — so it
**leaves the run rows in place and exits 1**.

```
  RUN ROWS cite a missing source (1 row(s); candidates.csv holds it under a whitespace-differing source): sources/ghosty.md
  run rows cite 1 missing source(s) (1 row(s) total) that candidates.csv holds under a `source` differing only by whitespace; `factlog eject --orphans` LEAVES those run rows in place (exit 1) rather than delete what merge rebuilds from — fix the whitespace in candidates.csv, then re-run it
```

> Stripping those rows would delete the artifact
> [#218](https://github.com/SeoyunL/factlog-academic/issues/218) names as the
> recovery path, leaving no way to re-assert a row a human ruled on: every later
> merge REFUSES the rebuild, and the only exit is `--allow-delete`, which kills the
> row. What needs fixing is the whitespace in `candidates.csv`, not the command.

A `runs/*.json` that cannot be read is left out of the counts, and the report
says so on stderr rather than skipping it in silence (merge cannot read it
either):

```
  skipped unreadable runs/2026-01-02-r.json — its rows are NOT in the counts above (merge cannot read it either)
```
