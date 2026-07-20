# Single-valued relations (`policy/single-valued.md`)

> 🌐 **English** | [한국어](single-valued.md)

A relation listed here may hold **at most one object per subject**. This is what turns
a contradiction into an error rather than two facts sitting quietly side by side — the
thing a plain notes wiki cannot do for you.

```
# policy/single-valued.md
published_year
`연구 유형`
```

One relation name per line; `#` comments and `-` bullets are allowed; backtick-quote a
name containing spaces. A relation you do not list may hold many objects per subject,
which is the right default for `cites` or `mentions`.

If two distinct objects are asserted for the same (subject, single-valued relation) it
is reported as a `CONFLICT` and the KB refuses to compile until a human resolves it. You
see conflicts with `factlog status` (`conflicts: N`), with `tools/check_conflicts.py`
(which prints each one and the resolution steps), or with `/factlog check` inside Claude
Code. You resolve one with `factlog eject --fact SUBJECT RELATION OBJECT` to retire a
row, or `factlog amend SUBJECT RELATION OBJECT --set-object NEW` to correct one. Never by hand-editing
`facts/candidates.csv`: that bypasses the gate the KB is built around. And if the two
values are a supertype and its subtype, neither is wrong — declare the relationship in
[`policy/value-hierarchy.md`](value-hierarchy.en.md) and both rows are kept.

`factlog init` scaffolds this file with a commented example.
