# Relation aliases (`policy/relation-aliases.md`)

> 🌐 **English** | [한국어](relation-aliases.md)

Map a **surface** relation name to the **canonical** one, so facts written `게재연도` and
`발행년도` are treated as one relation `published_year`. Without this the engine sees two
unrelated relations and a query for one misses facts stored under the other (#213).

```
# policy/relation-aliases.md
- `게재연도` -> `published_year`
- `publication_year` -> `published_year`
```

One mapping per line, `raw` -> `canonical`. **Backticks are required around both names**
here — unlike the other policy files, where they are optional. A line with an arrow but
no backticks is a mapping you meant to make and mis-spelled, so it is reported as
malformed on stderr and skipped, not applied silently. The canonical name is the one you
declare in the other policy files; aliases are folded to it before those apply.

`factlog init` scaffolds this file with a commented example.
