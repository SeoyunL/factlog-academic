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
(policy/questions.md) and whether the vocabulary each one's QUERY DRAFT
(facts/query.dl) leans on still has rows in engine input (#537). The source axis
cannot see that loss -- when every row of a relation is dropped at merge, no
candidate row cites it, so there is no orphan and no uncovered source, and the
summary stays clean while the KB can no longer answer the question it was built
to answer. Silence is the failure mode this tool exists to break, so it reports
both axes.

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
    FACTS_DIR,
    QUERY_ENTITY_NOT_ACCEPTED,
    QUERY_FACT_ABSENT,
    QUERY_OK,
    QUERY_RELATION_NOT_ACCEPTED,
    QUERY_REVIEW_REQUIRED,
    FactlogError,
    arg_value,
    classify_query,
    ensure_dirs,
    engine_facts,
    is_quoted_string,
    is_sync_ignored,
    is_text_source,
    load_accepted_facts,
    load_facts,
    load_logic_policy,
    load_questions,
    paired_conversion,
    query_args,
    source_files,
    source_rel_key,
    sync_ignore_patterns,
)


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
# The mapping from a declared question to the queries that answer it is NOT
# something this tool infers from the question's prose. It is a committed contract
# artifact the pipeline already produces: facts/query.dl, written from
# policy/questions.md per skills/factlog/references/text-to-datalog.md and
# described as the "question -> query-draft contract" in skills/factlog/SKILL.md.
# Each draft is anchored by a `// q3: <the question>` comment carrying the id
# policy/questions.md declares, so the mapping is read, never guessed.
#
# The VERDICT on each draft is the engine's own: common.classify_query, the single
# gate facts/logic_report.txt's "Query evaluation" section and /factlog ask both
# route every query through. Reusing it is what keeps this report from
# contradicting the report the engine writes — an earlier draft of this axis
# matched relation names against the question TEXT and called five questions the
# engine had just answered "unresolvable", which is worse than the silence it was
# built to break.

# A question's query is judged by the gate's stable CODE, never by its reason text
# (the codes exist so a reworded message cannot change routing; the reason is
# display-only). The two codes below both mean "the engine can evaluate this over
# engine input": QUERY_OK is a hit, QUERY_FACT_ABSENT is a VERIFIED NEGATIVE — the
# vocabulary is all there and the engine answers 0 rows. Calling a verified
# negative a coverage gap would flag the sample KB's deliberate q4.
_EVALUABLE_CODES = frozenset({QUERY_OK, QUERY_FACT_ABSENT})

# The #537/#538 loss: the draft names a relation or an entity that engine input no
# longer carries, so the engine refuses to answer at all.
_LOST_CODES = frozenset({QUERY_RELATION_NOT_ACCEPTED, QUERY_ENTITY_NOT_ACCEPTED})

# Per-question states, in the precedence a question's own drafts resolve to. A
# question with ANY evaluable draft is answerable (the engine answers it), and
# below that a lost vocabulary is the news worth printing.
_STATE_ORDER = ("resolvable", "lost", "unusable", "review")

# `// q3: ...` / `// [q3] ...` — the anchor comment shape. The bracket closes the id
# by itself; the bare form needs the `:` (or `.`/`)`) separator to be one.
_ANCHOR_RE = re.compile(
    r"^//\s*(?:\[(?P<bracketed>[A-Za-z][A-Za-z0-9_-]*)\]"
    r"|(?P<bare>[A-Za-z][A-Za-z0-9_-]*)\s*[:.)])"
)
# ...and what a QUESTION id looks like, so an anchor-shaped line can be told from
# ordinary prose that happens to carry a colon ("// Note: ..."). Question ids are
# `q1`/`q12` by convention throughout (policy/questions.md's scaffold, validate's
# `- [q1] 질문` shape). The distinction matters in one direction only: an
# anchor-shaped id this KB does not declare must END the current anchor rather than
# leave the queries under it attributed to the PREVIOUS question — a draft silently
# credited to a neighbour is how a lost relation would read as resolvable. Prose
# must not do that, because the committed convention puts explanatory comment lines
# INSIDE a question's block (examples/sample-kb/facts/query.dl, q4-q7).
_ANCHOR_ID_RE = re.compile(r"^[A-Za-z]{1,2}\d+$")


def query_drafts(text: str, question_ids: set[str]) -> dict[str, list[str]]:
    """Question id -> the query lines facts/query.dl drafts for it.

    An anchor comment claims every query line after it until the next anchor.

    What counts as a query line is run_logic_check's rule verbatim — a non-empty
    line that does not start with `//` — so the axis and the engine cannot disagree
    on what a query is. Lines before the first anchor belong to no declared
    question and are skipped here; the engine still evaluates them.
    """
    drafts: dict[str, list[str]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("//"):
            match = _ANCHOR_RE.match(line)
            anchor = (match.group("bracketed") or match.group("bare")) if match else None
            if anchor in question_ids:
                current = anchor
            elif anchor and _ANCHOR_ID_RE.match(anchor):
                current = None
            continue
        if current is not None:
            drafts.setdefault(current, []).append(line)
    return drafts


def relation_argument(line: str) -> str:
    """The relation NAME a `relation(...)`/`count(...)` draft names, or "".

    Read by POSITION (argument 1 in both shapes), never off the gate's reason
    string, so the report names the same constant the gate judged.
    """
    args = query_args(line)
    if len(args) >= 2 and is_quoted_string(args[1]):
        return arg_value(args[1])
    return ""


def draft_verdict(
    line: str,
    accepted: list[dict[str, str]],
    policy_program: str,
) -> tuple[str, str]:
    """(state, reason) for one query draft, decided by ``classify_query``."""
    _ok, code, reason = classify_query(line, accepted, policy_program)
    if code in _EVALUABLE_CODES:
        return "resolvable", ""
    if code == QUERY_REVIEW_REQUIRED:
        # Not a vocabulary gap: the draft routes this question to a human on
        # purpose, and the engine's report says review_required, not "0 rows".
        return "review", "routed to human review (review_required)"
    if code in _LOST_CODES:
        name = relation_argument(line) if code == QUERY_RELATION_NOT_ACCEPTED else ""
        if name:
            return "lost", f"relation {name!r} is not in engine input"
        return "lost", f"not in engine input — {reason}"
    return "unusable", f"query draft is not usable — {reason}"


def question_rows(
    questions: list[dict[str, str]],
    drafts: dict[str, list[str]],
    accepted: list[dict[str, str]],
    policy_program: str,
    draft_note: str = "no query draft in facts/query.dl — run /factlog query",
) -> list[dict[str, object]]:
    """Per-question rows: {id, question, state, reason}.

    ``state`` separates the three things that are NOT the same failure:

      * ``no_draft``   — policy/questions.md declares it, facts/query.dl has no
        query for it. The query step has not run for this question yet; nothing
        has been lost.
      * ``lost``       — a draft exists and names vocabulary engine input no longer
        carries. THIS is the #537 loss the axis exists to surface.
      * ``unusable``   — a draft exists but is not a well-formed query at all.

    plus ``review`` (routed to a human by design) and ``resolvable`` (the engine
    can evaluate it).
    """
    rows: list[dict[str, object]] = []
    for question in questions:
        lines = drafts.get(question["id"], [])
        if not lines:
            state, reason = "no_draft", draft_note
        else:
            verdicts = [draft_verdict(line, accepted, policy_program) for line in lines]
            state, reason = next(
                (state, reason)
                for want in _STATE_ORDER
                for state, reason in verdicts
                if state == want
            )
        rows.append({
            "id": question["id"],
            "question": question["question"],
            "state": state,
            "reason": reason,
        })
    return rows


def _one_line(exc: Exception) -> str:
    """An exception message flattened to one line — the report's line is a line."""
    return " ".join(str(exc).split())


def report_questions() -> list[dict[str, object]]:
    """Print the question axis to stdout and return the rows whose vocabulary is
    gone from engine input.

    Never raises: an absent/empty/malformed policy/questions.md, an absent
    facts/query.dl, an absent facts/accepted.dl and a malformed policy file behind
    the gate each degrade to a stated reason on the summary line. This report is
    informational, and a KB mid-setup must still get its source coverage.
    """
    try:
        questions = load_questions()
    except FactlogError as exc:
        print(f"questions: 0 declared ({_one_line(exc)})")
        return []

    notes: list[str] = []
    try:
        accepted = load_accepted_facts()
    except FactlogError:
        # No engine input at all: nothing a draft names can resolve, and the reason
        # is the missing file rather than each question's own vocabulary.
        accepted = []
        notes.append("facts/accepted.dl absent — run /factlog check")

    query_dl = FACTS_DIR / "query.dl"
    draft_note = "no query draft in facts/query.dl — run /factlog query"
    if query_dl.is_file():
        drafts = query_drafts(query_dl.read_text(encoding="utf-8"), {q["id"] for q in questions})
    else:
        # A question with no draft at all is NOT a lost relation. Saying so on the
        # summary line keeps "the query step has not run" apart from "the engine
        # input no longer carries what the draft asks for" (#538).
        drafts = {}
        draft_note = "facts/query.dl absent — run /factlog query"
        notes.append(draft_note)

    try:
        policy_program = load_logic_policy()
        rows = question_rows(questions, drafts, accepted, policy_program, draft_note)
    except FactlogError as exc:
        print(f"questions: {len(questions)} declared; vocabulary unreadable ({_one_line(exc)})")
        return []

    by_state = {state: [row for row in rows if row["state"] == state] for state in
                (*_STATE_ORDER, "no_draft")}
    lost = by_state["lost"]
    parts = [
        f"{len(by_state['resolvable'])} with resolvable vocabulary",
        f"{len(lost)} unresolvable",
    ]
    for state, label in (
        ("review", "routed to review"),
        ("no_draft", "without a query draft"),
        ("unusable", "with an unusable draft"),
    ):
        if by_state[state]:
            parts.append(f"{len(by_state[state])} {label}")
    note = f" ({'; '.join(notes)})" if notes else ""
    print(f"questions: {len(rows)} declared; {', '.join(parts)}{note}")
    for row in rows:
        if row["state"] != "resolvable":
            print(f"  - [{row['id']}] {row['question']}  ({row['reason']})")
    return lost


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report source coverage (extraction gaps).")
    parser.add_argument("--wiki", default=os.environ.get("FACTLOG_ROOT", "."), help="KB root")
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any text source has 0 facts")
    # A SEPARATE opt-in, not a widening of --strict. --strict's contract is "a text
    # source has no facts", and callers already read its exit code that way
    # (tests/test_coverage.sh, tests/test_sync_ignore.sh, any CI wired to it). A KB
    # whose questions are still aspirational would start failing a gate it never
    # signed up for, which is how a strict flag gets turned off for good. Opt in and
    # both axes can gate; they compose.
    #
    # Even under the flag, only a LOST vocabulary gates. A question with no draft in
    # facts/query.dl — the normal state right after `factlog init`, before the query
    # step has ever run — is reported and does not exit non-zero: nothing was lost.
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
        unresolvable = report_questions()
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
