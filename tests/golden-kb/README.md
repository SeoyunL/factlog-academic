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
| `policy/typed-relations.md` | `common._project_typed_relations` | six `conflict` findings pinning the projected VALUE of each type — date, number, ordinal, amount with an inline unit table, amount through the default table, and a number needing rounding |
| `policy/attribute-relations.md` | the accepted-entity precheck in `run_logic_check.py` | `path("Orbit", "2031-02-01")?` is refused before the engine is asked |
| `policy/attribute-relations.md` | the engine's `entity_node/1` extent (#329) | the `entity_extent` findings list exactly the seven real entities; all five declared literals are absent |
| `policy/relation-aliases.md` | alias canonicalisation | `canonical/3` block in `accepted.dl`; `requires_review: Beacon (alias_check)` reaches Beacon only through `owned_by -> maintained_by` |

`facts/query.dl` additionally carries the `count` and `path` query shapes, which
`examples/sample-kb` has none of.

Each rule in `policy/logic-policy.extra.dl` is a CLOSED band, `V >= X, V <= X`,
so it pins the projected value rather than which side of a boundary it sits on.
That distinction is the difference between real coverage and a boolean: with the
earlier one-sided `headcount_value >= 120000`, scaling 120 by 10000 instead of
1000 still satisfied the rule and the run stayed green, while scaling by 100 went
red — half of every scale mutation was invisible. Measured against the bands,
each of these now moves a finding out of the report: `NUMBER_SCALE` in either
direction, `ROUND_HALF_UP -> ROUND_DOWN`, a `+ 1` on the number product, a
changed or dropped term in the date arithmetic, `+ 1` on the ordinal, and a
corrupted default unit table.

Two of the values exist only to reach an axis the others cannot. `load_factor`
is 1.0005, whose scaled product 1000.5 is the only fraction in the KB — every
other value is integral, so the rounding mode was unexercised even with closed
bands. `market_cap` is declared with no inline unit table, so it resolves through
`literal_types.DEFAULT_AMOUNT_UNITS`, which `valuation` never reads: corrupting
the default table's 억 leaves `valuation` green (its inline table is
authoritative, confirmed by mutating that table instead) while corrupting 조
turns `market_cap` red.

The two `attribute-relations.md` rows are separate on purpose. The refused path
query is answered by a Python precheck that runs *before* the engine, so it only
shows the declaration was read. Dropping `!attr_rel` from the `entity_node` rule
in `WIRELOG_PROGRAM`, or making `attribute_relation_program` emit nothing, both
left the run green until the `entity_extent` rule named the extent.

Regenerating after an intended behaviour change: run `tools/compile_facts.py`,
`tools/run_logic_check.py` and `tools/check_conflicts.py` with
`FACTLOG_ROOT=tests/golden-kb`, then copy `facts/accepted.dl`,
`facts/logic_report.txt` and the `check_conflicts` output (stdout **and**
stderr — the tool writes its findings to stderr) into `tests/golden/policy-kb/`.

Doing that by hand is how `conflicts.txt` first got baked from stdout only,
which silently produced an empty golden. A `golden.sh --update` mode that writes
the goldens from the same code path the comparison reads would remove the
footgun; it is not implemented yet.
