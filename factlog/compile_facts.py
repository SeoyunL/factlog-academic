#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compile confirmed factlog facts into a Datalog-like fact file."""

from __future__ import annotations

import sys

from factlog.common import (
    _atomic_write_text,
    FACTS_DIR,
    FactlogError,
    canonical_atoms,
    corroboration_counts,
    dedup_engine_atoms,
    detect_conflicts,
    dl_string,
    dl_atom,
    engine_facts,
    ensure_dirs,
    load_facts,
    relation_aliases,
    single_valued_relations,
    typed_relations,
    wirelog_undecodable_chars,
)


def _reject_on_conflict(facts: list[dict[str, str]]) -> None:
    """Refuse to compile while a single-valued contradiction stands (#327).

    /factlog check is exactly compile_facts → run_logic_check (SKILL.md), and NEITHER
    step checked contradictions: the check_conflicts gate lived only in finalize. So a
    finalize that DELETED accepted.dl to heal a contradiction (#212) was undone by the
    very next `/factlog check`, which recompiled the contradictory rows straight back into
    accepted.dl and blessed them `errors: 0`. This gate makes the #212 invariant durable
    across commands: on a contradiction, nothing is written and any stale accepted.dl (a
    prior snapshot, or the pre-#212 poisoned file) is removed so no reader — ask/check both
    read accepted.dl straight from disk without recompiling — can trust it. Deterministic
    (candidates.csv only, no pyrewire); finalize's own step-3 check_conflicts stays as a
    defence-in-depth earlier gate.
    """
    single_valued = single_valued_relations()
    if not single_valued:
        return
    aliases = relation_aliases()
    conflicts = detect_conflicts(facts, single_valued, typed_relations(), aliases)
    if not conflicts:
        return
    canonical_names = set(aliases.values())
    print(f"check_conflicts: {len(conflicts)} conflict(s) found", file=sys.stderr)
    for (subject, relation), objects in sorted(conflicts.items()):
        suffix = " (canonical; incl. surface variants)" if aliases and relation in canonical_names else ""
        print(
            f"  CONFLICT: single-valued '{relation}'{suffix} on '{subject}' has "
            f"{len(objects)} values: {', '.join(objects)}",
            file=sys.stderr,
        )
    out = FACTS_DIR / "accepted.dl"
    removed = out.is_file()
    try:
        out.unlink(missing_ok=True)
    except OSError as exc:  # never crash on a cleanup failure
        print(f"compile_facts: could not remove facts/accepted.dl ({exc}).", file=sys.stderr)
        removed = False
    raise FactlogError(
        "CONTRADICTIONS were found (see CONFLICT lines above); facts were NOT compiled to "
        "facts/accepted.dl"
        + (
            " and the existing facts/accepted.dl was removed, so /factlog ask returns "
            "nothing until the conflict is resolved"
            if removed
            else ""
        )
        + ". Resolve them through the human gate — factlog eject --fact SUBJECT RELATION "
        "OBJECT to retire a row, or factlog amend ... --set-object to correct one — not by "
        "hand-editing facts/candidates.csv. If the values are a supertype and its subtype, "
        "neither is wrong: declare the relationship in policy/value-hierarchy.md and both "
        "rows are kept. Then re-run before trusting the KB."
    )


def _reject_undecodable_control_chars(rows: list[dict[str, str]]) -> None:
    """Refuse to compile a fact whose subject/relation/object carries a control character
    dl_string would emit as a wirelog-undecodable escape (#331).

    Why the gate sits here and not at load: see wirelog_undecodable_chars (common.py).

    dl_string is json.dumps; the engine decodes only \\" and \\\\, so a \\t/\\n/\\uXXXX
    escape (the C0 range U+0000–U+001F) is stored as a literal backslash+letter — python
    holds 'Fig<TAB>2', the engine holds 'Fig\\t2', their intern ids never meet, and the
    value is silently lost from every query (the #308 witness even decodes to a bare
    integer). We FAIL LOUD at compile rather than (a) normalizing — that would silently
    alter a recorded fact; a tab pasted from a PDF table is data, not noise — or (b)
    emitting the raw escape and hoping a downstream decoder agrees, which is exactly the
    silent identity loss this catches. The human gate that repairs the row is factlog
    amend/eject.
    """
    for row in rows:
        for field in ("subject", "relation", "object"):
            bad = wirelog_undecodable_chars(row[field])
            if not bad:
                continue
            shown = ", ".join(repr(c) for c in bad)
            raise FactlogError(
                f"control character(s) {shown} in {field} {row[field]!r} cannot be compiled: "
                "facts/accepted.dl encodes them as JSON escapes the wirelog engine does not "
                "decode (\\t \\n \\r \\b \\f and other U+0000–U+001F controls), so Python and the "
                "engine would hold different strings and the value would be silently dropped "
                "from every query (#331). This usually comes from a tab or newline pasted from a "
                "PDF table or the web into facts/candidates.csv. Correct the row through the human "
                "gate — factlog amend <subject> <relation> <object> --set-object <clean> (or "
                "--set-subject) — not by writing the control character back. "
                "(U+0085/U+2028/U+2029 are fine and never rejected.)"
            )


def _reject_undecodable_canonical_names(aliases: dict[str, str]) -> None:
    """Refuse to compile while ANY declared canonical relation name carries a control
    character dl_string would emit as a wirelog-undecodable escape (#357, widened by #363).

    Why the gate sits here and not at load: see wirelog_undecodable_chars (common.py).

    The canonical name is DERIVED from relation-aliases.md, not from a fact row, so it never
    passed _reject_undecodable_control_chars. #357 first checked it inside the canonical/3
    emission loop, which meant the gate only fired once some fact used the alias key: a tab
    authored into a canonical name that nothing referenced yet compiled rc 0. That was never
    a leak — with no participating fact no canonical atom is emitted, so the undecodable
    string had no path to the engine — but it deferred detection to a later, unrelated commit.
    Checking the DECLARATION surfaces the policy defect where it was authored; the checked
    set is the alias values, which are few enough for the cost to be irrelevant.
    """
    for raw, canon in sorted(aliases.items()):
        bad = wirelog_undecodable_chars(canon)
        if not bad:
            continue
        shown = ", ".join(repr(c) for c in bad)
        raise FactlogError(
            f"control character(s) {shown} in canonical relation name {canon!r} "
            "cannot be compiled: facts/accepted.dl would encode them as JSON escapes "
            "the wirelog engine does not decode (\\t \\n \\r \\b \\f and other "
            "U+0000–U+001F controls), so the canonical/3 EDB atom would silently "
            "diverge from every fact that maps to it (#357, the policy-authoring "
            "sibling of #331). This canonical name comes from policy/relation-aliases.md "
            f"— correct the mapping there (edit the {raw!r} -> `canonical` bullet to a "
            "clean name); do NOT write the control character back. "
            "(U+0085/U+2028/U+2029 are fine and never rejected.)"
        )


def main() -> None:
    ensure_dirs()
    facts = load_facts()
    # Gate BEFORE any write: a contradiction must never reach accepted.dl, the engine's
    # trusted input that ask/check read straight from disk without recompiling (#327/#212).
    _reject_on_conflict(facts)
    # Collapse the same (subject, relation, object) accepted from several sources
    # to a single engine atom so accepted.dl / ask / run_logic_check use set
    # semantics. Source aggregation (sources: N, provenance) stays on the
    # candidates path and is unaffected. First-occurrence keeps accepted.dl
    # byte-identical when there are no duplicate triples.
    accepted = dedup_engine_atoms(engine_facts(facts))
    # Reject wirelog-undecodable control chars BEFORE writing: dl_string would emit JSON
    # escapes the engine cannot decode, so the value silently diverges between Python and
    # the engine and drops out of every query (#331). Fail loud through the human gate.
    _reject_undecodable_control_chars(accepted)
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
    # Gate the whole DECLARATION, not just the names that reach an atom below: every canon
    # emitted here is an aliases.values() element, so this subsumes the per-atom check (#363).
    _reject_undecodable_canonical_names(aliases)
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
    # engine input (#329 — the prevention half; #328 adds the detection guard).
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
