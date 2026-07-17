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
