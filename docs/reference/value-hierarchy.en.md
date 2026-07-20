# Value hierarchy (`policy/value-hierarchy.md`)

> 🌐 **English** | [한국어](value-hierarchy.md)

Two values of the same relation are unrelated strings unless you say otherwise. A cohort
study **is** an observational study — but with no declaration,
`relation(P, "연구유형", "관찰연구")?` returns only the rows spelled exactly `관찰연구` and
misses every row filed under `코호트연구`. A silent omission, the one failure mode this KB
exists to prevent. On a real KB that query returned 6 of 14 papers.

## File syntax

```markdown
# policy/value-hierarchy.md
- 연구유형: 코호트연구 ⊂ 관찰연구
- 연구유형: 단면연구 ⊂ 관찰연구
- 대상질환: `emphysema` <: COPD
```

One declaration per line, shaped `relation: narrow ⊂ broad`.

- `<:` and `<` are ASCII spellings of `⊂`.
- `#` comments and `-` / `*` bullets are allowed.
- Backtick-quote a value containing a space, a `:`, or a `<`. Backticked names are lifted
  out **before** the operator is looked for, so a `:` or `<` inside a value can never be
  cut as a separator.
- Relation names and values are NFC-normalised on load. A policy file authored as NFD on
  macOS would otherwise never match NFC accepted facts — the quiet no-op this feature
  exists to remove.

Ancestry is transitive: if `a ⊂ b` and `b ⊂ c`, a query for `c` also catches `a` rows.

The broad value need not appear in any fact. Declaring it is enough to make it queryable.

`factlog init` scaffolds this file with a commented example.

## Scope contract

Subsumption applies when a query's **object** is matched, and the gate, the evaluator,
and the logic report all read the same declarations. That is why `/factlog ask` and
`/factlog check` cannot answer the same question differently.

- **It never rewrites facts.** `accepted.dl` stays a 1:1 projection of the accepted
  candidate rows; each row keeps its own value and its own provenance.
- **It is one-directional.** Asking for the broad value returns rows filed under the
  narrow one; asking for the narrow value does not return the broad one.
- **It does not apply to** `factlog search`, `provenance`, `vocab`, coverage, or conflict
  detection. Those still match values exactly.

## Cycles and warnings

Mistakes are reported, not silently turned into no-ops.

- A **cycle** is discarded whole. Left in, subsumption would become bidirectional and
  break the one-directional contract. The dropped values are reported as warnings.
- A line that is neither a comment nor a parsable declaration, and a line declaring a
  value as its own parent, are ignored with a warning naming the line number.
- Declaring a **relation** or a **value** that no accepted fact uses raises a warning in
  the logic report. Otherwise a single typo would let you **believe** a broad query is
  catching narrow rows.

## Relationship to conflicts

If one subject asserts both `관찰연구` and `코호트연구` and the relation is
[single-valued](single-valued.en.md), that is a `CONFLICT` by default. But if the two
values are a supertype and its subtype, neither is wrong — declare the relationship here
and both rows are kept.

## Related

- [Single-valued relations](single-valued.en.md) — `CONFLICT` and how to resolve it
- [Value vocabulary audit](value-audit.en.md) — finding values that are a *split*, not a hierarchy
- [Typed relations](typed-relations.en.md) — when values must be compared rather than matched
