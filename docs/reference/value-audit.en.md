# Value vocabulary audit (`tools/value_audit.py`)

> 🌐 **English** | [한국어](value-audit.md)

A KB's relation vocabulary is curated by policy. Its **value** vocabulary is not:
values arrive one extraction at a time, and nothing notices when the same thing lands
twice under two spellings. Observed in a real KB, both already `accepted`: `IL-10` and
`기타(IL-10)`, so `relation(P, "염증지표", "IL-10")?` returned 3 of 4 rows. The fourth was
hiding behind a different string. A silent omission — the one failure mode this KB
exists to prevent.

## Running it

```bash
python3 tools/value_audit.py --wiki ~/wiki                  # report (exits 0 by default)
python3 tools/value_audit.py --wiki ~/wiki --strict         # non-zero on a provable query leak
python3 tools/value_audit.py --wiki ~/wiki --all-statuses   # every candidate row, not just engine input
```

- `--wiki` is the KB root. Omitted, it falls back to `$FACTLOG_ROOT`, then the **active KB**
  (set by `factlog use`), then the current directory. If an active KB is set, running from
  any directory audits **the active KB, not the one you are standing in** — so if you feed
  `--strict` to a CI gate, confirm the target with `factlog where` or pass `--wiki`
  explicitly.
- By default only **engine input** rows (`confirmed`/`accepted`) are audited.
  `--all-statuses` includes candidates that no human has approved yet, and is much noisier.
- On a fresh KB with no `candidates.csv` it prints `value_audit: no candidate facts` and
  exits 0 — deliberate, so the tool stays usable inside the automation `--strict` is for.

## Findings

Values are only ever compared **within the same relation** (a value of `염증지표` has
nothing to do with a value of `대상질환`), and every finding is a rule, not a guess.

| Finding | Meaning |
|---|---|
| **split wrapper** | `기타(IL-10)` sitting beside `IL-10` — the same thing filed twice. Queries are leaking right now. |
| **wrapper value** | `기타(INFLA-score)` — the wrapped inner text is not (yet) a value of its own, so asking for `INFLA-score` returns nothing. |
| **placeholder** | `기타`, `불명`, `미상`, `N/A`, `unknown`, `-` — carries no information and hides what the source actually said. |
| **spelling duplicate** | Equal after folding case, spaces, and punctuation (`IL-8` / `il 8`). A query leak — except in an **identity relation** (below), where a collision across subjects suggests a duplicate *record*. |

## What `--strict` means

`--strict` exits 1 only on a **provable query leak**. It counts split wrappers and
spelling duplicates whose kind is `split`. A folded value shared by different subjects of
an identity relation is worth a human look but is not a leak, so it must not fail a CI
gate. Wrapper values and placeholders do not fail `--strict` either: they are hygiene
problems, not proof of a leak.

## Identity relations (`policy/identity-relations.md`)

A title or a DOI names one paper; a publication year or a study type does not. Declare
the former here.

```markdown
# policy/identity-relations.md
제목
DOI
```

One relation name per line; `#` comments and `-` bullets are allowed; backtick-quote a
name containing spaces — the same syntax as the other policy files. Names are
NFC-normalised on load, so a file authored as NFD on macOS still matches accepted facts.

When two subjects of an identity relation share a folded value, it is probably a
duplicate *record*: a different repair, and `--strict` does not fail. Everywhere else a
value shared by many subjects is normal, so a collision means one value split across two
spellings — a query leak, and `--strict` fails. With no declarations at all, every
relation is categorical, so title collisions are reported as leaks. Loud beats quiet, and
the report tells you which relation to declare.

Identity is **declared, not inferred**. The audit does not guess which relations belong
here. Deriving it from the data ("every value maps to one subject") is self-defeating: a
single genuine duplicate record makes the relation non-injective, flips it to categorical,
and then the duplicate fails the gate — exactly the case the classification was meant to
exempt. A two-fact KB would also be injective by accident. Declare only relations whose
value names one subject. **Never declare a category many subjects share** — that
permanently exempts the leak this audit exists to catch.

`factlog init` scaffolds this file with a commented example — every line is a comment, so
it declares nothing and behaves exactly like an absent file. **An existing KB does not
have it at all**, so every
relation starts categorical and title collisions are reported as leaks. Create
`policy/identity-relations.md` yourself and declare your identity relations (for
bibliography, title and DOI).

## Fixing what it finds

Nothing is merged automatically; every finding is reported for human judgement. Fix one
with `factlog amend <subject> <relation> <object> --set-object <canonical>`, which rewrites
the row durably — `candidates.csv` and the backing `runs/*.json` together.

For split wrappers and wrapper values the report also prints a `fix:` line sketching that
`amend` command. Note that its `<subject>` is a literal placeholder: substitute the real
subject before running it. Spelling duplicates and placeholders get no `fix:` line — which
spelling is canonical, and what a placeholder should become, is not something the tool
decides.

## What it does not catch

The wrapper rules are deliberately narrow, so a clean report is not proof of completeness.

- Missed shapes: `others: X`, an unparenthesised `기타 X`, `기타(X) 등`
- Digits are not folded — `1.5` is not `15`
- `etc` is not treated as a wrapper word — `ETC (electron transport chain)` is a real value
- No comparison across relations, by design

`tools/entity_audit.py` is the neighbouring check. It looks for *entity* fragmentation
across the whole KB with a shared-token heuristic, so it is broader and far noisier (2275
candidates on the same KB). Use `value_audit` when you want precise per-relation findings
you can act on immediately.

## Related

- [Value hierarchy](value-hierarchy.en.md) — two values that are a supertype and its subtype are not a split
- [Single-valued relations](single-valued.en.md) — one object per subject, `CONFLICT`
- [Reviewing facts](review.en.md) — the human gate, including `factlog amend`
