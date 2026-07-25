#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compile confirmed factlog facts into a Datalog-like fact file.

Usage:
    python3 -m factlog.compile_facts [--target /path/to/kb]
    python3 tools/compile_facts.py   [--target /path/to/kb]

    --target    KB root ("--wiki" is an accepted alias). Overrides both
                $FACTLOG_ROOT and the active-KB config; with no flag the root
                follows the precedence documented on the prepass below.

Every run names the KB it is about to compile and where that choice came from. A run
whose root came ONLY from the active-KB config, started outside that KB, still compiles
(#527) but will NOT delete an existing facts/accepted.dl when it finds a contradiction —
see :func:`unaimed_removal_refusal` (#547).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Pre-pass: resolve the KB root BEFORE importing common, whose module-level path
# globals (ROOT/FACTS_DIR/...) capture FACTLOG_ROOT at import time.
#
# The config tier of the precedence `factlog where` documents — --flag >
# $FACTLOG_ROOT > config > cwd — lived only in the tools that run this prepass
# (merge_candidates, source_coverage, check_conflicts, ...), and compile_facts
# was not one of them. So a bare run from outside a KB took cwd as the root and
# died with "not a factlog KB root" even with an active KB configured — and it
# had no root flag either, leaving a hand-set FACTLOG_ROOT= as the only way out.
# SKILL.md's `/factlog check` Step 1 invokes this script with no arguments, so
# following the documentation failed outside the KB directory (#527).
# ---------------------------------------------------------------------------

import argparse
import os
import sys
from pathlib import Path

from factlog import config as factlog_config

# --target is the canonical spelling (the `factlog` CLI subcommands use it);
# --wiki is accepted as an alias because the sibling engine scripts spell it
# that way. Both share one dest, so a run passing both settles on argparse's
# last-wins rule and the prepass can never disagree with main()'s parser.
_ROOT_FLAGS = ("--target", "--wiki")


def _peek_root_flag(argv: list[str] | None = None) -> str | None:
    """Return the KB root given on the command line, or None.

    ``parse_known_args`` because this runs at import time, before main()'s real
    parser exists: the peek must not reject an argument it is not responsible
    for, and it leaves the strict parse to main().
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(*_ROOT_FLAGS, dest="target", default=None)
    known, _ = pre.parse_known_args(sys.argv[1:] if argv is None else argv)
    return known.target


def resolve_root(argv: list[str] | None = None) -> tuple[str, str]:
    """This run's KB root **and where it came from** (#547).

    Precedence: flag > $FACTLOG_ROOT > active-KB config > cwd. The second answer —
    'flag' | 'env' | 'config' | 'cwd' — used to be dropped here, which is exactly
    what left this script out of the sibling write guards (#532/#529): consulting
    the config tier is what makes an *unaimed* run possible, and only the provenance
    tells one apart from an aimed one. It is what :func:`unaimed_removal_refusal`
    decides on and what ``main`` announces; nothing downstream can recover it, since
    FACTLOG_ROOT is exported below and every later reader would answer 'env'.
    """
    return factlog_config.resolve_root(_peek_root_flag(argv))


# Module level, bound once at import, for the same reason the export below is:
# common's path globals capture FACTLOG_ROOT at import, so the root the guard
# judges must be the very one those globals were derived from.
TARGET_ROOT, TARGET_SOURCE = resolve_root()

os.environ["FACTLOG_ROOT"] = TARGET_ROOT

# ---------------------------------------------------------------------------
# Now it is safe to import common (ROOT is already set correctly above).
# ---------------------------------------------------------------------------

from factlog.common import (  # noqa: E402
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
from factlog.runtime import provenance_line  # noqa: E402


def unaimed_removal_refusal(root: Path, source: str, cwd: Path) -> str | None:
    """Why this run may not DELETE *root*'s accepted.dl, or None if it may (#547).

    ``source`` is ``factlog_config.resolve_root``'s second answer. Only 'config' names
    a target nobody chose — not the command line, not the environment, not the directory
    the caller is standing in. The criterion is the siblings' verbatim (#532
    merge_candidates, #529 finalize): 'flag' and 'env' are aimed by definition, 'cwd' IS
    the directory the caller stands in, and a config root the caller is standing inside
    (or at) is the documented no-flag workflow.

    What differs is the SCOPE, and deliberately. The siblings refuse the whole run; this
    one refuses one operation — the ``out.unlink`` in :func:`_reject_on_conflict`. The
    guard is sized to the damage:

    * Compiling accepted.dl RE-DERIVES that KB's own confirmed rows. An unaimed compile
      writes the same bytes an aimed one would and destroys nothing, which is why #527
      let the config tier reach this script at all and pinned that behaviour
      (test_compile_facts_config_tier.py). Refusing the whole run would reverse those
      pins, not extend them.
    * Deleting accepted.dl destroys state no re-run can rebuild: the KB has no engine
      input, so ``/factlog ask`` answers nothing, until a human resolves the
      contradiction. That is strictly worse than the silent overwrite #532 exists for,
      and it is the measured symptom on #547 — a run from an unrelated directory left
      the configured KB unanswerable.

    So an unaimed run may still refuse to compile (it always exits non-zero on a
    contradiction) but may not take the KB's existing engine input away with it. The
    #212/#327 invariant — a contradictory KB must not keep a stale accepted.dl for
    readers to trust — is unchanged for every AIMED run, which is every documented
    flow: SKILL.md exports FACTLOG_ROOT before the no-flag call ('env'), finalize and
    `factlog amend/eject` hand the child an explicit FACTLOG_ROOT ('env'), and a run
    from inside the KB passes. The withheld case is reachable only from a run nobody
    aimed, where the alternative is disarming a KB the caller never named.

    Announcing the target (main does that too) is not a substitute: merge_candidates
    already printed its root before every write and #532 happened anyway. A line on
    stdout makes an intended write auditable; it does not stop an unintended one.
    """
    if source != "config":
        return None
    if cwd == root or root in cwd.parents:
        return None
    return (
        f"compile_facts: REFUSING to remove {root}/facts/accepted.dl — that KB comes from "
        f"the active-KB config, not from this command, and the current directory ({cwd}) is "
        f"not inside it.\n"
        f"  The contradiction gate removes facts/accepted.dl so no reader trusts the engine "
        f"input of a KB whose confirmed rows contradict each other (#212/#327). On a run "
        f"nobody aimed, that leaves the configured KB with no engine input at all — "
        f"/factlog ask returns nothing — until a human resolves the conflict (#547), so the "
        f"file is left as it is and this run compiles nothing.\n"
        f"  Name the target explicitly to let the gate heal that KB:\n"
        f"    python3 tools/compile_facts.py --target {root}\n"
        f"  or export FACTLOG_ROOT={root}"
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
    # The one destructive step in this script, and the only one the #547 guard covers:
    # a run nobody aimed may refuse to compile, but may not take the configured KB's
    # existing engine input away with it.
    withheld = unaimed_removal_refusal(Path(TARGET_ROOT), TARGET_SOURCE, Path.cwd().resolve())
    if withheld is not None and out.is_file():
        print(withheld, file=sys.stderr)
        removed = False
    else:
        # Nothing on disk to protect (or an aimed run): the #212/#327 gate applies
        # unchanged.
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
        + (
            " and the existing facts/accepted.dl was KEPT because this run did not aim at "
            "that KB (see the REFUSING line above), so /factlog ask still answers from the "
            "pre-contradiction snapshot until an aimed run resolves this"
            if withheld is not None and out.is_file()
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
    amend/eject, and both keep working on a KB this gate rejects: they read candidates.csv
    through csv.DictReader rather than load_facts, and they WRITE the correction before
    recompiling. Their recompile step does reach the shared loader (_recompile_accepted
    subprocesses factlog.compile_facts, whose main calls load_facts), but a failure there
    costs only accepted.dl — the command still exits 1, yet it says the edit WAS saved to
    candidates.csv/runs — so the edit is durable and the next compile picks it up (#371).
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


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Strict parse of the command line, for --help and for typo rejection.

    The root flag itself was already consumed by the prepass above — parsing it
    again here is what makes ``--help`` list it and, more importantly, what makes
    a misspelled ``--targt /path`` exit 2 instead of being silently ignored and
    compiling into whatever the config/cwd tier resolved to.
    """
    parser = argparse.ArgumentParser(
        prog="compile_facts",
        description=(
            "Compile the confirmed/accepted rows of facts/candidates.csv into "
            "facts/accepted.dl (the engine's input)."
        ),
    )
    parser.add_argument(
        *_ROOT_FLAGS,
        dest="target",
        default=None,
        metavar="PATH",
        help=(
            "KB root (--wiki is an alias). Overrides $FACTLOG_ROOT and the "
            "active-KB config; without it the root is resolved as "
            "$FACTLOG_ROOT > active-KB config > cwd."
        ),
    )
    return parser.parse_args(argv)


def main() -> None:
    _parse_args()
    # Which factlog is doing the compiling, before which KB it compiles (#554). After
    # the strict parse for the same reason the line below is: --help and a rejected
    # argument must stay pure argparse output, with no execution context leaking into
    # stdout (tests/unit/test_unaimed_engine_step_guard.py).
    print(provenance_line())
    # Name the KB about to be compiled and where that choice came from, before anything
    # is read or written — the same line `factlog ingest`/`status`, merge_candidates and
    # finalize print ("target KB {root} (from {source})"). The provenance is the part
    # that tells a reader this run picked the KB up from config rather than from their
    # command; the log below only ever showed the file it wrote (#547).
    print(f"compile_facts: target KB {TARGET_ROOT} (from {TARGET_SOURCE})")
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
