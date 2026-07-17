# Typed relations — full reference

The README's [Typed relations](../README.md#typed-relations-policytyped-relationsmd)
section (한국어: [타입 지정 관계](../README.ko.md#타입-지정-관계-policytyped-relationsmd))
covers the declaration format and the four types at a glance. This document holds
the details you need only when you actually author comparison thresholds: per-type
value normalisation, the `amount` unit table, and the compound-term form an
extractor may emit.

A typed relation is declared in `policy/typed-relations.md`, one per line:

```
- `relation name` : <type> as <ascii_alias>
```

`<ascii_alias>` names the engine side-relation that holds the comparable value; it
is an author-chosen ASCII identifier (`[A-Za-z_][A-Za-z0-9_]*`) so it stays a legal
engine name even when the relation name is non-ASCII.

## Per-type value normalisation

### `date`

`2030.1` / `2030-01-15` → a sortable `yyyymmdd` int64 (missing parts default to
`01`, so `2030.1` → `20300101`). A comparison threshold is written the same way and
is inclusive of the boundary day: `D >= 20300101` includes 2030-01-01.

### `ordinal`

`3rd` / `3위` / `제3호` → an int rank. The value must **start** with the number:
`rank 3` does not parse.

### `amount`

`100억` / `1,000원` → an integer in the base unit. `amount` needs a unit table.
Supply one inline at the end of the declaration line:

```
- `예산` : amount as budget (억=1e8, 만=1e4, 원=1)
```

Unit values must be positive integers. Omit the clause to use the built-in default
unit table.

### `number`

`1,000` / `3.5` → a numeric magnitude, **scaled ×1000** (3 decimal places) to a
sortable int64. This scaling is the one easy-to-miss rule: a threshold in a
comparison predicate MUST be written in **scaled units**.

```
version >= 2.0   →   version_num(S, V), V >= 2000
```

Precision beyond 3 decimals rounds (ROUND_HALF_UP).

## Compound-term objects

An extractor may emit a typed literal object as a compact compound term when that
preserves structure better than a prose string:

```
date(2030,1)   date(2030,1,15)   number(2.5)   ordinal(3)   amount(100,"억")
```

The flat `relation/3` fact stores that term as its object string (still
copy-paste queryable), while the typed side-relation projects the comparable
scalar from it.

## Authoring a comparison predicate

To ask a comparison over a typed relation ("which subjects launched on/after
2030?"), you write the rule yourself in `policy/logic-policy.extra.dl`. See the
bundled factlog skill reference for the full comparison-predicate contract (head
shape, where the threshold goes, and how its rows surface in the logic report).

### A policy `.decl` uses symbol columns only; scalars stay in the body

The typed side-relations above are the *only* place an `int64` column belongs.
Your own policy predicates must declare `symbol` (or `string`) columns:

```
# rejected at load: r is a scalar column
.decl low_rank(subject: symbol, r: int64)
low_rank(S, R) :- priority_rank(S, R), R < 5.

# correct: compare in the body, head a quoted reason
.decl low_rank(subject: symbol, reason: symbol)
low_rank(S, "rank below 5") :- priority_rank(S, R), R < 5.
```

The reason is that the logic report renders an emitted row by printing its
values. A `symbol` column is renderable text; a scalar arrives as a bare number
with nothing to say what it means, so the report would print `low_rank: alpha
(3)` where a reason belongs. Keeping the scalar in the body lets you say *why*
the row fired.

factlog rejects a scalar-column policy `.decl` when it loads
`logic-policy(.extra).dl`, with an error naming the column — it is a loud
failure at load, never a wrong number in a report.
