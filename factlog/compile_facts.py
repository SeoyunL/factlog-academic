#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compile confirmed factlog facts into a Datalog-like fact file."""

from __future__ import annotations

from factlog.common import (
    _atomic_write_text,
    FACTS_DIR,
    canonical_atoms,
    corroboration_counts,
    dedup_engine_atoms,
    dl_string,
    dl_atom,
    engine_facts,
    ensure_dirs,
    load_facts,
    relation_aliases,
)


def main() -> None:
    ensure_dirs()
    facts = load_facts()
    # Collapse the same (subject, relation, object) accepted from several sources
    # to a single engine atom so accepted.dl / ask / run_logic_check use set
    # semantics. Source aggregation (sources: N, provenance) stays on the
    # candidates path and is unaffected. First-occurrence keeps accepted.dl
    # byte-identical when there are no duplicate triples.
    accepted = dedup_engine_atoms(engine_facts(facts))
    lines = [
        "// generated from facts/candidates.csv",
        "// only confirmed/accepted facts become engine input",
        "",
    ]
    for row in accepted:
        lines.append(dl_atom(row))

    # Canonical block: emit canonical/3 EDB atoms for alias-participating facts.
    # Gate: no aliases → emit nothing (accepted.dl byte-identical to no-alias baseline).
    aliases = relation_aliases()
    if aliases:
        c_atoms = canonical_atoms(accepted, aliases)
        if c_atoms:
            lines.append("")
            lines.append("// canonical/3 EDB atoms — engine-only; never parsed by Python readers")
            for s, canon, o in c_atoms:
                lines.append(f"canonical({dl_string(s)}, {dl_string(canon)}, {dl_string(o)}).")

    out = FACTS_DIR / "accepted.dl"
    # Atomic temp+replace: a crash mid-write must never leave a line-boundary-
    # truncated accepted.dl, which parses cleanly yet drops confirmed facts from the
    # engine input.
    _atomic_write_text(out, "\n".join(lines) + "\n")
    # Distinct-source count per collapsed triple, so the compile log surfaces the
    # multi-source provenance of a deduped atom (observability only — accepted.dl,
    # render's `sources: N`, and provenance are unchanged). Computed on the
    # candidates path (corroboration_counts), which is untouched by the dedup.
    source_counts = corroboration_counts(facts)
    print(f"engine facts: {len(accepted)} / {len(facts)}")
    for row in accepted:
        key = (row["subject"], row["relation"], row["object"])
        n_sources = source_counts.get(key, 1)
        print(
            "  - "
            f"{row['subject']} / {row['relation']} / {row['object']} "
            f"(confidence={row['confidence']}, source={row['source']}, sources={n_sources})"
        )
    print(f"written: {out}")


if __name__ == "__main__":
    from factlog.common import run_cli

    raise SystemExit(run_cli(main))
