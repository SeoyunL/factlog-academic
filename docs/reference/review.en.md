# Reviewing facts

> 🌐 **English** | [한국어](review.md)

## Reviewing facts (`factlog review` / `accept` / `reject`)

Extraction marks facts `candidate` or `needs_review`; only `confirmed`/`accepted`
facts become engine input. Promote or retire them without hand-editing
`facts/candidates.csv`:

```bash
factlog review                       # list the pending queue (candidate + needs_review)
factlog review --status needs_review # narrow to one pending status
factlog accept Acme uses FastAPI     # pending → accepted (compiled into accepted.dl)
factlog accept Acme                  # accept every pending fact about a subject ('-' wildcards a position)
factlog reject Acme uses Datadog     # pending → superseded (retired, kept for audit)
factlog accept Acme uses FastAPI --dry-run
```

`accept`/`reject` change **only pending rows**; a `confirmed`/`accepted`/
`superseded` match is reported and left untouched (use `factlog eject` to retire
a non-pending fact). Both recompile `accepted.dl`.

`accept`/`reject` record the decision in the backing `runs/*.json` as well as in
`candidates.csv`: merge rebuilds `candidates.csv` from `runs/*.json`, so a
decision written only to the CSV would vanish silently on the next sync. **That
record applies only to the rows the gate actually changed.** A "row" here is
merge's own fact identity — `(subject, relation, object, source file)`, with any
`#anchor` ignored — not the triple alone. So when the same triple is asserted by
two documents, deciding one document's row leaves the other document's evidence
row untouched, and rows reported as "non-pending skipped" stay as they are in
`runs/*.json` too. An `amount` object is compared in merge's canonical form
`amount(N,"unit")`, so `amount(7,억)` and `amount(7,"억")` are one fact.

Subject, relation, object and source are all **normalised to NFC** for both
comparison and storage. Two values that look identical but differ only in Unicode
form (NFC vs NFD) are therefore **one fact**: accepting one reaches the evidence
row behind the other spelling too, and `candidates.csv` keeps a single row folded
to NFC. Pasted text and macOS filenames do mix the forms in practice, but merge
folds them onto one fact, so there is no manual reconciliation to do. (This
identity also matches the engine's grouping axes, which fold to NFC as well.) To
re-fold a `candidates.csv` built under the earlier spelling policy, use the
one-shot `factlog migrate-unicode` command. It reports conflicts by default (safe);
only `--resolve-status=priority` rewrites `candidates.csv` immediately (no
interactive confirmation). The command targets the active KB when `--target` is
omitted, so confirm the target with `--target` before using priority. Priority can
REVIVE a retired (superseded) row by folding it into a confirmed/accepted one, so
handle any group whose retirement must stand with `amend` instead. It also folds
colliding groups only, leaving a lone NFD row as-is — to complete the all-fields
NFC unification, re-merge (`/factlog sync` or `merge_candidates.py`).

Boundary: repairing drift — `confirmed` in `candidates.csv` while `runs/*.json`
still says `candidate`, as in a KB predating #233 — is not a side effect of
`accept`/`reject`. They write down the decision they just made, nothing else;
recovering drifted rows is a separate command's job.

To **correct** a fact's value (not just its status), use `factlog amend`:

```bash
factlog amend Widget codename Draft --set-object Falcon --set-note "name finalized" --accept
factlog amend Acme uses FastApi --set-object FastAPI    # fix a typo
```

The positional triple identifies the fact (exact match); `--set-subject` /
`--set-relation` / `--set-object` / `--set-note` give the new values (at least
one, or `--accept`). amend updates **both** `candidates.csv` and the backing
`runs/*.json` so the edit survives `/factlog sync` (a fact's value lives in
`runs/*.json` — merge rebuilds `candidates.csv` from it). `--accept` also
promotes to `accepted`. Confidence is not editable. `--dry-run` previews.

### Kinds of status

A fact's `status` falls into three classes.

| Class | Status values | Meaning |
|-------|---------------|---------|
| **pending** | `candidate`, `needs_review` | Extracted, but still waiting on a human decision. Shows up in the `factlog review` queue. |
| **engine input** | `accepted`, `confirmed` | A fact a human confirmed. **Only these two statuses compile into `accepted.dl`** and become engine input. |
| **retired** | `superseded` | A fact that has stepped down. Kept in `candidates.csv` for audit, but it is not engine input and is ignored by conflict detection. |

### Status transition table

| Current status | `accept` | `reject` | `amend --set-*` | `amend --accept` |
|----------------|----------|----------|-----------------|------------------|
| `candidate` | → `accepted` | → `superseded` | value corrected (status kept) | value corrected + → `accepted` |
| `needs_review` | → `accepted` | → `superseded` | value corrected (status kept) | value corrected + → `accepted` |
| `accepted` | no change (reported, exit code 1) | no change (reported, exit code 1) | value can be corrected | value corrected (already `accepted`) |
| `confirmed` | no change (reported, exit code 1) | no change (reported, exit code 1) | value can be corrected | value corrected + → `accepted` |
| `superseded` | no change (reported, exit code 1) | no change (reported, exit code 1) | **not a target** — `no fact matches` (exit code 1) | **not a target** — `no fact matches` (exit code 1) |

How to read it:

- **`accept`/`reject` only create edges out of a pending status.** If every
  matching row is non-pending, they change nothing and end with a notice and exit
  code 1.

  ```text
  factlog accept: 1 matching row(s) are not pending (already confirmed/accepted/superseded);
  nothing to change. Use `factlog eject` to retire a non-pending fact.
  ```

- **`amend` deals in values, not status.** That is why it can fix a typo even in
  an already-confirmed `accepted`/`confirmed` fact — territory `accept`/`reject`
  cannot touch.
- **A `superseded` row is not an `amend` target.** Re-targeting the tombstone a
  previous `amend` left behind would revive the retired value, so `amend` only
  looks for non-retired rows. With no live matching row, the result is
  `no fact matches`.

Transitions that **do not** happen are in the table too. No command demotes
backwards, e.g. `accepted` → `candidate`, and there is no edge back to a pending
status.

Exit codes when there is no transition (no matching row, non-pending) or the
arguments are wrong are as follows.

| Situation | Exit code |
|-----------|-----------|
| transition succeeded | 0 |
| `--dry-run` (preview only) | 0 |
| no row matches the triple (`no fact matches`) | 1 |
| rows match but all are non-pending (`nothing to change`) | 1 |
| status was saved but recompiling `accepted.dl` failed | 1 |
| argument error (more than 3 triple terms, none given, `amend` without `--set-*`/`--accept`) | 2 |

Even when the recompile fails, **the status change itself has already been saved
to `candidates.csv`**; just rebuild `accepted.dl` with `/factlog check`.

> **Durability:** a human `accept` (and `amend --accept`) is preserved across
> re-merge the same way `reject`/`superseded` is — `/factlog sync` will not
> revert your decisions.
