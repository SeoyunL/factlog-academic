#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Merge Claude-extracted candidate rows into the factlog KB.

Input contract:
    Claude writes extracted fact rows as JSON (FACT_HEADER schema) under
    $FACTLOG_ROOT/runs/*.json.  This script reads those files, normalises and
    deduplicates the rows, then writes:

        facts/candidates.csv
        pages/<entity>.md
        decisions/open-questions.md   (needs_review bullets)
        decisions/open-questions.md   (stale source refs)

    Finally it delegates structural validation to tools/validate.py.

Canonical source value:
    The "source" field in each candidate row MUST be a path relative to the
    KB root, prefixed with "sources/".  Examples:
        "sources/my-doc.md"
        "sources/subdir/notes.md#section-heading"
    Bare filenames (e.g. "my-doc.md") are NOT valid.  The extraction step in
    the factlog skill must emit fully-qualified sources/-prefixed paths.

What this script does NOT do:
    - parallel worker fan-out or subprocess CLI plumbing (native Claude session)
    - parse_claude_rows / build_source_prompt (LLM steps stay in the skill)
    - SyncReport / print_sync_report (subprocess orchestration)

Usage:
    python3 tools/merge_candidates.py --target /path/to/kb

    --target    KB root ("--wiki" is an accepted alias). Overrides $FACTLOG_ROOT,
                which overrides the active-KB config, which overrides cwd; this
                flag is authoritative and does NOT require FACTLOG_ROOT to be set
                in the environment.
    --input     Glob pattern relative to KB root for input JSON files.
                Default: runs/*.json  (sourced from common.RUNS_DIR)
    --strict    Exit non-zero if any input row is rejected during normalisation
                (useful in automation to surface contract violations).
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Pre-pass: resolve the KB-root flag BEFORE importing common.py so the module-level
# ROOT global captures the correct KB root regardless of the caller's cwd or
# whether FACTLOG_ROOT is set in the environment.
# ---------------------------------------------------------------------------

import argparse
import os
import sys
from pathlib import Path

# Ensure the tools/ directory is on sys.path when this script is run
# directly (python3 tools/merge_candidates.py) so common can be imported.
_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# Resolve the KB root and export it before importing common, which binds
# its module-level paths from FACTLOG_ROOT at import time.
import factlog_config  # noqa: E402

# --target is the canonical spelling across the toolchain (the `factlog` CLI, the
# engine steps and finalize all take it); --wiki stays as an alias because SKILL.md
# and tests/*.sh spell it that way (#533). ONE tuple feeds BOTH this pre-pass and
# main()'s strict parser: a spelling only one of the two knew would be either
# read-but-unadvertised or accepted-but-ignored, and an ignored KB flag silently
# rewrites whatever the config/cwd tier resolved to.
_ROOT_FLAGS = ("--target", "--wiki")

# The resolver's SECOND answer — where the root came from — is kept too. The write
# guard in main() turns on that provenance (#532), and it cannot be recovered later:
# the export below overwrites $FACTLOG_ROOT with the resolved path, after which the
# environment reads back the same value whether or not the caller passed the flag.
_PRE = argparse.ArgumentParser(add_help=False)
_PRE.add_argument(*_ROOT_FLAGS, dest="target", default=None)
_KNOWN, _ = _PRE.parse_known_args()
# The flag value exactly as it arrived, kept alongside the resolution so main() can
# refuse a blank one. Resolution cannot carry that: ``resolve_root`` tests the value
# for truth, so "" and "not given" are the same input to it (#546).
TARGET_FLAG_VALUE = _KNOWN.target
TARGET_ROOT, TARGET_SOURCE = factlog_config.resolve_root(TARGET_FLAG_VALUE)

os.environ["FACTLOG_ROOT"] = TARGET_ROOT

# ---------------------------------------------------------------------------
# Now it is safe to import common (ROOT is already set correctly above).
# ---------------------------------------------------------------------------

import csv  # noqa: E402
import hashlib  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
import subprocess  # noqa: E402
import unicodedata  # noqa: E402
from datetime import datetime, timezone  # noqa: E402

from common import (  # noqa: E402
    FACT_HEADER,
    candidates_csv_writer,
    ENGINE_STATUSES,
    KNOWN_STATUSES,
    REVIEW_STATUSES,
    SUPERSEDED_STATUSES,
    RUNS_DIR,
    attribute_relation_forms,
    dedup_engine_atoms,
    is_attribute_relation,
    engine_facts,
    ensure_dirs,
    fact_key,
    is_sync_ignored,
    is_text_source,
    load_logic_policy,
    load_questions,
    normalize_confidence,
    paired_conversion,
    relation_aliases,
    resolve_relation,
    slugify,
    source_file_refs,
    source_rel_key,
    sync_ignore_patterns,
)
# #537's question axis, reused rather than reimplemented (see the drop-impact
# section below): `query_drafts` READS the question -> query mapping out of
# facts/query.dl's `// qN:` anchors, and `question_rows` (with the vocabulary
# `relation_vocabulary` builds) delegates the verdict on each draft to
# common.classify_query — the gate the engine's own report and `/factlog ask` use.
# Importing costs ~4ms on top of common and buys the guarantee that this line and
# facts/logic_report.txt cannot disagree about which questions the engine can answer.
from source_coverage import (  # noqa: E402
    estimated_verdict,
    mentioned_relations,
    query_drafts,
    question_rows,
    relation_argument,
    relation_vocabulary,
)
from factlog import literal_types  # noqa: E402
from factlog.runtime import provenance_line  # noqa: E402
# No `Heading` here on purpose: this module never holds a heading's coordinates
# across a write. `insert_bullet` resolves them from the text it is editing and
# drops them again, which is what keeps a stale one from being expressible.
from factlog.md_lines import (  # noqa: E402
    bullets,
    ends_inside_fence,
    headings,
    section_body_end,
    unclosed_fence_line,
)
from factlog.review_sections import (  # noqa: E402
    OPEN_QUESTIONS_SCAFFOLD,
    REVIEW_KEYWORDS,
    ensure_review_sections,
    section_for,
)

# The category keywords this module routes bullets with — `decision_section`'s four
# return values and the one `record_stale_page_refs` files stale refs under. Which
# row belongs to which category is a classification rule and lives here; what heading
# a keyword resolves to is `section_for`'s to answer.
#
# Checked at import because the failure is otherwise invisible where it matters. The
# two sets must stay identical, and the drift is silent in both directions:
#   - A keyword this module routes but the contract no longer defines raises KeyError
#     from `section_for` only on a fresh KB; on a populated one `heading_for` finds the
#     old heading and returns it, so bullets keep filing under a category the contract
#     has stopped naming and nothing says so.
#   - A keyword the contract defines but this module never routes makes the scaffolder
#     write a heading and the validator demand it while the producer classifies nothing
#     into it, so every KB grows an empty section forever with no signal at all.
# Loud on every KB, at import, before a single row is classified.
ROUTED_KEYWORDS = frozenset({"중복", "모호", "출처", "충돌"})
if ROUTED_KEYWORDS != REVIEW_KEYWORDS:
    routed_only = sorted(ROUTED_KEYWORDS - REVIEW_KEYWORDS)
    contract_only = sorted(REVIEW_KEYWORDS - ROUTED_KEYWORDS)
    raise RuntimeError(  # not `assert`: this must survive `python -O`
        "merge_candidates routing keywords have drifted from "
        "factlog.review_sections.REVIEW_CATEGORIES.\n"
        f"  routed but the contract no longer defines: {routed_only}\n"
        f"  the contract defines but nothing routes: {contract_only}"
    )

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Statuses that are valid in candidates.csv — the vocabulary common.py owns.
# Rows produced by either the old llmwiki-ops worker or the native Claude
# extraction are preserved, as are rows a human retired (superseded).
VALID_STATUSES: frozenset[str] = KNOWN_STATUSES

# Marker written into auto-generated concept pages.
# We use a factlog-specific marker ("generated-by-factlog") so that pages
# previously generated by earlier tooling (e.g. pages carrying a different
# marker from a prior migration run) are NOT reclaimed — they remain intact
# and this script only manages pages it has itself created.
GENERATED_PAGE_MARKER = "<!-- generated-by-factlog -->"

# Concept-page layout. A KB may override this by placing a template at
# <kb>/templates/pages.md (see load_page_template); absent that file, this
# built-in default is used so existing KBs keep their current page layout.
# Placeholders ({{ENTITY}}, {{SOURCES}}, {{RELATIONS}}, {{REVIEW}}) follow the
# same {{...}} convention as the policy/prompts templates.
#
# IMPORTANT: keep this byte-identical to factlog/cli.py _TEMPLATES["templates/
# pages.md"]; tests/test_page_template.sh pins the two to prevent divergence.
PAGE_TEMPLATE_REL = "templates/pages.md"
DEFAULT_PAGE_TEMPLATE = """\
<!-- generated-by-factlog -->
# {{ENTITY}}

## 요약
- `sources/`에서 추출된 candidate fact를 기준으로 정리한 개념입니다.

## 출처
{{SOURCES}}

## 관련 페이지
{{RELATIONS}}

## 확인 필요
{{REVIEW}}
"""

# Matches exactly the four supported page placeholders. Substitution is a single
# regex pass (see render_page) so a value injected into one slot can never be
# re-scanned and re-substituted as another slot's placeholder.
_PAGE_PLACEHOLDER_RE = re.compile(r"\{\{(ENTITY|SOURCES|RELATIONS|REVIEW)\}\}")


# ---------------------------------------------------------------------------
# Source-file helpers
# ---------------------------------------------------------------------------

# A KB has two source roots: sources/ (the user's originals) and runs/sources/
# (text conversions of binary originals produced by `factlog ingest`). Both are
# valid `source` locations for extracted facts. Source discovery + text sniffing
# (source_file_refs, is_text_source) are imported from common above, shared with
# source_coverage.py.


def unconverted_binary_sources(root: Path) -> list[str]:
    """List binary files under sources/ that have no runs/sources/ conversion.

    A binary original in sources/ yields no facts on its own, but if
    `factlog ingest` has produced a runs/sources/<stem>.{md,txt} conversion the
    text IS ingested, so such originals are NOT reported. Hidden/system files
    (names starting with '.', e.g. .DS_Store) are skipped to avoid noise.
    Sources on the sync-ignore list (policy/sync-ignore.md) are skipped too:
    they are excluded from sync on purpose, so a missing conversion is expected.
    """
    base = root / "sources"
    derived = root / "runs" / "sources"
    if not base.is_dir():
        return []
    # Index every runs/sources/ conversion by its subdir-aware pairing key. ingest
    # names a conversion by the original's full name plus the out-suffix
    # (report.pptx -> runs/sources/report.pptx.md, #213), and mirrors its subdir;
    # source_rel_key drops just that out-suffix so the key equals the original's
    # full-name key. paired_conversion() then matches new naming exactly and falls
    # back (provenance-verified) to a legacy <stem>.{md,txt} conversion.
    conv_by_key: dict[str, str] = {}
    conv_paths: dict[str, Path] = {}
    if derived.is_dir():
        for p in sorted(derived.rglob("*")):
            if p.is_file() and not p.name.startswith("."):
                ref = unicodedata.normalize("NFC", p.relative_to(root).as_posix())
                conv_by_key.setdefault(source_rel_key(ref), ref)
                conv_paths[ref] = p
    patterns = sync_ignore_patterns(root)
    found: list[str] = []
    for path in sorted(p for p in base.rglob("*") if p.is_file()):
        if path.name.startswith("."):
            continue
        if is_text_source(path):
            continue
        orig_ref = unicodedata.normalize("NFC", path.relative_to(root).as_posix())
        if paired_conversion(orig_ref, conv_by_key, lambda ref: conv_paths[ref]) is not None:
            continue  # a conversion backs this binary — it is ingested, not a gap
        if is_sync_ignored(orig_ref, patterns):
            continue
        found.append(path.relative_to(root).as_posix())
    return found


# ---------------------------------------------------------------------------
# Input: read candidate rows from runs/*.json
# ---------------------------------------------------------------------------

def load_candidate_files(root: Path, pattern: str = "runs/*.json") -> list[dict[str, str]]:
    """Read all JSON files matching *pattern* under *root*.

    Each file must contain a JSON array of objects with FACT_HEADER keys.
    Invalid rows are skipped with a warning; invalid files raise SystemExit.

    The "source" field in each row must be a sources/-prefixed path relative to
    the KB root (e.g. "sources/my-doc.md").  Bare filenames are NOT accepted.
    """
    rows: list[dict[str, str]] = []
    candidate_files = sorted(root.glob(pattern))
    if not candidate_files:
        # No candidates yet — not an error; we will just write an empty CSV.
        return rows

    for path in candidate_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise SystemExit(f"failed to parse {path.relative_to(root)}: {exc}") from exc
        if not isinstance(data, list):
            # Not a candidate-rows file (a fact run is a JSON array). Other tools
            # also write JSON under runs/ — e.g. generate_logic_policy.py writes
            # runs/natural-language-to-policy-response.json (an object). Skip such
            # non-array files with a warning instead of failing, so the pipeline
            # stays idempotent when those artifacts are present.
            print(
                f"  skip non-candidate JSON in {path.parent.name}/: {path.name} (not a JSON array)",
                file=sys.stderr,
            )
            continue
        for item in data:
            if not isinstance(item, dict):
                continue
            row = {field: str(item.get(field, "")).strip() for field in FACT_HEADER}
            if all(row[field] for field in FACT_HEADER[:4]):
                rows.append(row)
            else:
                print(
                    f"  skip incomplete row in {path.name}: {item}",
                    file=sys.stderr,
                )
    return rows


# ---------------------------------------------------------------------------
# Normalise & dedup
# ---------------------------------------------------------------------------

def _flush_skipped_sources(skipped: dict[str, int], *, show_counts: bool = True) -> None:
    """Print one 'skip row' line per missing source path, with its row count
    unless *show_counts* is False.

    A single missing source produces one byte-identical warning per row it
    carries, so a handful of stale paths can bury the merge summary (and the
    validate failures after it) under a hundred-line scroll (#492).  Folding on
    the anchor-stripped source_file gives one line per file rather than one per
    anchor.  Order is by path so the diagnostic block is identical regardless of
    input row order.

    What keeps a line from being printed twice is the CALL SITES, not this
    function: the strict flush is immediately followed by ``raise SystemExit``,
    so the end-of-loop flush is never reached on that path.  Anyone changing
    strict to collect every violation before exiting must re-check that, rather
    than rely on the clear() below.

    The same call-site coupling is why strict passes ``show_counts=False``: it
    exits on the FIRST offending row, so its skipped dict always holds exactly
    one path with a count of 1.  The count is then not information but a false
    quantity -- a path carrying 60 rejected rows still reads '(1 row)' (#494).
    The count is real only for the end-of-loop flush, which has seen every row.
    If strict is ever changed to collect every violation before exiting, the
    count becomes meaningful again and ``show_counts=False`` must be revisited.

    ``show_counts=False`` removes the count suffix and nothing else.  The rest
    of the line, the correction hint included, is a constant instruction rather
    than a quantity, and strict exits right after printing it -- so that hint is
    the only guidance the reader gets before the run dies.
    """
    for source_file, count in sorted(skipped.items()):
        message = (
            f"  skip row: source '{source_file}' not found in sources/ "
            f"(expected a sources/- or runs/sources/-prefixed path like 'sources/{Path(source_file).name}')"
        )
        if show_counts:
            # Leading space lives with the suffix so omitting it leaves no
            # trailing whitespace on the line.
            message += f" ({count} row{'s' if count != 1 else ''})"
        print(message, file=sys.stderr)
    # Not load-bearing today (see above) -- kept so a future second flush in one
    # run reports only what accumulated since the last one.
    skipped.clear()


def clean_row(row: dict[str, str]) -> dict[str, str]:
    """One raw run row as it would be stored in facts/candidates.csv.

    Lifted out of :func:`normalize_rows` so a row the merge REJECTS can be put on
    the same footing as one it keeps (#538): the drop-impact report has to ask
    "what would engine input have held if this row had survived", and that question
    is only answerable against the normalised status and the NFC-folded relation
    name, not against the raw run row. One definition, so the counterfactual and
    the CSV cannot disagree about what a row says.

    - source is NFC-folded but NOT stripped: macOS stores filenames as NFD while
      extracted sources are NFC, so folding is what keeps a Korean-named source's
      facts from being dropped; the value is otherwise passed through as-is,
      because it is also the key the source-existence check compares.
    - subject/relation/object are stripped and NFC-folded, so key, CSV and engine
      hold ONE form even when the surviving row of a dedup arrived as NFD.
    - status falls back to 'needs_review' when it is not a known status.
    - confidence is clamped to [0.00, 1.00].
    - an amount object is canonicalised to `amount(N,"unit")` — before the dedup
      key is taken, so `amount(7,"억")` and `amount(7,억)` collapse to one.
    """
    source = unicodedata.normalize("NFC", row["source"])
    clean = {field: row.get(field, "").strip() for field in FACT_HEADER}
    clean["source"] = source  # NFC-normalised canonical source
    for field in ("subject", "relation", "object"):
        clean[field] = unicodedata.normalize("NFC", clean[field])
    clean["status"] = clean["status"] if clean["status"] in VALID_STATUSES else "needs_review"
    clean["confidence"] = normalize_confidence(clean["confidence"])
    # The engine .dl parser supports \" escapes (wirelog#924), so quoting the unit
    # unconditionally keeps a unit with spaces/commas unambiguous and accepted.dl
    # still loads cleanly.
    canon_amount = literal_types.canonical_amount(clean["object"])
    if canon_amount is not None:
        clean["object"] = canon_amount
    return clean


def normalize_rows(
    root: Path,
    rows: list[dict[str, str]],
    *,
    strict: bool = False,
    dropped_rows: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Validate, normalise, and deduplicate raw candidate rows.

    The "source" field must be a sources/-prefixed path relative to the KB root
    (e.g. "sources/my-doc.md" or "sources/subdir/notes.md#anchor").  A bare
    filename ("my-doc.md") does not match and the row is dropped.

    - status is normalised to 'needs_review' if not in VALID_STATUSES
    - confidence is clamped to [0.00, 1.00]
    - subject/relation/object (and source) are NFC-normalised on the way in, so the
      value stored in candidates.csv is on the same NFC form fact_key keys on — key,
      CSV and engine stay one consistent form even when a survivor arrived as NFD.
    - duplicate (subject, relation, object, source-file) tuples collapse to one
      row.  The key is ``common.fact_key`` — the single definition of fact
      identity, shared with the review CLI so a human decision reaches exactly the
      run rows this dedup collapsed (#477).  It strips any '#anchor' (so
      'sources/a.md' and 'sources/a.md#sec1' are one fact) and canonicalises an
      amount object, consistent with the anchor-insensitive
      status-preservation keys (existing_superseded_keys / existing_engine_keys
      / existing_review_keys).  The surviving row is chosen deterministically:
      the one whose full 'source' sorts lexicographically first — a bare
      'sources/a.md' (a prefix of, thus less than, any 'sources/a.md#anchor')
      wins over every anchored variant, otherwise the lexicographically-first
      anchor wins.  This is independent of input row order.
    - result is sorted by (source, subject, relation, object)

    If *strict* is True, any dropped row causes a non-zero exit.

    *dropped_rows*, when given, is EXTENDED with the cleaned form of every row
    rejected because its source is not on disk — the input the drop-impact report
    needs (#538).  Rows lost to dedup are deliberately NOT collected: a dedup loser
    shares its (subject, relation, object, source-file) key with the row that
    survived, so it can take nothing out of engine input and can affect no
    question.  An out-parameter rather than a second return value, so that the
    existing callers (and the tests pinning them) keep reading one list.
    """
    known_sources = source_file_refs(root)
    # Anchor-insensitive dedup: key on (subject, relation, object, source-file),
    # mapping to the surviving row.  On collision the row whose full 'source'
    # sorts lexicographically first wins (bare path < anchored variant), so the
    # winner is fixed by value, not input order.
    dedup: dict[tuple[str, str, str, str], dict[str, str]] = {}
    dropped = 0
    # Missing sources are counted per anchor-stripped path and reported once at
    # the end instead of once per row (#492).
    skipped: dict[str, int] = {}

    for row in rows:
        # Cleaned up front, before the source check, so a REJECTED row can be handed
        # to the drop-impact report in the same shape a kept one has (#538). The
        # source is NFC-folded here (see clean_row): macOS stores filenames as NFD
        # but extracted sources are typically NFC, and comparing on one form is what
        # keeps a Korean-named source's facts from being silently dropped.
        clean = clean_row(row)
        # Compare the pre-anchor portion against known sources/-prefixed refs.
        # The canonical source value is already sources/-prefixed; bare filenames
        # will not match and are dropped with a warning.
        source_file = clean["source"].partition("#")[0]
        if source_file not in known_sources:
            skipped[source_file] = skipped.get(source_file, 0) + 1
            dropped += 1
            if dropped_rows is not None:
                dropped_rows.append(clean)
            if strict:
                # strict still dies on the FIRST offending row -- flush here so
                # the diagnostic is not lost to the early exit.  No count: it
                # would always be 1 regardless of how many rows that path
                # carries (#494).
                _flush_skipped_sources(skipped, show_counts=False)
                raise SystemExit(
                    f"--strict: input row rejected (source not found): {source_file}"
                )
            continue
        # Fact identity comes from ONE place, common.fact_key -- the same function the
        # review CLI keys its runs/*.json writes on. Re-deriving it here (subject,
        # relation, object, anchor-stripped source) is how the two definitions drifted
        # apart and a human-confirmed fact vanished from the engine (#477). canonical_amount
        # is idempotent, so passing the already-canonicalised object changes nothing.
        key = fact_key(clean["subject"], clean["relation"], clean["object"], clean["source"])
        existing = dedup.get(key)
        if existing is None:
            dedup[key] = clean
        else:
            dropped += 1
            # Keep the row whose full source sorts first (bare < anchored).
            if clean["source"] < existing["source"]:
                dedup[key] = clean

    _flush_skipped_sources(skipped)

    if dropped:
        print(f"  warning: {dropped} row(s) dropped during normalise/dedup", file=sys.stderr)

    return sorted(
        dedup.values(),
        key=lambda item: (item["source"], item["subject"], item["relation"], item["object"]),
    )


# ---------------------------------------------------------------------------
# Drop impact: what the dropped rows cost the declared questions (#538)
# ---------------------------------------------------------------------------
# A row dropped because its source file is gone takes its relation's vocabulary with
# it, and every downstream summary then reads clean: no candidate row cites the
# missing source, so there is no orphan citation and no uncovered source; the logic
# report has no errors because a relation with no rows contradicts nothing. The KB
# has lost the ability to answer a question it declares in policy/questions.md and
# nothing says so. The `warning: N row(s) dropped` line above is a quantity, not a
# consequence.
#
# The question -> query mapping and the verdict on each query are NOT re-invented
# here. Both are #537's, imported from source_coverage: the mapping is READ from the
# `// qN:` anchor comments in facts/query.dl (a committed contract artifact), and the
# verdict comes from common.classify_query — the same gate facts/logic_report.txt's
# "Query evaluation" section and `/factlog ask` route every query through. #537's own
# first draft matched relation names against the question TEXT and called five
# questions the engine had just answered "unresolvable"; a second implementation of
# that rule here would put this line and the engine's report back into disagreement.

# The state source_coverage reports for a question the engine can evaluate (or, for a
# question with no query draft, one its text estimate found engine-input evidence
# for). "The drop cost the KB an answer" is defined as a question that HELD this
# state before the drop and does not hold it after.
_ANSWERABLE = "resolvable"

# Gate verdicts that say nothing about a question's VOCABULARY, so the flip test
# cannot see a loss through them: `review` is a draft that defers to a human by
# design, `unusable` one that is not a well-formed query at all. Both read the same
# before and after any drop. For those two — and only those — the report also asks
# source_coverage for the ESTIMATE it falls back to on a draft-less question, because
# the issue's own KB is in exactly that state (its six questions evaluate to
# review_required) and reporting it as unaffected would be the silence again.
#
# The estimate is still required to FLIP, which is what keeps this from re-raising
# #537's false alarm: that failure was an estimate that read a question wrong in a
# fixed way, and a verdict that reads the same on both sides of the drop reports
# nothing. Only a verdict that CHANGED because rows were dropped is printed.
#
# Each maps to the reason PREFIX its line carries. Spelled here rather than reusing
# source_coverage's own estimate prefix, which reads "no query draft": these questions
# HAVE a draft, and saying they do not would misdirect the reader to the query step.
# The half that must not drift — "this is an estimate off the question text, weaker
# than the gate's own answer" — is stated by both.
_GATE_SILENT_STATES = {
    "review": "the query draft routes this question to human review",
    "unusable": "the query draft is not a usable query",
}


def engine_atoms(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    """The (subject, relation, object) atoms *rows* would put into facts/accepted.dl.

    This is the answer to "judged against what?" (#538). The drop happens DURING the
    merge, so facts/accepted.dl on disk still describes the previous run and cannot
    say what this run costs. What can is the merge's own output: compile_facts writes
    accepted.dl from exactly the candidates.csv rows this run is about to write,
    filtered to ENGINE_STATUSES. So a relation is "gone" when the PROJECTED
    post-merge engine input has no row for it — not when the stale file on disk lacks
    it, and not merely because a dropped row named it (a relation whose dropped rows
    all had surviving twins lost nothing).

    Reuses ``engine_facts`` for the status filter and ``dedup_engine_atoms`` for the
    triple collapse, the two rules compile_facts itself applies, so the projection
    and the real thing cannot disagree.
    """
    return dedup_engine_atoms([
        {"subject": row["subject"], "relation": row["relation"], "object": row["object"]}
        for row in engine_facts(rows)
    ])


def counterfactual_status(
    row: dict[str, str],
    superseded_keys: set[tuple[str, str, str, str]],
    engine_keys: dict[tuple[str, str, str, str], str],
    review_keys: set[tuple[str, str, str, str]],
) -> str:
    """The status *row* would carry had the merge not dropped it.

    A dropped row never reaches the status-preservation passes in ``main``, so its
    own status understates what it would have contributed: a fact a human accepted
    in a previous merge comes back from the run as 'candidate' and is re-promoted
    from candidates.csv. Without that, a KB whose accepted rows all lost their source
    would measure as "nothing left engine input", and the report would say nothing.

    The precedence reimplements ``main``'s passes, in their order: a superseded
    tombstone wins outright; a row the run itself already retired is left retired;
    otherwise a recorded engine status is restored; a deliberate re-review then holds
    an engine status back at needs_review (and leaves a 'candidate' alone, as the hold
    there does).

    THIS IS THE ONE PLACE THOSE PASSES ARE WRITTEN TWICE. Keep it in step with them
    (``main``, from the superseded preservation down to the needs_review hold) — when
    the two drift, this reports a loss that never happened. The engine-restore guard
    below is exactly that bug once: without ``status not in SUPERSEDED_STATUSES`` a
    row the run retired is promoted back to accepted here, is counted as engine input
    it would never have reached, and the report announces a relation "gone" that was
    already gone before the drop.
    """
    key = fact_key(row["subject"], row["relation"], row["object"], row["source"])
    if key in superseded_keys:
        return "superseded"
    status = row["status"]
    # main: `if want and row["status"] not in SUPERSEDED_STATUSES` — a later rejection
    # wins over an earlier acceptance, so a retired row is not restored from
    # candidates.csv.
    if status not in SUPERSEDED_STATUSES:
        status = engine_keys.get(key, status)
    if key in review_keys and status in ENGINE_STATUSES:
        return "needs_review"
    return status


def lost_relations(
    kept: list[dict[str, str]],
    dropped: list[dict[str, str]],
    aliases: dict[str, str],
) -> list[str]:
    """Relation names only the DROPPED rows would have put into engine input.

    Compared canonically (``resolve_relation``, THE alias probe — the same one
    source_coverage's ``supported_relations`` uses) so a declared alias of a
    surviving relation is not reported as a loss. Sorted, for a line that does not
    move between runs.
    """
    survivors = {resolve_relation(atom["relation"], aliases) for atom in engine_atoms(kept)}
    return sorted({
        canonical
        for atom in engine_atoms(dropped)
        if (canonical := resolve_relation(atom["relation"], aliases)) not in survivors
    })


def relations_behind(
    question: str,
    drafts: list[str],
    lost: list[str],
    aliases: dict[str, str],
) -> list[str]:
    """Which of *lost* the question actually leans on, in *lost*'s order, or [].

    The gate's own reason does not point at the drop. ``common.classify_query`` checks
    the SUBJECT before the relation, so a triple whose every row is gone comes back as
    `entity_not_accepted` — true, and about the wrong thing: the entity stopped being
    accepted BECAUSE the relation lost its rows. Naming the lost relation on the
    question's line puts the cause next to the effect, which is the shape #538 asked
    for.

    Attribution, so it is deliberately conservative — only a relation this report has
    already established as lost can appear, and if none of them is one the question
    names, nothing is claimed and the gate's reason stands alone. Read off the draft
    BY POSITION (``relation_argument``, the same probe source_coverage judges with),
    falling back for a draft-less question to source_coverage's own text probe over
    the lost names.
    """
    if not lost:
        return []
    named = {resolve_relation(relation_argument(line), aliases) for line in drafts}
    hits = [name for name in lost if name in named]
    if hits:
        return hits
    # No draft (or a draft naming nothing lost, e.g. a review_required(...) line):
    # ask the question text, the same fallback the reported verdict itself used.
    mentioned = set(mentioned_relations(question, set(lost)))
    return [name for name in lost if name in mentioned]


def question_states(
    questions: list[dict[str, str]],
    drafts: dict[str, list[str]],
    accepted: list[dict[str, str]],
    vocabulary: set[str],
    policy_program: str,
    aliases: dict[str, str],
) -> dict[str, dict[str, object]]:
    """id -> source_coverage's per-question verdict over *accepted* as engine input."""
    return {
        str(row["id"]): row
        for row in question_rows(
            questions, drafts, accepted, policy_program, vocabulary, aliases
        )
    }


def unanswerable_questions(
    questions: list[dict[str, str]],
    drafts: dict[str, list[str]],
    kept: list[dict[str, str]],
    would_be: list[dict[str, str]],
    policy_program: str,
    aliases: dict[str, str],
) -> list[tuple[str, str, str]]:
    """(id, question, reason) for each question the drop turned unanswerable.

    Measured as a counterfactual, not as an absolute: the verdict is taken twice,
    once over the engine input this merge produces (*kept*) and once over the engine
    input it WOULD have produced had nothing been dropped (*kept* + *would_be*), and
    only a question that was answerable before and is not after is reported. That is
    what makes the line about THIS drop — a question whose vocabulary was already
    missing, or that was never grounded to begin with, is #537's axis to report and
    is not news the merge caused.

    It is also why dedup losers are left out of *would_be* upstream: their twins
    survive, so the two passes would be identical.
    """
    before, after = kept + would_be, kept
    acc_before, acc_after = engine_atoms(before), engine_atoms(after)
    voc_before = relation_vocabulary(before, acc_before, aliases)
    voc_after = relation_vocabulary(after, acc_after, aliases)
    before_states = question_states(
        questions, drafts, acc_before, voc_before, policy_program, aliases
    )
    after_states = question_states(
        questions, drafts, acc_after, voc_after, policy_program, aliases
    )

    lost: list[tuple[str, str, str]] = []
    gate_silent: list[tuple[dict[str, str], str]] = []
    for question in questions:
        was, now = before_states[question["id"]], after_states[question["id"]]
        if was["state"] == _ANSWERABLE and now["state"] != _ANSWERABLE:
            lost.append((question["id"], question["question"], str(now["reason"])))
        elif was["state"] in _GATE_SILENT_STATES and now["state"] == was["state"]:
            gate_silent.append((question, str(was["state"])))

    # For those, ask source_coverage's own text estimate — the fallback it applies to a
    # draft-less question — on both sides of the drop, and report only a flip.
    for question, state in gate_silent:
        was, _ = estimated_verdict(question["question"], voc_before, acc_before, aliases)
        now, why = estimated_verdict(question["question"], voc_after, acc_after, aliases)
        if was == _ANSWERABLE and now != _ANSWERABLE:
            reason = f"{_GATE_SILENT_STATES[state]}; estimated from the question text: {why}"
            lost.append((question["id"], question["question"], reason))

    return lost


def report_drop_impact(
    root: Path,
    kept: list[dict[str, str]],
    dropped: list[dict[str, str]],
    superseded_keys: set[tuple[str, str, str, str]],
    engine_keys: dict[tuple[str, str, str, str], str],
    review_keys: set[tuple[str, str, str, str]],
) -> None:
    """Print what the dropped rows cost this KB's declared questions (#538).

    Silent when nothing was dropped; otherwise it always prints at least one line,
    including "no relation lost, no question affected" — a drop with no consequence
    is a fact worth stating, because the whole complaint is that the reader could
    not tell the two cases apart.

    Never raises, for the same reason source_coverage's question axis does not: every
    input read here is a file a KB may legitimately have absent, empty, malformed or
    not valid UTF-8, and this report is a diagnostic printed alongside a warning. It
    must never be the reason a merge fails.
    """
    if not dropped:
        return
    try:
        _report_drop_impact(root, kept, dropped, superseded_keys, engine_keys, review_keys)
    except Exception as exc:  # noqa: BLE001 — see docstring: never take the merge down
        detail = " ".join(str(exc).split())
        print(f"  drop impact: unavailable ({detail})", file=sys.stderr)


def _report_drop_impact(
    root: Path,
    kept: list[dict[str, str]],
    dropped: list[dict[str, str]],
    superseded_keys: set[tuple[str, str, str, str]],
    engine_keys: dict[tuple[str, str, str, str], str],
    review_keys: set[tuple[str, str, str, str]],
) -> None:
    """:func:`report_drop_impact` without the guard. May raise."""
    aliases = relation_aliases(root)
    would_be = [
        {**row, "status": counterfactual_status(row, superseded_keys, engine_keys, review_keys)}
        for row in dropped
    ]
    relations = lost_relations(kept, would_be, aliases)
    if relations:
        print(
            f"  drop impact: {len(relations)} relation(s) left with no row in engine "
            f"input: {', '.join(relations)}",
            file=sys.stderr,
        )
    # The relation half is reported on every path, including the ones that return
    # early below: "no relation left engine input" is half the finding, and dropping it
    # whenever policy/questions.md happens to be unreadable leaves the reader with a
    # bare error where a result belongs. Folded into whichever line comes next rather
    # than printed on its own, so the harmless case stays one line.
    lead = "" if relations else "no relation left engine input; "

    try:
        questions = load_questions()
    except Exception as exc:  # noqa: BLE001
        # A KB with no readable policy/questions.md declares no questions, so there is
        # no answerability to lose — said out loud rather than passed over, because
        # "no question was affected" and "no question could be checked" are different
        # findings and the reader is here looking for the second one.
        #
        # Not only FactlogError: policy/questions.md is a file, and a Korean one stored
        # EUC-KR raises UnicodeDecodeError instead (the surprise that made #537's axis a
        # regression rather than a missing feature).
        detail = " ".join(str(exc).split())
        print(
            f"  drop impact: {lead}no declared questions to check ({detail})",
            file=sys.stderr,
        )
        return

    query_dl = root / "facts" / "query.dl"
    if query_dl.is_file():
        try:
            text = query_dl.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            # Named, because the outer guard prints the exception alone and a bare
            # "'utf-8' codec can't decode byte 0xba" does not say WHICH of the files
            # this report reads was the bad one (a Korean query.dl stored EUC-KR is
            # the same surprise policy/questions.md already reports by name above).
            raise RuntimeError(f"reading facts/query.dl: {' '.join(str(exc).split())}") from exc
        drafts = query_drafts(text, {q["id"] for q in questions})
    else:
        # facts/query.dl is written by the LLM `/factlog query` step, so its absence is
        # a normal state rather than a loss. Every question then falls back to
        # source_coverage's text estimate, and each reported line says so itself.
        drafts = {}
        print(
            "  drop impact: facts/query.dl absent (run /factlog query) — questions "
            "estimated from their text",
            file=sys.stderr,
        )

    unanswerable = unanswerable_questions(
        questions, drafts, kept, would_be, load_logic_policy(), aliases
    )
    if not unanswerable:
        # Stated even when a relation WAS lost: "some vocabulary is gone" and "an
        # answer is gone" are different findings, and a relation nothing asks about is
        # the case where the merge is allowed to look clean.
        print(
            f"  drop impact: {lead}no declared question changed answerability",
            file=sys.stderr,
        )
        return
    ids = " ".join(f"[{qid}]" for qid, _question, _reason in unanswerable)
    print(
        f"  drop impact: {len(unanswerable)} declared question(s) in policy/questions.md "
        f"turned unanswerable: {ids}",
        file=sys.stderr,
    )
    for qid, question, reason in unanswerable:
        behind = relations_behind(question, drafts.get(qid, []), relations, aliases)
        # "; " and not " — ": the gate's own reason already carries an em dash, and two
        # of them in one line stop reading as one clause after another.
        cause = f"lost relation {', '.join(behind)}; " if behind else ""
        print(f"    - [{qid}] {question}  ({cause}{reason})", file=sys.stderr)


# ---------------------------------------------------------------------------
# Write facts/candidates.csv
# ---------------------------------------------------------------------------

def write_facts(root: Path, rows: list[dict[str, str]]) -> None:
    out = root / "facts" / "candidates.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as f:
        writer = candidates_csv_writer(f, FACT_HEADER)
        writer.writeheader()
        writer.writerows(rows)


# ---------------------------------------------------------------------------
# Write pages/<entity>.md
# ---------------------------------------------------------------------------

def page_filename(title: str) -> str:
    slug = slugify(title)
    # Guard against the filesystem NAME_MAX limit (255 bytes on most systems):
    # a very long entity name (e.g. a stray free-text value) would otherwise
    # raise OSError when the page is written. Truncate on a UTF-8 byte boundary
    # and append a short stable hash so distinct long names never collide.
    _MAX_SLUG_BYTES = 200
    if len(slug.encode("utf-8")) > _MAX_SLUG_BYTES:
        digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]
        truncated = slug.encode("utf-8")[:_MAX_SLUG_BYTES].decode("utf-8", "ignore").strip("-")
        slug = f"{truncated}-{digest}"
    return f"{slug}.md"


def load_page_template(root: Path) -> str:
    """Return the concept-page template: <kb>/templates/pages.md if present,
    else the built-in DEFAULT_PAGE_TEMPLATE (backward-compatible).

    CRLF is normalised to LF. An empty/whitespace-only custom template falls back
    to the default (with a warning); a non-empty one with no recognised
    placeholder is honoured but warned about (pages would be static text)."""
    custom = root / PAGE_TEMPLATE_REL
    if not custom.is_file():
        return DEFAULT_PAGE_TEMPLATE
    text = custom.read_text(encoding="utf-8").replace("\r\n", "\n")
    if not text.strip():
        print(f"  {PAGE_TEMPLATE_REL} is empty; using built-in default page template", file=sys.stderr)
        return DEFAULT_PAGE_TEMPLATE
    if not _PAGE_PLACEHOLDER_RE.search(text):
        print(
            f"  {PAGE_TEMPLATE_REL} has no {{{{ENTITY}}}}/{{{{SOURCES}}}}/{{{{RELATIONS}}}}/{{{{REVIEW}}}}"
            " placeholders; generated pages will be static text",
            file=sys.stderr,
        )
    return text


def render_page(template: str, entity: str, sources: str, relations: str, review: str) -> str:
    """Substitute the page placeholders and guarantee the generated marker.

    Substitution is a SINGLE regex pass: each {{...}} is replaced exactly once,
    so a value placed in one slot (e.g. a source string that literally contains
    '{{REVIEW}}') is never re-scanned and re-substituted. The marker is what
    makes regeneration non-destructive (hand-authored pages are never
    overwritten); if a custom template doesn't start with it, prepend it so a
    user's edit cannot silently break regeneration detection."""
    mapping = {"ENTITY": entity, "SOURCES": sources, "RELATIONS": relations, "REVIEW": review}
    text = _PAGE_PLACEHOLDER_RE.sub(lambda m: mapping[m.group(1)], template)
    if not text.startswith(GENERATED_PAGE_MARKER):
        text = f"{GENERATED_PAGE_MARKER}\n{text}"
    return text


def write_pages(root: Path, rows: list[dict[str, str]]) -> list[str]:
    """Generate per-entity concept pages from confirmed/accepted candidate rows.

    Only pages carrying GENERATED_PAGE_MARKER are touched; hand-authored pages
    are never overwritten.  Pre-existing pages generated by earlier tooling
    that carry a different marker are NOT reclaimed — they remain as-is and
    are excluded from this script's management scope.
    """
    pages_dir = root / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    template = load_page_template(root)

    # Remove previously generated pages (ours only).
    for page in pages_dir.glob("*.md"):
        text = page.read_text(encoding="utf-8", errors="ignore")
        if GENERATED_PAGE_MARKER in text:
            page.unlink()

    # Superseded rows are retired: keep them in candidates.csv (audit) but do not
    # render them on concept pages, where they would read as the current value.
    rows = [row for row in rows if row.get("status") not in SUPERSEDED_STATUSES]

    # Objects of attribute (literal-valued) relations are values, not first-class
    # entities (see common.entity_set), so they get no concept page — consistent
    # with entity listings/path nodes and avoiding a page per free-text value.
    # Shared predicate, so a concept page is created for exactly the objects the
    # engine and the vocabulary treat as entities.
    literal_rels = attribute_relation_forms()
    by_entity: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        by_entity.setdefault(row["subject"], []).append(row)
        if row["object"] and not is_attribute_relation(row["relation"], literal_rels):
            by_entity.setdefault(row["object"], []).append(row)

    # Assign collision-safe page filenames deterministically (#258). slugify
    # collapses every non-alphanumeric run to '-', so distinct entities like
    # "A B" and "A-B" share one slug and would overwrite each other's page (a
    # silent concept-page loss; the rows survive in candidates.csv). Group by base
    # filename over the sorted entities; when a base name is claimed by >1 entity,
    # suffix EACH colliding entity with a short stable hash of its full title — the
    # same disambiguation the long-title (NAME_MAX) guard already uses. Non-
    # colliding names are untouched, so the common case keeps its readable slug.
    named_entities = [e for e in sorted(by_entity) if e.strip()]
    base_name: dict[str, str] = {e: page_filename(e) for e in named_entities}
    base_count: dict[str, int] = {}
    for name in base_name.values():
        base_count[name] = base_count.get(name, 0) + 1
    # Assign GLOBALLY-unique names, not merely base-unique. A base-only collision
    # check would let a hash-suffixed disambiguation (e.g. "A B" -> a-b-<sha1>.md)
    # re-collide with ANOTHER entity whose own clean slug is literally that suffixed
    # name (a distinct title `f"A B {sha1('A B')[:10]}"` slugifies to a-b-<sha1>),
    # reopening the #258 silent overwrite by a second path. Walk entities in sorted
    # order tracking every claimed name: keep a clean slug only when its base is
    # unique AND still free; otherwise disambiguate with the full-title hash and, if
    # that suffix is itself taken (adversarial), bump a counter until free.
    # Deterministic: sorted input + deterministic tie-break.
    used: set[str] = set()
    page_name: dict[str, str] = {}
    for entity in named_entities:
        name = base_name[entity]
        if base_count[name] == 1 and name not in used:
            page_name[entity] = name
            used.add(name)
            continue
        digest = hashlib.sha1(entity.encode("utf-8")).hexdigest()[:10]
        stem = name[:-3]  # strip '.md', re-add below
        candidate = f"{stem}-{digest}.md"
        bump = 1
        while candidate in used:
            candidate = f"{stem}-{digest}-{bump}.md"
            bump += 1
        page_name[entity] = candidate
        used.add(candidate)
    # Defensive invariant: distinct named entities map to distinct pages. A breach
    # would mean a silent page-view overwrite — exactly the #258 failure (and its
    # reverse-collision variant) this pre-pass exists to prevent.
    assert len(set(page_name.values())) == len(named_entities), (
        "write_pages: page filename collision — distinct entities share a page"
    )

    written: list[str] = []
    for entity, entity_rows in sorted(by_entity.items()):
        if not entity.strip():
            continue
        path = pages_dir / page_name[entity]
        # Skip hand-authored pages that don't carry our marker.
        if path.exists() and GENERATED_PAGE_MARKER not in path.read_text(encoding="utf-8", errors="ignore"):
            continue

        source_lines = sorted({f"- {row['source']}" for row in entity_rows})
        # A source may carry a #anchor (sources/a.md#s1, sources/a.md#s2 = same doc),
        # so count distinct FILE paths with the anchor stripped and NFC-normalized —
        # the same anchor-strip primitive fact_signals/status keys use. (An original
        # under sources/ and its runs/sources/ conversion are counted as two paths;
        # in practice facts cite only the conversion, so this is a rare over-count and
        # a benign, human-visible one — never data loss.)
        doc_keys = {
            unicodedata.normalize("NFC", row["source"].partition("#")[0])
            for row in entity_rows
            if row["source"]
        }
        relation_lines: list[str] = []
        review_lines: list[str] = []

        for row in entity_rows:
            if row["subject"] == entity:
                relation_lines.append(
                    f"- {row['relation']} -> [[{row['object']}]]"
                    f" ({row['source']}, confidence={row['confidence']})"
                )
            else:
                relation_lines.append(
                    f"- [[{row['subject']}]] -> {row['relation']}"
                    f" ({row['source']}, confidence={row['confidence']})"
                )
            if row["status"] in REVIEW_STATUSES:
                review_lines.append(
                    f"- {row['subject']} / {row['relation']} / {row['object']}"
                    f" (confidence={row['confidence']}): {row['note']}"
                )

        sources_block = "\n".join(source_lines or ["- 확인된 source가 없습니다."])
        if len(doc_keys) >= 2:
            sources_block = (
                f"- 참고: 서로 다른 문서 {len(doc_keys)}개에서 수집된 사실입니다 "
                f"(동음이의어 병합 가능성 확인 필요).\n"
            ) + sources_block

        text = render_page(
            template,
            entity,
            sources_block,
            "\n".join(relation_lines or ["- 확인된 관계가 없습니다."]),
            "\n".join(review_lines or ["- 현재 추출 결과에서 별도 검토 항목이 없습니다."]),
        )
        path.write_text(text, encoding="utf-8")
        written.append(path.relative_to(root).as_posix())

    return written


# ---------------------------------------------------------------------------
# Write decisions/open-questions.md
# ---------------------------------------------------------------------------

def decision_section(row: dict[str, str]) -> str:
    """Which review *category* the row belongs to, as a review_sections keyword.

    A keyword, not a heading: the heading is whatever the target file already calls
    that category, which only :func:`section_for` can say (#495). Returning a fixed
    heading here is what opened a second "## 모호한 관계명" in a KB that already had
    a "## 모호 (...)" section — the bullets went to the new one and the section a
    human reads stayed empty.
    """
    text = " ".join(row.get(key, "") for key in ["subject", "relation", "object", "note"]).lower()
    if any(word in text for word in ["duplicate", "same_concept", "same_as", "동일", "중복"]):
        return "중복"
    if any(word in text for word in ["source", "evidence", "출처"]):
        return "출처"
    if any(word in text for word in ["conflict", "충돌"]):
        return "충돌"
    return "모호"


def insert_bullet(text: str, keyword: str, bullet: str) -> str:
    """*text* with *bullet* filed under its *keyword* section.

    A **category keyword**, not a heading. `section_for` answers with a `Heading`,
    and a `Heading` is a pair of line indices — coordinates that are only valid for
    the exact document they were read from. Taking one as an argument made a stale
    coordinate expressible, and the shape that produced one is the obvious
    refactoring: resolve the section once, then file several bullets in a loop. Under
    `origin/main` that was harmless, because `section_for` returned a *string* and
    `insert_bullet` looked it up again in the current text every time.

    It is not harmless now, and it is silent. Measured on a freshly scaffolded file,
    hoisting the lookup out of the loop filed the 출처 and 충돌 rows into the 모호
    section — with `missing_review_sections` empty, `split_review_sections` empty and
    the run exiting 0, which is every gate this cluster exists to keep honest saying
    the file is fine while rows sit under the wrong heading.

    So the coordinates are not passed in; they are read here, from the text they
    belong to, and the caller cannot hold one across a write. The lookup costs
    nothing that was not already being paid — this function already walks the same
    text for `bullets`, for `section_body_end` and for `headings`.
    """
    lines = text.splitlines()
    # Idempotency by exact line, not substring: a plain `bullet in text` dropped a
    # new bullet whenever it was a prefix-substring of a longer existing bullet
    # (e.g. "...note" skipped because "...note extra" was already present).
    #
    # Against the bullets md_lines counts, not every line in the file. Matching
    # fenced lines too meant a KB that documents its own bullet format in a code fence
    # silently swallowed the first real bullet identical to that example — and
    # validate.py, counting the same fenced line as a review bullet, then reported the
    # file complete. One reader, so the two cannot disagree about it.
    if bullet.rstrip() in {line.rstrip() for line in bullets(text)}:
        return text
    # Where the section ends is md_lines' to answer — the same answer the scaffolder
    # and the validator get. This was a `## ` prefix scan of its own, which found
    # headings *inside code fences*: measured, a bullet was filed against a
    # fenced heading and written just past the closing fence, under no section at
    # all, and the run exited 0.
    #
    # Where the section *starts* is `section_for`'s answer and is never looked up by
    # line equality again — `lines.index(section)` asked a different question, "which
    # line says this", whose answer is the first line of the *body* that happens to
    # repeat the heading's text. Resolved here rather than by the caller so that the
    # heading and the text it indexes into cannot come apart; see the docstring.
    #
    # After the duplicate check, not before: a bullet that is already filed is
    # answered without needing a section at all, and `section_for` raises when a
    # category has none.
    section = section_for(text, keyword)
    boundary = section_body_end(text, section)
    # The bullet needs a blank line on either side of it, and both questions are
    # about the document as it is *now* — before anything moves. So both are asked in
    # `text`'s coordinates, answered here, and applied as one splice at `boundary`.
    #
    # One splice on purpose. This used to be two inserts, and the first of them
    # shifted the index the second was still comparing against: whenever a blank line
    # was opened *above* the bullet, the blank line *below* it was silently skipped.
    # Measured — and not only on Setext. On an ordinary ATX file the bullet ended up
    # flush against the next `## `, which is a regression against main; on a Setext
    # one the blank line is what makes the following heading a heading at all, so the
    # next section stopped existing, `missing_review_sections` reported it, and the
    # following merge appended a second section for a category that already had one.
    # That last step is the exact damage #500 is about, re-created by the fix for it.
    #
    # Leading blank: the bullet goes at the end of the section's body, and if that
    # body ends in a content line the bullet would otherwise abut it.
    lead = boundary > section.end and bool(lines[boundary - 1].strip())
    # Trailing blank: is the line the bullet will be pushed in front of the start of
    # a heading? Asked of md_lines rather than of a `## ` prefix — a Setext heading
    # has no `## ` on it, so the prefix test let the bullet's list swallow the title
    # line below it as a lazy continuation and turn its `----` into a horizontal
    # rule. The scan said the section was there and a renderer showed none, which is
    # the disagreement this module pair exists to prevent. Asked of `text`, because
    # after the insert the damage is already done and the scan agrees with it.
    trail = any(found.start == boundary for found in headings(text))
    lines[boundary:boundary] = ([""] if lead else []) + [bullet] + ([""] if trail else [])
    return "\n".join(lines).rstrip() + "\n"


# Which files this process has already complained about. Printing only — the refusal
# itself is decided from the text every time; see refuse_unclosed_fence. A second run
# in the same process is therefore quiet, which the CLI (one run per process) never
# sees, but a caller looping over KBs still hears about each distinct file.
_FENCE_WARNED: set[Path] = set()


def refuse_unclosed_fence(root: Path, text: str) -> bool:
    """Is *text* unwritable because it ends inside an open code fence? Say so if it is.

    Everything appended to such a file lands inside the fence. `ensure_review_sections`
    already declines to add headings there, but declining is not enough on its own:
    `insert_bullet` appends its own `## heading` + bullet when the section is not
    found, and *that* append went into the fence too — measured, a needs_review item
    was written into a code block, invisible in any rendered view, while the run
    exited 0 and the validator complained about a heading sitting in plain sight.

    So both writers stop here, before writing anything, and name the line. The row
    itself is not lost: it stays `needs_review` in facts/candidates.csv, which is the
    queue's source of truth, and the mirror into open-questions.md resumes as soon as
    the fence is closed.

    The refusal is re-decided from *text* every call. Only the printing is
    remembered, so that one file draws one complaint from the two writers a run calls
    — an earlier version returned early on the remembered path, which made the second
    writer's refusal a cache hit rather than a decision.
    """
    if not ends_inside_fence(text):
        return False
    decisions = (root / "decisions" / "open-questions.md").resolve()
    if decisions not in _FENCE_WARNED:
        _FENCE_WARNED.add(decisions)
        print(
            f"  WARNING: decisions/open-questions.md opens a code fence on line "
            f"{unclosed_fence_line(text)} and never closes it, so everything after it "
            f"is code. Nothing was written to that file this run — close the fence and "
            f"re-run merge.",
            file=sys.stderr,
        )
    return True


def write_decisions(root: Path, rows: list[dict[str, str]]) -> list[str]:
    decisions = root / "decisions" / "open-questions.md"
    decisions.parent.mkdir(parents=True, exist_ok=True)
    text = decisions.read_text(encoding="utf-8") if decisions.exists() else OPEN_QUESTIONS_SCAFFOLD
    if refuse_unclosed_fence(root, text):
        return []
    # The four sections are an invariant of the file, not a side effect of having
    # had something to review: a KB that has never produced a needs_review row still
    # has to validate (#495). Only categories with no heading at all are added.
    text = ensure_review_sections(text)
    added: list[str] = []
    for row in rows:
        if row["status"] not in REVIEW_STATUSES:
            continue
        bullet = (
            f"- needs_review: {row['subject']} / {row['relation']} / "
            f"{row['object']} ({row['source']}, confidence={row['confidence']}) - {row['note']}"
        )
        before = text
        text = insert_bullet(text, decision_section(row), bullet)
        if text != before:
            added.append(bullet)
    decisions.write_text(text, encoding="utf-8")
    return added


# ---------------------------------------------------------------------------
# Record stale page references
# ---------------------------------------------------------------------------

def existing_source_refs(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="ignore")
    # Match the ingestible text extensions a page can cite (md/txt/csv — pdftotext
    # and textutil emit .txt, direct text sources may be .txt/.csv), with the
    # optional runs/ prefix, for parity with validate.py. An .md-only pattern
    # silently never recorded stale refs to deleted .txt/.csv sources.
    return re.findall(r"(?:runs/)?sources/[^\s`)>,]+?\.(?:md|txt|csv)(?:#[^\s`)>,]+)?", text)


def record_stale_page_refs(root: Path) -> list[str]:
    known_sources = source_file_refs(root)
    decisions = root / "decisions" / "open-questions.md"
    text = decisions.read_text(encoding="utf-8") if decisions.exists() else OPEN_QUESTIONS_SCAFFOLD
    if refuse_unclosed_fence(root, text):
        return []
    text = ensure_review_sections(text)
    added: list[str] = []
    for page in sorted((root / "pages").glob("*.md")):
        for ref in existing_source_refs(page):
            source_file = unicodedata.normalize("NFC", ref.partition("#")[0])
            if source_file in known_sources:
                continue
            bullet = f"- stale_source: {page.relative_to(root).as_posix()} references removed source {ref}"
            before = text
            text = insert_bullet(text, "출처", bullet)
            if text != before:
                added.append(bullet)
    decisions.write_text(text, encoding="utf-8")
    return added


# ---------------------------------------------------------------------------
# Validation delegate — repoints to tools/validate.py (u5)
# ---------------------------------------------------------------------------

def validate_outputs(root: Path) -> int:
    """Delegate structural validation to tools/validate.py.

    This replaces the old run_fact_sync.py reference to validate_fact_sync.py.
    """
    validator = Path(__file__).parent / "validate.py"
    if not validator.is_file():
        print(f"warning: validate.py not found at {validator}", file=sys.stderr)
        return 0
    completed = subprocess.run(
        [sys.executable, str(validator), str(root)],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.stdout.strip():
        print(completed.stdout.strip())
    if completed.stderr.strip():
        print(completed.stderr.strip(), file=sys.stderr)
    return completed.returncode


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def existing_superseded_keys(root: Path) -> set[tuple[str, str, str, str]]:
    """(subject, relation, object, source) of rows currently marked 'superseded'
    in candidates.csv. Preserved across re-merge so a human's conflict resolution
    (or `factlog eject --fact`) sticks even when a run re-asserts the retired fact.

    The source is keyed WITHOUT its '#anchor' (partitioned on '#', same as
    normalize_rows' source-existence check), so a supersession survives a later
    sync that re-asserts the same triple from the same file with a drifted
    section anchor (sources/a.md#sec3 -> sources/a.md). Different files stay
    distinct, so a supersession is still scoped to the source file it came from."""
    csv_path = root / "facts" / "candidates.csv"
    if not csv_path.is_file():
        return set()
    keys: set[tuple[str, str, str, str]] = set()
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip() in SUPERSEDED_STATUSES:
                keys.add(
                    fact_key(
                        row.get("subject") or "",
                        row.get("relation") or "",
                        row.get("object") or "",
                        row.get("source") or "",
                    )
                )
    return keys


def existing_superseded_rows(root: Path) -> list[dict[str, str]]:
    """Full candidates.csv rows currently marked 'superseded' (FACT_HEADER fields).

    Companion to existing_superseded_keys: the keys drive re-status preservation
    for a fact a run re-asserts; these full rows let a tombstone be carried over
    even when NO run asserts the fact anymore, so a human supersession is durable
    regardless of whether the backing run still exists (#260)."""
    csv_path = root / "facts" / "candidates.csv"
    if not csv_path.is_file():
        return []
    rows: list[dict[str, str]] = []
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip() in SUPERSEDED_STATUSES:
                rows.append({k: (row.get(k) or "") for k in FACT_HEADER})
    return rows


def existing_keepable_rows(root: Path) -> list[dict[str, str]]:
    """candidates.csv rows a rebuild would DESTROY — everything but tombstones.

    `superseded` rows are carried over by existing_superseded_rows() regardless of
    runs/, so they survive a rebuild. Every other row is reconstructed from
    runs/*.json and vanishes if runs/ is empty (#218)."""
    csv_path = root / "facts" / "candidates.csv"
    if not csv_path.is_file():
        return []
    with csv_path.open(newline="", encoding="utf-8") as f:
        return [
            {k: (row.get(k) or "") for k in FACT_HEADER}
            for row in csv.DictReader(f)
            if (row.get("status") or "").strip() not in SUPERSEDED_STATUSES
        ]


def existing_engine_keys(root: Path) -> dict[tuple[str, str, str, str], str]:
    """Map (subject, relation, object, source-without-#anchor) -> engine status
    for rows currently marked confirmed/accepted in candidates.csv.

    Preserved across re-merge so a human acceptance (`factlog accept` /
    `amend --accept`, or a hand-confirm) sticks even though extraction re-asserts
    the fact as `candidate` — symmetric with existing_superseded_keys. The exact
    engine status (confirmed vs accepted) is kept. Anchor-insensitive key, like
    the superseded preservation."""
    csv_path = root / "facts" / "candidates.csv"
    if not csv_path.is_file():
        return {}
    keys: dict[tuple[str, str, str, str], str] = {}
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            status = (row.get("status") or "").strip()
            if status in ENGINE_STATUSES:
                key = fact_key(
                    row.get("subject") or "",
                    row.get("relation") or "",
                    row.get("object") or "",
                    row.get("source") or "",
                )
                keys[key] = status
    return keys


def existing_review_keys(root: Path) -> set[tuple[str, str, str, str]]:
    """Keys (subject, relation, object, source-without-#anchor) a human pulled back
    to `needs_review` in the live candidates.csv.

    Preserved across re-merge so a deliberate re-review survives: a stale
    runs/*.json value left from an earlier accept (status 'accepted') must not
    silently re-promote a fact the human downgraded. Only `needs_review` is held
    (not the default `candidate`), and superseded is stronger and still wins.
    Anchor-insensitive key, like existing_engine_keys / existing_superseded_keys."""
    csv_path = root / "facts" / "candidates.csv"
    if not csv_path.is_file():
        return set()
    keys: set[tuple[str, str, str, str]] = set()
    with csv_path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if (row.get("status") or "").strip() == "needs_review":
                keys.add(fact_key(
                    row.get("subject") or "",
                    row.get("relation") or "",
                    row.get("object") or "",
                    row.get("source") or "",
                ))
    return keys


def implicit_target_refusal(root: Path, source: str, cwd: Path) -> str | None:
    """Why this run may not write to *root*, or None if it may (#532).

    ``source`` is ``factlog_config.resolve_root``'s second answer: 'flag', 'env',
    'config' or 'cwd'. Only 'config' is a target nobody named — neither the command
    line nor the environment nor the directory the caller is standing in — and it is
    the one that made a merge run from ``/tmp`` rewrite the configured KB's
    candidates.csv and pages/, flipping its logic report to STALE with exit 0.

    The refusal stands on what THIS run would do — rewrite ``facts/candidates.csv``,
    ``pages/`` and ``decisions/open-questions.md`` in a KB the caller never named,
    invalidating its logic report — not on a comparison with the other steps. That
    comparison does not hold still: the config tier is being extended to the sibling
    write steps (#527 compile_facts, #528 run_logic_check, #529 finalize), which gives
    each of them the same exposure this guard exists for. Per the decision on those
    issues, finalize carries a matching refusal (#529), while compile_facts and
    run_logic_check write only derived artifacts and are left to a follow-up.

    Announcing the target instead of refusing was the other option on the issue, and
    main() does announce it — but announcing alone cannot be the whole fix here,
    because this script ALREADY printed ``wiki=<root>`` before every write and the
    incident happened anyway. A line on stdout is a fine way to make an intended
    write auditable; it is not a way to stop an unintended one.

    'cwd' needs no refusal: the root IS the cwd, so a non-KB there is caught by
    ``ensure_dirs``. And a config root the caller is standing inside is not implicit —
    running the merge from the KB (or a subdirectory of it) with no flag is the
    documented workflow, and the shell harness's ``--wiki``/``FACTLOG_ROOT`` calls are
    'flag'/'env'.
    """
    if source != "config":
        return None
    if cwd == root or root in cwd.parents:
        return None
    return (
        f"merge_candidates: REFUSING to write to {root} — that KB comes from the active-KB "
        f"config, not from this command, and the current directory ({cwd}) is not inside it.\n"
        f"  This run would rewrite facts/candidates.csv, pages/ and "
        f"decisions/open-questions.md there, invalidating that KB's logic report (#532).\n"
        f"  Name the target explicitly:\n"
        f"    python3 tools/merge_candidates.py --wiki {root}   (--target is the same flag)\n"
        f"  or export FACTLOG_ROOT={root}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Merge Claude-extracted candidate rows (runs/*.json) into the factlog KB.\n"
            "\n"
            "Input: JSON files under $FACTLOG_ROOT/runs/ produced by the native\n"
            "Claude fact-extraction step in the factlog skill.  Each file must\n"
            "contain a JSON array of objects with the FACT_HEADER schema:\n"
            "  [subject, relation, object, source, status, confidence, note]\n"
            "\n"
            "The 'source' field MUST be a sources/- or runs/sources/-prefixed path relative to the KB\n"
            "root (e.g. 'sources/my-doc.md').  Bare filenames are not accepted.\n"
            "\n"
            "Output: facts/candidates.csv, pages/, decisions/open-questions.md"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    # The root flag was already consumed by the pre-pass above; declaring it again
    # here is what puts it in --help and what makes a misspelled `--targt /path`
    # exit 2 instead of being silently ignored and rewriting whatever the config/cwd
    # tier resolved to. No argparse `default=`: the default is not a constant but a
    # resolution over env → config → cwd, and the old
    # `os.environ.get("FACTLOG_ROOT", ".")` described a rule this tool has not
    # followed since the pre-pass landed (#531).
    parser.add_argument(
        *_ROOT_FLAGS,
        dest="target",
        default=None,
        metavar="PATH",
        help=(
            "KB root (--wiki is an alias; authoritative, and sets FACTLOG_ROOT for "
            "the delegated steps). Overrides $FACTLOG_ROOT and the active-KB "
            "config; without it the root is resolved as $FACTLOG_ROOT > active-KB "
            "config > cwd."
        ),
    )
    parser.add_argument(
        "--input",
        default=None,
        help=(
            "glob pattern relative to KB root for input JSON files "
            "(default: runs/*.json via common.RUNS_DIR)"
        ),
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="exit non-zero if any input row is rejected during normalisation",
    )
    parser.add_argument(
        "--allow-delete",
        action="store_true",
        help=(
            "permit a rebuild that deletes facts a human accepted (DESTRUCTIVE; only when the "
            "loss is intended — normally restore runs/*.json instead, see #218)"
        ),
    )
    args = parser.parse_args()

    # An empty flag value is refused rather than dropped to the next tier (#546).
    # SKILL.md recommends `merge_candidates.py --wiki "$FACTLOG_ROOT"`, which in a
    # shell that never exported the variable is exactly `--wiki ""` — so this is a
    # shape the documentation produces, not a hypothetical. Refusing here is the
    # same answer tools/validate.py gives a blank target (#530).
    if args.target is not None and not args.target.strip():
        print(
            "merge_candidates: the KB-root flag (--target/--wiki) was empty; pass a KB path, "
            "or pass no flag at all to use the active KB.",
            file=sys.stderr,
        )
        return 1

    # TARGET_ROOT, not a second resolution of args.target. The root was resolved once,
    # in the pre-pass, and that is the value common's path globals were bound to and
    # the value TARGET_SOURCE describes. Re-deriving it here from argv is what made a
    # blank flag land on cwd while the guard below judged the config tier and the
    # announcement printed "(from config)" over a cwd path — the write went through,
    # unrefused, under a false provenance (#546). One resolution, one target, one
    # label.
    root = Path(TARGET_ROOT)

    # Name the KB about to be written and where that choice came from, before
    # anything is read or written — the same line `factlog ingest`/`pubmed-*` print
    # (cli.py: "target KB {target} (from {source})"). The run_id line below already
    # showed `wiki=`, but not the provenance, which is the part that tells a reader
    # this run picked the KB up from config rather than from their command.
    #
    # Which factlog is doing the writing comes first (#554) — after the strict parse
    # above, so --help and a rejected argument stay pure argparse output with no
    # execution context in stdout.
    print(provenance_line())
    print(f"merge_candidates: target KB {root} (from {TARGET_SOURCE})")
    refusal = implicit_target_refusal(root, TARGET_SOURCE, Path.cwd().resolve())
    if refusal is not None:
        print(refusal, file=sys.stderr)
        return 1

    # Derive the default --input glob from common.RUNS_DIR so the runs/ location
    # is single-sourced from common.py.
    if args.input is not None:
        input_pattern = args.input
    else:
        try:
            input_pattern = f"{RUNS_DIR.relative_to(root)}/*.json"
        except ValueError:
            input_pattern = "runs/*.json"

    # Validate KB structure and ensure output directories exist.
    ensure_dirs()

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"merge_candidates run_id={run_id} wiki={root}")

    # ------------------------------------------------------------------
    # 0. Surface binary source files in sources/ that have NO runs/sources/
    # conversion. Extraction reads sources/ as text, so an un-ingested binary
    # yields no facts (silent non-ingestion). Warn always; under --strict treat
    # their presence as a hard failure.
    # ------------------------------------------------------------------
    nontext_sources = unconverted_binary_sources(root)
    if nontext_sources:
        print(
            f"  WARNING: {len(nontext_sources)} binary source file(s) in sources/ "
            "have no runs/sources/ conversion and will yield no facts "
            "(run `factlog ingest --scan` to convert them):",
            file=sys.stderr,
        )
        for name in nontext_sources:
            print(f"    - {name}", file=sys.stderr)

    # ------------------------------------------------------------------
    # 1. Read candidate rows from runs/*.json
    # ------------------------------------------------------------------
    raw_rows = load_candidate_files(root, input_pattern)
    print(f"  candidate rows loaded: {len(raw_rows)}")

    # ------------------------------------------------------------------
    # 1b. Refuse to erase a populated KB (#218)
    # ------------------------------------------------------------------
    # runs/*.json is the SOURCE OF TRUTH for candidates.csv, which is rebuilt from
    # it on every merge. If runs/ is gone but candidates.csv still holds facts, the
    # rebuild writes an (almost) empty table: every accepted fact a human reviewed
    # is destroyed, and only `superseded` rows survive. That happened silently, and
    # finalize then reported "no contradictions" over an empty engine input.
    #
    # runs/ is easy to lose: the docs call it an engine artifact and the shipped
    # .gitignore excludes it, so a version-controlled KB loses every fact on a
    # fresh clone. Until that contract is settled, refuse the destructive write.
    if not raw_rows:
        keepable = existing_keepable_rows(root)
        if keepable and not args.allow_delete:
            print(
                f"merge_candidates: REFUSING to rebuild facts/candidates.csv — '{input_pattern}' yielded 0 "
                f"candidate rows but candidates.csv still holds {len(keepable)} fact(s).\n"
                f"  runs/*.json is the source of truth for candidates.csv; rebuilding from an empty runs/ "
                f"would destroy every reviewed fact and leave only superseded rows.\n"
                f"  Restore runs/*.json (version-control it with the KB — see #218), or accept the loss:\n"
                f"    python3 tools/merge_candidates.py --wiki {root} --allow-delete",
                file=sys.stderr,
            )
            return 1

    # ------------------------------------------------------------------
    # 2. Normalise and deduplicate
    # ------------------------------------------------------------------
    # Rows rejected for a missing source are kept aside for the drop-impact report
    # (#538); it can only run once the status-preservation passes below have settled
    # what this merge's engine input actually is.
    dropped_rows: list[dict[str, str]] = []
    rows = normalize_rows(root, raw_rows, strict=args.strict, dropped_rows=dropped_rows)
    print(f"  rows after normalise/dedup: {len(rows)}")

    # Preserve human-marked supersessions across re-merge: a row previously set
    # to 'superseded' in candidates.csv stays superseded even if a run re-asserts
    # it — so a conflict resolution (see check_conflicts.py) is durable.
    superseded_keys = existing_superseded_keys(root)
    if superseded_keys:
        preserved = 0
        for row in rows:
            # Anchor-insensitive key (see existing_superseded_keys): a drifted
            # section anchor must not let a retired fact slip back in.
            key = fact_key(row["subject"], row["relation"], row["object"], row["source"])
            if key in superseded_keys and row["status"] not in SUPERSEDED_STATUSES:
                row["status"] = "superseded"
                preserved += 1
        if preserved:
            print(f"  preserved {preserved} superseded row(s) from candidates.csv")

    # Carry over superseded tombstones the current runs no longer assert (#260): a
    # human supersession must be durable even if the backing run was replaced or
    # removed (e.g. a source re-extracted without the fact). Rows a run re-asserts
    # were re-marked superseded above; rows absent from this run are re-added from
    # candidates.csv so a retired fact cannot silently resurrect as 'candidate' on
    # a later re-assertion. Anchor-insensitive key, matching the preservation above.
    present_keys = {
        fact_key(row["subject"], row["relation"], row["object"], row["source"])
        for row in rows
    }
    carried = 0
    for tombstone in existing_superseded_rows(root):
        key = fact_key(
            tombstone["subject"],
            tombstone["relation"],
            tombstone["object"],
            tombstone["source"],
        )
        if key not in present_keys:
            rows.append(tombstone)
            present_keys.add(key)
            carried += 1
    if carried:
        print(f"  carried over {carried} superseded tombstone(s) not re-asserted by runs")

    # Preserve human acceptances across re-merge: a row a human set to
    # confirmed/accepted stays so even though extraction re-asserts it as
    # 'candidate' — symmetric with the superseded preservation above. Skip rows
    # now superseded (a later rejection wins over an earlier acceptance).
    engine_keys = existing_engine_keys(root)
    if engine_keys:
        restored = 0
        for row in rows:
            key = fact_key(row["subject"], row["relation"], row["object"], row["source"])
            want = engine_keys.get(key)
            if want and row["status"] not in SUPERSEDED_STATUSES and row["status"] != want:
                row["status"] = want
                restored += 1
        if restored:
            print(f"  preserved {restored} human-accepted row(s) from candidates.csv")

    # Respect a deliberate human re-review: a row the human pulled back to
    # needs_review in candidates.csv must not be silently re-promoted by a stale
    # engine status carried in runs/*.json (e.g. left from an earlier accept).
    # Only engine statuses are overridden — a re-asserted 'candidate' is untouched
    # — and superseded (handled above) still wins.
    #
    # Ordered BEFORE the ratchet so `rows` carries its final statuses on both sides of
    # it: the ratchet may return, and the drop-impact report it prints there has to
    # measure against the same engine input the success path would (#538). The ratchet
    # itself only reads KEYS (present_keys, and engine/review keys off disk), so
    # nothing it decides depends on the statuses this pass rewrites.
    review_keys = existing_review_keys(root)
    if review_keys:
        held = 0
        for row in rows:
            key = fact_key(row["subject"], row["relation"], row["object"], row["source"])
            if key in review_keys and row["status"] in ENGINE_STATUSES:
                row["status"] = "needs_review"
                held += 1
        if held:
            print(f"  held {held} human-reviewed row(s) at needs_review (re-review respected)")

    # --- the ratchet (#218) -------------------------------------------------
    # A row a human has ruled on may only leave candidates.csv through a human
    # gate: `factlog eject --fact` retires it as a tombstone (carried over above).
    # If this merge would drop such a row because the runs no longer assert it, the
    # backing runs/*.json was LOST, not retired — and the rebuild would erase the
    # decision.
    #
    # Guarding only "runs/ is empty" was a cliff, not a ratchet: the real disaster
    # is a KB cloned without runs/ and then re-synced, where extraction writes a
    # FEW rows, the empty-check stays silent, and every fact that was not
    # re-extracted vanishes with exit 0. The question is not how many rows came in;
    # it is whether this merge destroys a human decision.
    #
    # "Ruled on" means engine statuses (confirmed/accepted) AND needs_review: the
    # empty-runs guard above already counts needs_review as keepable, and guarding
    # one but not the other gave the absurd split of refusing a total loss while
    # silently accepting a partial one. One definition, both guards.
    protected = dict(engine_keys)
    protected.update(dict.fromkeys(review_keys, "needs_review"))
    destroyed = sorted(key for key in protected if key not in present_keys)
    if destroyed and not args.allow_delete:
        print(
            f"merge_candidates: REFUSING to rebuild facts/candidates.csv — it would delete "
            f"{len(destroyed)} fact(s) a human has ruled on, which the current runs/ no longer "
            f"assert.\n"
            f"  Such a fact may only leave the KB through `factlog eject --fact SUBJECT RELATION "
            f"OBJECT` (which leaves a tombstone). `factlog reject` will NOT do it — it only "
            f"retires rows still pending. Its backing runs/*.json is missing, so this rebuild "
            f"would erase the decision instead:",
            file=sys.stderr,
        )
        for subject, relation, object_, source in destroyed[:10]:
            print(f"    - {subject} / {relation} / {object_}   ({source})", file=sys.stderr)
        if len(destroyed) > 10:
            print(f"    ... and {len(destroyed) - 10} more", file=sys.stderr)
        print(
            f"  Restore runs/*.json (version-control it with the KB — see #218), or re-run with "
            f"--allow-delete to accept the loss:\n"
            f"    python3 tools/merge_candidates.py --wiki {root} --allow-delete",
            file=sys.stderr,
        )
        # The refusal lists the facts at stake; it does not say what ANSWERS are at
        # stake, and the reader standing here has to decide whether to pass
        # --allow-delete. This is the path where the loss is largest — every destroyed
        # key is a row a human ruled on — so it is the last path that should be quiet
        # about which declared questions the deletion would cost (#538).
        report_drop_impact(root, rows, dropped_rows, superseded_keys, engine_keys, review_keys)
        return 1

    # `rows` is now exactly what candidates.csv will hold, so the projection
    # engine_atoms() makes is the engine input this merge produces — the only basis
    # available for "which relation is gone" while the merge is still running (#538).
    report_drop_impact(root, rows, dropped_rows, superseded_keys, engine_keys, review_keys)

    # Warn prominently when rows were loaded but all were dropped — this
    # distinguishes a silent contract violation from a genuinely empty run.
    if raw_rows and not rows:
        print(
            "  WARNING: all candidate rows were dropped during normalise/dedup "
            "(0 rows survived).  Check that source values are sources/- or runs/sources/-prefixed paths.",
            file=sys.stderr,
        )
        if args.strict:
            return 1

    # ------------------------------------------------------------------
    # 3. Write facts/candidates.csv
    # ------------------------------------------------------------------
    write_facts(root, rows)
    print(f"  facts/candidates.csv written ({len(rows)} rows)")

    # ------------------------------------------------------------------
    # 4. Record stale page references (before pages are re-generated)
    # ------------------------------------------------------------------
    stale_refs_added = record_stale_page_refs(root)
    print(f"  stale page refs recorded: {len(stale_refs_added)}")

    # ------------------------------------------------------------------
    # 5. Write pages/<entity>.md
    # ------------------------------------------------------------------
    pages_written = write_pages(root, rows)
    print(f"  pages written: {len(pages_written)}")

    # ------------------------------------------------------------------
    # 6. Write decisions/open-questions.md bullets for needs_review rows
    # ------------------------------------------------------------------
    decisions_added = write_decisions(root, rows)
    print(f"  decision bullets added: {len(decisions_added)}")

    # ------------------------------------------------------------------
    # 7. Delegate structural validation to tools/validate.py
    # Validation is informational: merge_candidates exits 0 on success
    # regardless of validation result.  Callers that need strict validation
    # should run tools/validate.py directly.
    # ------------------------------------------------------------------
    validation_code = validate_outputs(root)
    if validation_code != 0:
        print(f"  validation warnings (exit {validation_code}) — run validate.py for details", file=sys.stderr)

    if args.strict and nontext_sources:
        print(
            f"--strict: {len(nontext_sources)} unconverted binary source file(s) in sources/",
            file=sys.stderr,
        )
        return 1

    return 0


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
