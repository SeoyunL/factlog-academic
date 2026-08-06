# golden-kb — the policy-gate half of the golden regression

`tests/golden.sh` runs two KBs. `examples/sample-kb` is the tutorial KB: plain
`relation/3` facts and one compiled `requires_review` rule. Its `policy/`
declares no single-valued relations, no typed relations, no attribute relations
and no aliases, so every policy gate is inert on it — `check_conflicts` returns
early, typed projection never runs, no `canonical/3` atom is emitted. A green
golden run said nothing about any of those paths (#354).

This KB exists to walk them. It is synthetic — Orbit, Beacon, Ledger, Vault name
nothing real — and each declaration below is load-bearing for one gate. Removing
any of the four policy files changes the committed golden output, which is how
the coverage is verified rather than asserted.

| Declaration | Gate it walks | What the golden shows |
| --- | --- | --- |
| `policy/single-valued.md` | `check_conflicts.py` | Orbit has two `maintained_by` values, so the tool exits 1 and names the contradiction (Step 4) |
| `policy/typed-relations.md` | `common._project_typed_relations` | four `conflict` findings, one per type — date, number, ordinal, amount |
| `policy/attribute-relations.md` | literal exclusion from the entity graph | `path("Orbit", "2031-02-01")?` is refused as not an accepted entity instead of answered |
| `policy/relation-aliases.md` | alias canonicalisation | `canonical/3` block in `accepted.dl`; `requires_review: Beacon (alias_check)` reaches Beacon only through `owned_by -> maintained_by` |

`facts/query.dl` additionally carries the `count` and `path` query shapes, which
`examples/sample-kb` has none of.

The thresholds in `policy/logic-policy.extra.dl` are deliberately tight against
the single fact that satisfies each — `headcount_value >= 120000` is the scaled
form of 120, `valuation_won >= 10000000000` the base-unit form of 100억 — so a
change in how a literal is parsed or scaled moves the finding out of the report
instead of leaving it comfortably inside a loose bound.

Regenerating after an intended behaviour change: run `tools/compile_facts.py`,
`tools/run_logic_check.py` and `tools/check_conflicts.py` with
`FACTLOG_ROOT=tests/golden-kb`, then copy `facts/accepted.dl`,
`facts/logic_report.txt` and the `check_conflicts` output (stdout **and**
stderr — the tool writes its findings to stderr) into `tests/golden/policy-kb/`.
