# Typed relations (`policy/typed-relations.md`)

> 🌐 **English** | [한국어](typed-relations.md)

Some relations carry a literal object that should be **compared**, not just
matched — so the deterministic engine can order it, threshold it, or range over
it (e.g. "launched after 2030", "rank <= 3"). Declare such relations in
`policy/typed-relations.md`. Because the object is a literal, the relation should
ALSO be declared in `policy/attribute-relations.md`.

One declaration per line:

```
- `relation name` : <type> as <ascii_alias>
```

`<ascii_alias>` names the engine side-relation that holds the comparable value.
It is an author-chosen ASCII identifier (`[A-Za-z_][A-Za-z0-9_]*`) so it stays a
legal engine name even when the relation name is non-ASCII. Quote a relation name
containing spaces in backticks.

The four types:

- `date` — `2030.1` / `2030-01-15` → sortable yyyymmdd. **Engine-projectable**
  (ordering / threshold / range).
- `ordinal` — `rank 3` / `3rd` → int rank. **Engine-projectable**.
- `amount` — `100억` / `1,000원` → integer base unit. **Engine-projectable**.
  Needs a unit table; supply one inline at the end of the line:
  `: amount as <alias> (억=1e8, 만=1e4, 원=1)` (values must be positive ints).
  Omit the clause to use the built-in default unit table.
- `number` — `1,000` / `3.5` → numeric magnitude. **Engine-projectable**: scaled
  ×1000 (3 decimal places) to a sortable int64. ⚠️ Thresholds in comparison
  predicates MUST be written in **scaled units**: `version >= 2.0` →
  `version_num(S, V), V >= 2000`. Precision beyond 3 decimals rounds
  (ROUND_HALF_UP).

Extractors may emit typed literal objects as compact compound terms when that
preserves structure better: `date(2030)`, `date(2030,1)`, `date(2030,1,15)`,
`number(2.5)`, `ordinal(3)`, `amount(100,"억")`. The `relation/3` object stores
that term as a string, and the typed side-relation projects the comparable
scalar.

A date compound term takes year, month or day precision. Missing parts default
to `01`, so `date(2030)` sorts as `20300101`, the start of the year — the same
convention that already fills in a missing day (`2030.1` → `20300101`). A
threshold like `D >= 20300101` therefore includes a year-only fact. The
human-readable form appended to an answer, by contrast, carries only the
precision the term actually has: `date(2030)` shows as `2030`, never padded out
to `2030-01`. A bare `2030` with no `date(…)` wrapper still does NOT parse as a
date — with neither a separator nor the wrapper it is indistinguishable from a
plain number.

Digits must be **ASCII**. A value carrying full-width digits — `１００억`,
`date(２０２０,１)`, the half-and-half `1２3억` — does NOT parse as any of
date/number/ordinal/amount. It takes the ordinary "does not parse → load
untyped" path and surfaces as a `typed-relations: … does not parse as …`
warning. Full-width is not folded to ASCII silently, because folding would
rewrite the stored fact string — the fix is to correct the source to ASCII and
re-collect. Under a relation that is not declared typed the parsers never run at
all, so there the two spellings simply stay separate values.

⚠️ **Migrating an existing KB.** If full-width values collected before this rule
are still in the KB, `tools/check_conflicts.py` may now exit **1** — a gate
failure, not a warning. `１００억` and `100억` used to fold onto the same scalar
and count as one value; the full-width one now keys on its raw string, so for the
same subject a single-valued relation sees two values.

That gate failure is loud; there is also a **quiet** one. If an existing KB holds
a full-width amount compound term (`amount(１００,"억")`), a query written without
the quotes — `amount(１００,억)` — now **misses silently**, because a full-width
term is no longer a valid amount and so no longer folds to the same canonical
form as the stored value. That miss is indistinguishable from an engine-verified
"no such fact", which makes it harder to notice than the failure.

Both cases clear the same way: correct the source of those facts to ASCII and
re-collect.

`factlog vocab` shows declared typed relations with a `[typed:<type>]` tag (e.g.
`[attribute, typed:date]`).
