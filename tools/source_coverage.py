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

Orphans are read off facts/candidates.csv, which is the state AFTER merge, so a
row citing a deleted source disappears from that axis the moment merge rebuilds
the CSV without it -- and the KB keeps carrying those run rows with nothing left
to say so (#558). A THIRD figure therefore reads runs/*.json directly: the
sources run rows cite that are not on disk, and how many rows each carries.

Counts use engine facts only: a source backed solely by superseded or
needs_review rows contributes nothing to accepted.dl, so it is correctly a gap.

A second axis is reported below the source list: the DECLARED QUESTIONS
(policy/questions.md) and whether the vocabulary each one leans on still has rows
in engine input (#537). The source axis cannot see that loss -- when every row of
a relation is dropped at merge, no candidate row cites it, so there is no orphan
and no uncovered source, and the summary stays clean while the KB can no longer
answer the question it was built to answer. Silence is the failure mode this tool
exists to break, so it reports both axes.

A question's vocabulary comes from its QUERY DRAFT in facts/query.dl when it has
one, judged by the engine's own gate; facts/query.dl is written by the LLM
`/factlog query` step, so for a question with no draft the axis falls back to an
estimate read off the question text and labels it as such. "No draft yet" and
"the relation is gone from engine input" are different states and are reported
differently.

Always exits 0 by default (informational, never blocks the pipeline). With
--strict, exit non-zero when any TEXT source is uncovered; with
--strict-questions, when any declared question has no engine-input vocabulary --
so automation can surface either kind of silent gap. The run-cited-missing-source
figure gates NOTHING: it reports a state many KBs have carried for a long time,
and hanging it on --strict would fail pipelines that never opted in.

Usage:
    python3 source_coverage.py [--target <kb>] [--strict] [--strict-questions]

--target ("--wiki" is an accepted alias) overrides $FACTLOG_ROOT, which overrides
the active-KB config, which overrides cwd.
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

# --target is the canonical spelling across the toolchain (the `factlog` CLI, the
# engine steps and finalize all take it); --wiki stays as an alias because SKILL.md
# and tests/*.sh spell it that way (#533). ONE tuple feeds BOTH the import-time
# pre-pass below and main()'s strict parser: a spelling only one of the two knew
# would be either read-but-unadvertised or accepted-but-ignored, and an ignored KB
# flag silently retargets the run at whatever the config/cwd tier resolved to.
_ROOT_FLAGS = ("--target", "--wiki")


def _peek_root_flag(argv: list[str] | None = None) -> str | None:
    """The KB root given on the command line, or None.

    ``parse_known_args`` because this runs at import time, before main()'s real
    parser exists: the peek must not reject an argument it is not responsible for.
    Rejecting a typo is main()'s job, once, through its own strict parse.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(*_ROOT_FLAGS, dest="target", default=None)
    known, _ = pre.parse_known_args(sys.argv[1:] if argv is None else argv)
    return known.target


os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root(_peek_root_flag())[0]

from common import (  # noqa: E402
    CANDIDATES_CSV,
    FACTS_DIR,
    QUERY_ENTITY_NOT_ACCEPTED,
    QUERY_FACT_ABSENT,
    QUERY_OK,
    QUERY_RELATION_NOT_ACCEPTED,
    QUERY_REVIEW_REQUIRED,
    FactlogError,
    allowed_relations,
    arg_value,
    attribute_relation_forms,
    classify_query,
    ensure_dirs,
    engine_facts,
    identity_relations,
    is_quoted_string,
    is_sync_ignored,
    is_text_source,
    load_accepted_facts,
    load_facts,
    load_logic_policy,
    load_questions,
    paired_conversion,
    query_args,
    relation_aliases,
    resolve_relation,
    run_cited_sources,
    single_valued_relations,
    source_file_refs,
    source_files,
    source_rel_key,
    sync_ignore_patterns,
    typed_relations,
)
# Below the `from common import (...)` block, never in the sys.path bootstrap at the
# top of this file: that block is #553's conflict surface and must stay untouched.
from factlog.runtime import provenance_line, script_tree_split  # noqa: E402

# Two things the text-estimate fallback needs, both already defined in ask_router:
#
#   grounding_facts(question, accepted) — "the engine-verified facts about the
#     accepted entities this question mentions". That IS the question's evidence;
#     ask_router shows exactly these rows as the verified anchors of an answer.
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


def run_orphan_sources(root: Path) -> list[tuple[str, int]]:
    """(source ref, row count) for every source runs/*.json cites that is missing.

    The blind spot the orphan axis above cannot see (#558). `cited` there comes
    from facts/candidates.csv, and merge drops a row whose source is not on disk
    BEFORE writing that file — so the rows stay in runs/*.json, get dropped and
    warned about on every merge, and the status query reports 0 forever. Reading
    the run files is the only input that still holds them.

    Deliberately NOT folded into ``coverage_rows``: its two-tuple return is a
    pinned signature (tests/unit/test_hidden_sources.py unpacks it), and the two
    figures answer different questions anyway — one is "a fact in engine input
    points at nothing", this one is "input rows will be dropped again next merge".

    Both sides come from common: the refs from ``run_cited_sources`` (keyed by
    merge's own rule) and the disk state from ``source_file_refs`` (the very set
    merge compares against), so this report cannot disagree with the merge whose
    drops it is reporting. Sorted by path, so the output is order-independent.
    """
    on_disk = source_file_refs(root)
    return sorted(
        (ref, count) for ref, count in run_cited_sources(root).items() if ref not in on_disk
    )


def report_run_orphans(run_orphans: list[tuple[str, int]]) -> None:
    """Print the run-cited-missing-source lines to stderr. No-op when empty.

    The remedy hint states what is TRUE today: `factlog eject --orphans` builds
    its cited set from candidates.csv exactly as the orphan axis does, so it
    reports "no orphaned sources found" on precisely this state. Pointing a
    reader at it would send them to a command that does nothing — the loop this
    report exists to break. Fixing eject is tracked separately (#559), and the
    hint names that issue: a reader who has just been told the obvious command
    does not apply should be able to see that the gap is known, rather than
    conclude they are holding the tool wrong.
    """
    if not run_orphans:
        return
    for ref, count in run_orphans:
        print(
            f"  RUN ROWS cite a missing source (dropped at merge, {count} row(s)): {ref}",
            file=sys.stderr,
        )
    total = sum(count for _ref, count in run_orphans)
    print(
        f"  run rows cite {len(run_orphans)} missing source(s) ({total} row(s) total); "
        "inspect runs/*.json — `factlog eject --orphans` does not cover these (see #559)",
        file=sys.stderr,
    )


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

# Every state a question can land in, summary order. `lost` is the only one the
# `--strict-questions` gate fires on: it is THE #537/#538 loss, and it is the only
# verdict that does not rest on an estimate.
_STATES = ("resolvable", "lost", "review", "no_vocabulary", "unmatched", "unusable")

# What a fallback (no query draft) verdict prefixes its reason with, so a reader
# can tell an ESTIMATE from the engine gate's own answer.
_ESTIMATE = "no query draft; estimated from the question text: "

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
            return "lost", f"relation {name!r} has no rows in engine input"
        return "lost", f"no rows in engine input — {reason}"
    return "unusable", f"query draft is not usable — {reason}"


# --- fallback: no query draft (#537) -----------------------------------------
# facts/query.dl is written by the LLM `/factlog query` step (SKILL.md "Step 3 —
# Write facts/query.dl"), so a KB can legitimately have questions and no drafts at
# all. For those questions the axis falls back to an ESTIMATE read off the question
# text — strictly weaker than the gate, and labelled as such in the report so the
# two are never confused (a missing draft and a lost relation are different states).


def relation_probes(name: str) -> list[str]:
    """Surface spellings of a relation NAME to look for in a question text.

    A relation is stored `총_문항_수` / `developed_by` but a natural-language
    question spells it `총 문항 수` / `총문항수` / `developed by`, so the
    separator-folded and (for CJK) separator-REMOVED forms are probed alongside the
    name as declared. Korean compounds are written both ways and neither spelling is
    the wrong one; without the joined probe `총문항수는?` matched no relation at all
    and the question was reported as naming none.

    The folded ASCII probe is dropped unless it carries a word longer than two
    characters — the precision floor ask_router's own keyword matcher applies to
    ASCII. Without it `is_a` folds to "is a", which matches nearly every English
    question and would report a question as grounded in a relation it never
    mentions. The JOINED probe stays CJK-only for the same reason: English does not
    drop the separator, so `developedby` would only ever be noise. CJK keeps
    ask_router's length-2 rule (applied by _entity_mentioned).
    """
    name = unicodedata.normalize("NFC", name)
    probes = [name]
    folded = re.sub(r"[_\-]+", " ", name).strip()
    if folded and folded != name and (_is_cjk(folded) or any(len(word) > 2 for word in folded.split())):
        probes.append(folded)
    joined = re.sub(r"[_\-\s]+", "", name)
    if joined and joined not in probes and _is_cjk(joined):
        probes.append(joined)
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


def effective_relations(relations: list[str]) -> list[str]:
    """*relations* with every name SHADOWED by a longer one dropped.

    A question naming 총_문항_수 necessarily also "names" 문항_수, because one
    spelling contains the other. Treating both as independent evidence is how the
    exact loss this axis exists to catch went silent: 총_문항_수 had zero rows and
    문항_수 (a real, separately declared relation in the same KB) still had one about
    the very subject the question named, so the surviving BROAD relation answered
    for the lost NARROW one and the report read `0 unresolvable`. The narrower name
    is the one the author meant — mentioned_relations already says so in its
    ordering — so it is the only one that gets to speak for that spelling.

    Relations that merely coexist (`developed_by` and `uses`) shadow nothing and are
    all kept: they are independent evidence, and a loss in any of them is real.
    """
    names = [unicodedata.normalize("NFC", name) for name in relations]
    return [
        name for name in names
        if not any(other != name and name in other for other in names)
    ]


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
    the failure this axis exists to catch. Every policy file that declares a
    relation NAME is read for that reason, single-valued.md and typed-relations.md
    included: a relation declared only there and then emptied is exactly as lost as
    one declared in attribute-relations.md, and leaving it out of the vocabulary
    downgraded the report to a vaguer reason.
    """
    names = allowed_relations(candidates) | allowed_relations(accepted)
    names |= attribute_relation_forms(aliases=aliases)
    names |= identity_relations()
    names |= single_valued_relations()
    names |= set(typed_relations())
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


def estimated_verdict(
    question: str,
    vocabulary: set[str],
    accepted: list[dict[str, str]],
    aliases: dict[str, str],
) -> tuple[str, str]:
    """(state, reason) for a question with NO query draft, read off its text.

    Three ways an estimate falls short, told apart because they call for different
    work:

      * ``no_vocabulary`` — the question names no relation this KB knows at all.
        Nothing has been lost; the question was never grounded to begin with.
      * ``lost``          — it names one whose rows are gone from engine input. The
        relation is still DECLARED somewhere, so the report can name it.
      * ``unmatched``     — it names one that HAS rows, but none about anything the
        question names. Naming a relation is not evidence on its own: measured on
        the issue's KB, four of six questions mention `벤치마크`, which survives as a
        one-row relation on an unrelated arXiv paper.

    NFC-normalise the question ONCE, here, for both halves: `mentioned_relations`
    normalises internally, and grounding_facts compares against entity names stored
    NFC, so handing it the raw text made every question on an NFD-stored
    questions.md (the macOS default) report as ungrounded.
    """
    text = unicodedata.normalize("NFC", question)
    relations = effective_relations(mentioned_relations(text, vocabulary))
    if not relations:
        return "no_vocabulary", "the question names no relation this KB declares"
    supported = supported_relations(accepted, aliases)
    missing = [name for name in relations if resolve_relation(name, aliases) not in supported]
    if missing:
        # Named before the grounding check: a relation with NO rows can have no
        # grounding row either, and its absence is the more specific finding.
        return "lost", f"relation {missing[0]!r} has no rows in engine input"
    named = {resolve_relation(name, aliases) for name in relations}
    grounded = any(
        resolve_relation(unicodedata.normalize("NFC", fact["relation"]), aliases) in named
        for fact in grounding_facts(text, accepted)
    )
    if grounded:
        return "resolvable", ""
    return "unmatched", (
        f"relation {relations[0]!r} has rows in engine input, but none about "
        "anything the question names"
    )


def question_rows(
    questions: list[dict[str, str]],
    drafts: dict[str, list[str]],
    accepted: list[dict[str, str]],
    policy_program: str,
    vocabulary: set[str],
    aliases: dict[str, str],
) -> list[dict[str, object]]:
    """Per-question rows: {id, question, state, reason, estimated}.

    A question WITH a draft in facts/query.dl is judged by the engine's own gate; a
    question WITHOUT one falls back to the text estimate, and ``estimated`` records
    which, because the two are not the same claim. ``state`` then separates:

      * ``lost``         — vocabulary engine input no longer carries. THIS is the
        #537 loss, and the only state the strict gate fires on.
      * ``unusable``     — a draft exists but is not a well-formed query at all.
      * ``review``       — the draft routes the question to a human by design.
      * ``no_vocabulary``/``unmatched`` — the fallback's two shortfalls.
      * ``resolvable``   — the engine can evaluate it (or the estimate found
        engine-input evidence for it).
    """
    rows: list[dict[str, object]] = []
    for question in questions:
        lines = drafts.get(question["id"], [])
        estimated = not lines
        if estimated:
            state, reason = estimated_verdict(
                question["question"], vocabulary, accepted, aliases
            )
            if reason:
                reason = _ESTIMATE + reason
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
            "estimated": estimated,
        })
    return rows


def _one_line(exc: Exception) -> str:
    """An exception message flattened to one line — the report's line is a line."""
    return " ".join(str(exc).split())


def _measure(
    questions: list[dict[str, str]],
    candidates: list[dict[str, str]],
    notes: list[str],
) -> list[dict[str, object]]:
    """The per-question rows, reading every input this axis needs. May raise."""
    try:
        accepted = load_accepted_facts()
    except FactlogError:
        # No engine input at all: nothing a draft names can resolve, and the reason
        # is the missing file rather than each question's own vocabulary.
        accepted = []
        notes.append("facts/accepted.dl absent — run /factlog check")

    query_dl = FACTS_DIR / "query.dl"
    if query_dl.is_file():
        drafts = query_drafts(query_dl.read_text(encoding="utf-8"), {q["id"] for q in questions})
        missing = [q for q in questions if q["id"] not in drafts]
        if missing:
            notes.append(f"{len(missing)} question(s) have no query draft — estimated")
    else:
        # facts/query.dl is LLM-authored (/factlog query), so its absence is a
        # normal state, not a loss. Saying so on the summary line keeps "the query
        # step has not run" apart from "engine input no longer carries what the
        # draft asks for" (#538); the per-question lines below then carry the
        # `no query draft; estimated ...` prefix for the same reason.
        drafts = {}
        notes.append("facts/query.dl absent — run /factlog query; questions estimated from text")

    aliases = relation_aliases()
    return question_rows(
        questions,
        drafts,
        accepted,
        load_logic_policy(),
        relation_vocabulary(candidates, accepted, aliases),
        aliases,
    )


def report_questions(candidates: list[dict[str, str]]) -> list[dict[str, object]]:
    """Print the question axis to stdout and return the rows whose vocabulary is
    gone from engine input.

    Never raises. Every input this axis reads is a file some KB can have in a state
    this axis has no business dying on: absent, empty, malformed, or — the one that
    made this a REGRESSION rather than a missing feature — not valid UTF-8. The
    source axis never read policy/questions.md or the relation-declaring policy
    files, so a KB whose Korean policy file is stored EUC-KR used to get its source
    coverage and, once this axis started reading them, got a UnicodeDecodeError and
    rc=1 instead. `UnicodeDecodeError` is not a `FactlogError`, and neither is the
    next surprise, so the catch here is deliberately the broad one: this report is
    informational and must never be the reason the tool fails.
    """
    try:
        questions = load_questions()
    except Exception as exc:  # noqa: BLE001 — see docstring: never take the tool down
        print(f"questions: 0 declared ({_one_line(exc)})")
        return []

    notes: list[str] = []
    try:
        rows = _measure(questions, candidates, notes)
    except Exception as exc:  # noqa: BLE001 — see docstring
        print(f"questions: {len(questions)} declared; vocabulary unreadable ({_one_line(exc)})")
        return []

    by_state = {state: [row for row in rows if row["state"] == state] for state in _STATES}
    lost = by_state["lost"]
    parts = [
        f"{len(by_state['resolvable'])} with resolvable vocabulary",
        f"{len(lost)} unresolvable",
    ]
    for state, label in (
        ("review", "routed to review"),
        ("no_vocabulary", "naming no known relation"),
        ("unmatched", "with no matching rows"),
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
    # The root flag was already consumed by the pre-pass above; declaring it again
    # here is what puts it in --help and what makes a misspelled `--targt /path`
    # exit 2 instead of being silently ignored and reading whatever the config/cwd
    # tier resolved to. No argparse `default=`: the default is not a constant but a
    # resolution over env -> config -> cwd, and the old
    # `os.environ.get("FACTLOG_ROOT", ".")` described a rule this tool has not
    # followed since the pre-pass landed (#531).
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
    parser.add_argument("--strict", action="store_true", help="exit non-zero if any text source has 0 facts")
    # A SEPARATE opt-in, not a widening of --strict. --strict's contract is "a text
    # source has no facts", and callers already read its exit code that way
    # (tests/test_coverage.sh, tests/test_sync_ignore.sh, any CI wired to it). A KB
    # whose questions are still aspirational would start failing a gate it never
    # signed up for, which is how a strict flag gets turned off for good. Opt in and
    # both axes can gate; they compose.
    #
    # Even under the flag, only a LOST vocabulary gates — a relation this KB still
    # declares with zero rows in engine input, which is the one verdict that is a
    # provable loss rather than a shortfall. A question that names no known relation
    # (the scaffolded question of a fresh `factlog init` KB) or whose relation has
    # rows about other subjects is reported on its own line and exits 0.
    parser.add_argument(
        "--strict-questions",
        action="store_true",
        help="exit non-zero if any declared question has no engine-input vocabulary",
    )
    args = parser.parse_args(argv)
    # Which factlog produced the coverage figures, before the first of them (#554).
    # After the strict parse, so --help and a rejected argument stay pure argparse
    # output with no execution context in stdout
    # (tests/unit/test_unaimed_engine_step_guard.py pins that contract).
    print(provenance_line())
    # ...and whether that package came from the same tree as this script (#553): a
    # bundled tools/ driving a working-tree factlog (or the reverse) is the state that
    # made four closed defects look live. Printed to STDERR on purpose — the first two
    # stdout lines are positional contract
    # (tests/unit/test_report_factlog_provenance.py pins out[0]/out[1]), and a warning
    # that fires only in a split installation must not renumber them.
    _tree_split = script_tree_split(__file__)
    if _tree_split:
        print(_tree_split, file=sys.stderr)
    # An empty flag value is refused rather than dropped to the next tier (#546):
    # `--target "$FACTLOG_ROOT"` in a shell that never exported the variable is
    # exactly this shape, and falling through would target the configured KB while
    # the caller believes they named one.
    if args.target is not None and not args.target.strip():
        print(
            "source_coverage: the KB-root flag (--target/--wiki) was empty; pass a KB path, "
            "or pass no flag at all to use the active KB.",
            file=sys.stderr,
        )
        return 1

    ensure_dirs()
    facts = load_facts() if CANDIDATES_CSV.is_file() else []
    root = Path(os.environ["FACTLOG_ROOT"])
    rows, orphans = coverage_rows(root, facts)
    # Read straight off runs/*.json, so it survives what candidates.csv cannot
    # carry (#558). NOT gated by --strict: that flag's contract is "a text source
    # has no facts", and widening it would start failing every pipeline already
    # wired to it, on a state that has always been present.
    run_orphans = run_orphan_sources(root)

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
        # THE worst case of #558 and the one this branch used to say nothing
        # about: every source deleted, so there are no rows to report and every
        # run row is now citing a missing source.
        print("coverage: no source files")
        rc = question_axis()
        if orphans:
            for ref in orphans:
                print(f"  ORPHAN citation (source file missing): {ref}", file=sys.stderr)
        report_run_orphans(run_orphans)
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
    # Omitted entirely at 0, so the summary line of a KB with no run orphans is
    # byte-identical to what it has always been (#558) — this figure is news, not
    # a new column every reader and every grep has to absorb.
    run_orphan_note = (
        f", {len(run_orphans)} run-cited source(s) missing" if run_orphans else ""
    )
    print(
        f"coverage: {len(rows)} source(s); {n_covered} covered{via_note}, "
        f"{len(text_gaps)} text gap(s), {len(binary_gaps)} binary needing conversion, "
        f"{len(orphans)} orphan citation(s){run_orphan_note}{excluded_note}"
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
    report_run_orphans(run_orphans)

    if args.strict and text_gaps:
        print(f"--strict: {len(text_gaps)} text source(s) with no extracted facts", file=sys.stderr)
        rc = 1
    return rc


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
