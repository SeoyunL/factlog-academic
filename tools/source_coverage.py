#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Coverage critic: which sources has the KB actually extracted facts from?

A plain notes wiki cannot tell you what it failed to capture. This reports, per
source file under sources/ and runs/sources/, how many *engine-input* facts
(status in {confirmed, accepted}) cite it, and flags the gaps:
  - a TEXT source with 0 facts      -> an extraction gap (run /factlog sync)
  - a BINARY source under sources/  -> needs conversion first (factlog ingest)
  - a BINARY source under runs/sources/ -> anomaly: ingest output should be text
A binary original is paired with its runs/sources/<stem> conversion: facts
attach to the conversion, so a binary whose conversion carries facts is
"covered via conversion" (NOT a binary gap). A binary is only flagged as needing
conversion when it has no conversion at all.
It also surfaces orphan citations: a fact citing a source file that no longer
exists on disk (a stale/typo'd reference).

Counts use engine facts only: a source backed solely by superseded or
needs_review rows contributes nothing to accepted.dl, so it is correctly a gap.

A second axis is reported below the source list: the DECLARED QUESTIONS
(policy/questions.md) and whether the relation vocabulary each one leans on
still has rows in engine input (#537). The source axis cannot see that loss --
when every row of a relation is dropped at merge, no candidate row cites it, so
there is no orphan and no uncovered source, and the summary stays clean while
the KB can no longer answer the question it was built to answer. Silence is the
failure mode this tool exists to break, so it reports both axes.

Always exits 0 by default (informational, never blocks the pipeline). With
--strict, exit non-zero when any TEXT source is uncovered; with
--strict-questions, when any declared question has no engine-input vocabulary --
so automation can surface either kind of silent gap.

Usage:
    python3 source_coverage.py [--wiki <kb>] [--strict] [--strict-questions]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# Resolve the KB root and export it before importing common, which binds
# its module-level paths from FACTLOG_ROOT at import time.
import factlog_config  # noqa: E402

os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root_from_argv("--wiki")

from common import (  # noqa: E402
    CANDIDATES_CSV,
    FactlogError,
    allowed_relations,
    attribute_relation_forms,
    ensure_dirs,
    engine_facts,
    identity_relations,
    is_sync_ignored,
    is_text_source,
    load_accepted_facts,
    load_facts,
    load_questions,
    paired_conversion,
    relation_aliases,
    resolve_relation,
    source_files,
    source_rel_key,
    sync_ignore_patterns,
)

# Two things the question axis needs, both already defined in ask_router:
#
#   grounding_facts(question, accepted) — "the engine-verified facts about the
#     accepted entities this question mentions". That IS the question's evidence;
#     ask_router shows exactly these rows as the verified anchors of an answer. The
#     axis below asks one thing of it, and reuses it whole rather than restating
#     what "evidence for a question" means in a second place.
#   _entity_mentioned(name, question_low) — the bilingual "does this question name
#     X?" predicate grounding_facts itself applies to entities: CJK substring at
#     length >= 2 so an attached 조사 cannot hide a match, ASCII lookaround
#     boundaries so a short name does not match inside an unrelated word. Applied
#     here to relation names, so the two halves of one question's vocabulary are
#     matched by ONE rule. Private by name because ask_router exposes no public
#     matcher; a copy would be the only alternative, and copies drift (#213).
from ask_router import _entity_mentioned, _is_cjk, grounding_facts  # noqa: E402


def coverage_rows(root: Path, facts: list[dict[str, str]]) -> tuple[list[dict[str, object]], list[str]]:
    """Return (per-source rows, orphan citations).

    Each row: {file, dir, text, facts, ignored, conversion, conv_facts} where
    facts is how many engine-input rows cite it (source path before any '#') and
    ignored marks a source excluded by policy/sync-ignore.md. For a binary
    original under sources/, conversion is its runs/sources/<stem> text
    conversion (if any) and conv_facts how many facts cite that conversion — so a
    binary whose conversion carries facts is "covered via conversion", not a gap
    (facts attach to the conversion, never to the binary original). Orphans are
    cited paths with no file on disk.
    """
    # NFC-normalise both sides: macOS stores filenames as NFD but candidate
    # sources are NFC, so an un-normalised compare would mis-report a Korean-named
    # source as 0-facts + orphan (see merge_candidates' matching).
    cited: dict[str, int] = {}
    for row in engine_facts(facts):
        ref = unicodedata.normalize("NFC", row.get("source", "").partition("#")[0])
        if ref:
            cited[ref] = cited.get(ref, 0) + 1

    patterns = sync_ignore_patterns()
    rows: list[dict[str, object]] = []
    on_disk: set[str] = set()
    for path in source_files(root):
        # source_files() already drops hidden paths (any dot-prefixed component
        # under the source root), so every enumerator shares one rule (#67).
        ref = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        on_disk.add(ref)
        rows.append({
            "file": ref,
            "dir": "runs/sources" if ref.startswith("runs/sources/") else "sources",
            "text": is_text_source(path),
            "facts": cited.get(ref, 0),
            "ignored": is_sync_ignored(ref, patterns),
            "conversion": "",
            "conv_facts": 0,
        })

    # Pair a binary original under sources/ with its runs/sources/<rel>
    # conversion using the subdir-aware rel key (ingest mirrors the original's
    # subtree), so sources/a/x.pdf pairs with runs/sources/a/x.md and a same-stem
    # file in another subtree does NOT mispair. Only *text* conversions count —
    # a stray binary under runs/sources/ is an anomaly, not a usable conversion,
    # so it must not mask a real "needs conversion" gap on the original.
    conv_by_key: dict[str, str] = {}
    conv_rows: dict[str, dict[str, object]] = {}
    for r in rows:
        if r["dir"] == "runs/sources" and r["text"]:
            cref = str(r["file"])
            conv_by_key.setdefault(source_rel_key(cref), cref)
            conv_rows[cref] = r
    for r in rows:
        if r["dir"] == "sources" and not r["text"]:
            # Match on the full-name key (#213), with a provenance-verified legacy
            # stem-key fallback (paired_conversion) so a pre-#213 conversion still
            # pairs — but never mispairs a same-stem/different-extension sibling.
            cref = paired_conversion(str(r["file"]), conv_by_key, lambda ref: root / ref)
            if cref is not None:
                conv = conv_rows[cref]
                r["conversion"] = conv["file"]
                r["conv_facts"] = conv["facts"]

    orphans = sorted(set(cited) - on_disk)
    return rows, orphans


# --- question axis (#537) ----------------------------------------------------
# A declared question is answered over RELATIONS: `relation("S", "R", O)?` names
# one, and the engine can only answer it if that relation has rows in
# facts/accepted.dl. So the axis asks, per declared question: which relations does
# it name, and does its evidence (ask_router.grounding_facts — engine-input facts
# about the entities it names) hold a row under one of them?
#
# Naming a relation is NOT enough on its own. Measured on the issue's KB, four of
# the six questions mention `벤치마크`, which survives as a one-row relation on an
# unrelated arXiv paper; counting that as coverage would have declared the exact
# loss this axis exists to catch "resolvable". The row has to be about something
# the question actually names.


def relation_probes(name: str) -> list[str]:
    """Surface spellings of a relation NAME to look for in a question text.

    A relation is stored `총_문항_수` / `developed_by` but a natural-language
    question spells it `총 문항 수` / `developed by`, so the separator-folded form
    is probed alongside the name as declared.

    The folded ASCII probe is dropped unless it carries a word longer than two
    characters — the precision floor ask_router's own keyword matcher applies to
    ASCII. Without it `is_a` folds to "is a", which matches nearly every English
    question and would report a question as grounded in a relation it never
    mentions. CJK keeps ask_router's length-2 rule (applied by _entity_mentioned).
    """
    name = unicodedata.normalize("NFC", name)
    probes = [name]
    folded = re.sub(r"[_\-]+", " ", name).strip()
    if folded and folded != name and (_is_cjk(folded) or any(len(word) > 2 for word in folded.split())):
        probes.append(folded)
    return probes


def mentioned_relations(question: str, vocabulary: set[str]) -> list[str]:
    """Relations *question* names, most specific first (longest name, then sorted).

    Most-specific-first because a question asking for 총_문항_수 also contains
    문항_수: the narrower relation is the one the author meant, and it is the one
    worth naming in the report.
    """
    low = unicodedata.normalize("NFC", question).lower()
    hits = [
        name for name in vocabulary
        if any(_entity_mentioned(probe, low) for probe in relation_probes(name))
    ]
    return sorted(hits, key=lambda name: (-len(name), name))


def relation_vocabulary(
    candidates: list[dict[str, str]],
    accepted: list[dict[str, str]],
    aliases: dict[str, str],
) -> set[str]:
    """Every relation name this KB knows: the ones its rows use (candidate and
    engine-input alike) plus the ones its policy files declare.

    The DECLARED half is what makes a loss visible. A relation whose rows were all
    dropped has no row left to name it anywhere in candidates.csv, so a vocabulary
    read off the data alone goes blind at exactly the moment the report matters —
    the failure this axis exists to catch.
    """
    names = allowed_relations(candidates) | allowed_relations(accepted)
    names |= attribute_relation_forms(aliases=aliases)
    names |= identity_relations()
    names |= set(aliases) | set(aliases.values())
    return {unicodedata.normalize("NFC", name) for name in names if name}


def supported_relations(accepted: list[dict[str, str]], aliases: dict[str, str]) -> set[str]:
    """Canonical names of the relations with >= 1 row in facts/accepted.dl.

    Engine input, not candidates: a needs_review row cannot answer a question, and
    the issue's own measurement ("행이 0건") is against accepted.dl. Compared
    canonically (``resolve_relation``, THE alias probe) so a declared alias is not
    mistaken for a missing relation.
    """
    return {
        resolve_relation(unicodedata.normalize("NFC", row["relation"]), aliases)
        for row in accepted
        if row.get("relation")
    }


def question_rows(
    questions: list[dict[str, str]],
    vocabulary: set[str],
    accepted: list[dict[str, str]],
    aliases: dict[str, str],
) -> list[dict[str, object]]:
    """Per-question rows: {id, question, relations, resolvable, reason}.

    A question is RESOLVABLE when its evidence — the engine-input facts about the
    entities it names — holds a row under a relation it names. Anything short of
    that is unresolvable, with the reason that distinguishes the three ways a
    question loses its vocabulary:

      * it names no relation this KB knows at all;
      * it names one whose rows are gone from engine input (the #537 loss: the
        relation is still DECLARED, so the report can name it);
      * it names one that has rows, but none about anything the question names.

    Erring toward "unresolvable" is deliberate: a false alarm is a line to read, a
    missed one is the silence this whole tool exists to break.
    """
    supported = supported_relations(accepted, aliases)
    rows: list[dict[str, object]] = []
    for question in questions:
        text = question["question"]
        relations = mentioned_relations(text, vocabulary)
        named = {resolve_relation(name, aliases) for name in relations}
        grounded = [
            fact for fact in grounding_facts(text, accepted)
            if resolve_relation(unicodedata.normalize("NFC", fact["relation"]), aliases) in named
        ]
        missing = [name for name in relations if resolve_relation(name, aliases) not in supported]
        if grounded:
            reason = ""
        elif not relations:
            reason = "질문에서 KB 관계 어휘를 찾지 못함"
        elif missing:
            reason = f"관계 {missing[0]!r} 가 engine 입력에 없음"
        else:
            reason = f"관계 {relations[0]!r} 의 engine 입력 행이 질문이 언급한 개체와 맞물리지 않음"
        rows.append({
            "id": question["id"],
            "question": text,
            "relations": relations,
            "resolvable": bool(grounded),
            "reason": reason,
        })
    return rows


def _one_line(exc: Exception) -> str:
    """An exception message flattened to one line — the report's line is a line."""
    return " ".join(str(exc).split())


def report_questions(candidates: list[dict[str, str]]) -> list[dict[str, object]]:
    """Print the question axis to stdout and return the unresolvable rows.

    Never raises: an absent/empty/malformed policy/questions.md, an absent
    facts/accepted.dl, and a malformed policy file behind the vocabulary each
    degrade to a stated reason on the summary line. This report is informational,
    and a KB mid-setup must still get its source coverage.
    """
    try:
        questions = load_questions()
    except FactlogError as exc:
        print(f"questions: 0 declared ({_one_line(exc)})")
        return []

    note = ""
    try:
        accepted = load_accepted_facts()
    except FactlogError:
        # No engine input at all: every question is unresolvable, and the reason is
        # the missing file rather than each question's own vocabulary.
        accepted = []
        note = " (facts/accepted.dl absent — run /factlog check)"
    try:
        aliases = relation_aliases()
        vocabulary = relation_vocabulary(candidates, accepted, aliases)
    except FactlogError as exc:
        print(f"questions: {len(questions)} declared; vocabulary unreadable ({_one_line(exc)})")
        return []

    rows = question_rows(questions, vocabulary, accepted, aliases)
    unresolvable = [row for row in rows if not row["resolvable"]]
    print(
        f"questions: {len(rows)} declared; {len(rows) - len(unresolvable)} with resolvable "
        f"vocabulary, {len(unresolvable)} unresolvable{note}"
    )
    for row in unresolvable:
        print(f"  - [{row['id']}] {row['question']}  ({row['reason']})")
    return unresolvable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report source coverage (extraction gaps).")
    parser.add_argument("--wiki", default=os.environ.get("FACTLOG_ROOT", "."), help="KB root")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any text source has 0 facts")
    # A SEPARATE opt-in, not a widening of --strict. --strict's contract is "a text
    # source has no facts", and callers already read its exit code that way
    # (tests/test_coverage.sh, tests/test_sync_ignore.sh, any CI wired to it). A KB
    # whose questions are still aspirational — the normal state right after
    # `factlog init`, whose scaffolded question names no relation — would start
    # failing a gate it never signed up for, which is how a strict flag gets turned
    # off for good. Opt in and both axes can gate; they compose.
    parser.add_argument(
        "--strict-questions",
        action="store_true",
        help="exit non-zero if any declared question has no engine-input vocabulary",
    )
    args = parser.parse_args(argv)

    ensure_dirs()
    facts = load_facts() if CANDIDATES_CSV.is_file() else []
    rows, orphans = coverage_rows(Path(os.environ["FACTLOG_ROOT"]), facts)

    def question_axis() -> int:
        """Report the question axis and return its exit code contribution."""
        unresolvable = report_questions(facts)
        if args.strict_questions and unresolvable:
            print(
                f"--strict-questions: {len(unresolvable)} declared question(s) with no "
                "engine-input vocabulary",
                file=sys.stderr,
            )
            return 1
        return 0

    if not rows:
        print("coverage: no source files")
        rc = question_axis()
        if orphans:
            for ref in orphans:
                print(f"  ORPHAN citation (source file missing): {ref}", file=sys.stderr)
        return rc

    # A binary original is "covered via conversion" when its runs/sources/<stem>
    # conversion carries facts (facts attach to the conversion, not the binary).
    covered_direct = [r for r in rows if r["facts"]]
    covered_via_conv = [r for r in rows if not r["facts"] and r["conv_facts"]]
    excluded = [r for r in rows if r["ignored"]]
    # Sources on the sync-ignore list are never gaps: they're excluded on purpose.
    text_gaps = [r for r in rows if not r["facts"] and r["text"] and not r["ignored"]]
    # A binary original with ANY conversion has been ingested — not a "needs
    # conversion" gap (if its conversion has 0 facts, that surfaces as the
    # conversion's own text gap). Only an unconverted binary is a binary gap.
    binary_gaps = [
        r for r in rows
        if not r["facts"] and not r["text"] and not r["ignored"] and not r["conversion"]
    ]
    n_covered = len(covered_direct) + len(covered_via_conv)
    via_note = f" ({len(covered_via_conv)} via conversion)" if covered_via_conv else ""
    excluded_note = f", {len(excluded)} excluded (sync-ignored)" if excluded else ""
    print(
        f"coverage: {len(rows)} source(s); {n_covered} covered{via_note}, "
        f"{len(text_gaps)} text gap(s), {len(binary_gaps)} binary needing conversion, "
        f"{len(orphans)} orphan citation(s){excluded_note}"
    )
    for r in rows:
        if r["ignored"]:
            tag = "  [excluded]"
        elif not r["facts"] and r["conv_facts"]:
            tag = f"  [covered via {r['conversion']}: {r['conv_facts']} fact(s)]"
        elif not r["facts"] and r["conversion"]:
            tag = f"  [converted → {r['conversion']} (0 facts — re-run /factlog sync)]"
        else:
            tag = ""
        print(f"  {r['facts']} fact(s): {r['file']}{tag}")
    rc = question_axis()
    for r in text_gaps:
        print(f"  GAP (text, run /factlog sync): {r['file']}", file=sys.stderr)
    for r in binary_gaps:
        if r["dir"] == "runs/sources":
            print(f"  GAP (binary under runs/sources — ingest output should be text): {r['file']}", file=sys.stderr)
        else:
            print(f"  GAP (binary, run factlog ingest): {r['file']}", file=sys.stderr)
    for ref in orphans:
        print(f"  ORPHAN citation (source file missing): {ref}", file=sys.stderr)

    if args.strict and text_gaps:
        print(f"--strict: {len(text_gaps)} text source(s) with no extracted facts", file=sys.stderr)
        rc = 1
    return rc


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
