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
  (ordering / threshold / range). Missing parts default to `01`. For a
  year-only value use the compound term `date(2030)` (→ `20300101`); a **bare
  `2030`** does not parse, because without a separator or the `date(…)` wrapper
  it is indistinguishable from a number.
- `ordinal` — `3rd` / `3위` / `제3호` → int rank. **Engine-projectable**. In the
  **prose form** a rank unit must follow the number — Korean
  `호`/`위`/`번`/`차`/`등`/`째` (with an optional leading `제`), or English
  `st`/`nd`/`rd`/`th`. Whitespace between the number and the unit is allowed
  (`3 위`, `3 rd`). A prose value with no unit does not parse and stays untyped:
  `3`, `제3` and `rank 3` all fail. The compound term `ordinal(3)` described
  below is a **separate path** that carries no unit and is not bound by this
  constraint.
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
preserves structure better: `date(2030)`, `date(2030,1)`, `date(2030,1,15)`, `number(2.5)`,
`ordinal(3)`, `amount(100,"억")`. The `relation/3` object stores that term as a
string, and the typed side-relation projects the comparable scalar.

`factlog vocab` shows declared typed relations with a `[typed:<type>]` tag (e.g.
`[attribute, typed:date]`).
