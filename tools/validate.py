#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Validate factlog KB outputs."""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
from pathlib import Path

from common import FACT_HEADER, KNOWN_STATUSES, source_files

# Imported after ``common``, which is what puts the repo root on sys.path — this
# module is run as a script from tools/, so ``factlog`` is not importable before
# that line. Reordering these two breaks `python tools/validate.py`.
from factlog.front_matter_scan import (  # noqa: E402
    FRONT_MATTER_NO_OPENING_FENCE,
    FRONT_MATTER_UNCLOSED,
    FRONT_MATTER_UNSCANNED,
    front_matter_absence,
)
from factlog.integrations.common.source_writer import (  # noqa: E402
    IDENTITY_KEYS_BY_SOURCE,
)
from factlog.md_lines import bullets, unclosed_fence_line  # noqa: E402
from factlog.review_sections import (  # noqa: E402
    missing_review_sections,
    split_review_sections,
)

# An unregistered status is an *error* here, not a warning — so this set drifting
# from the vocabulary is worse than the #208 warning bug. Derive, never restate.
VALID_STATUSES = KNOWN_STATUSES

# The absence reasons worth telling an operator about. Not every ``None`` from the
# reader is one: a file with no opening fence is the normal shape of an ingest
# conversion, which carries an HTML provenance comment instead of YAML, and an
# undecodable file is a different complaint with a different remedy. These two are
# the ones where a source *looks* imported to a human and does not to the reader.
WARNED_FRONT_MATTER_ABSENCES = (FRONT_MATTER_UNCLOSED, FRONT_MATTER_UNSCANNED)

# What the operator loses while the block stays unreadable — the same cost the
# reader's fail-closed policy documents, stated where it can still be prevented.
FRONT_MATTER_CONSEQUENCE = (
    "the source reads as not yet imported, so it drops out of de-duplication and "
    "a re-import writes a duplicate .md instead of updating this one"
)

# The far side of #422: a source whose *opening* fence a human deleted, not its
# closing one. That reads as ``FRONT_MATTER_NO_OPENING_FENCE`` — the same reason an
# ingest conversion (HTML provenance comment) or a hand-written note carries, which
# is the ordinary majority of a source tree and must not be warned on (measured: in
# a real KB every source read as no-opening-fence). What sets the damaged file apart
# is its first line. Each writer emits ``---`` and then its integration's identity
# key (``IDENTITY_KEYS_BY_SOURCE``: ``arxiv_id``/``pmid``/``openalex_id``/
# ``zotero_key``), so once the ``---`` is gone the file opens with, e.g., ``arxiv_id:``
# at column 0 — a line no conversion header (``<!--``) or prose note (``#``) begins
# with. That is the signal, reused from the writers' own map so it cannot drift from
# what they emit.
#
# Reused, not restated: this regex is built from ``IDENTITY_KEYS_BY_SOURCE`` at import,
# so a fifth integration's key is covered here the day it is added there.
_OPENING_IDENTITY_KEY_RE = re.compile(
    r"^(" + "|".join(re.escape(k) for k in sorted(IDENTITY_KEYS_BY_SOURCE.values())) + r"):"
)

# How far to read while looking for the first non-empty line. The identity key is
# line 1 of a rendered source, so the only way it sits past this is a file that opens
# with kilobytes of blank lines — not a real source. Bounded so a pathological file
# cannot pull its whole body through the codec here (the reader made the same trade,
# front_matter_scan).
_IDENTITY_SCAN_CHARS = 4096


def _opening_identity_key(path: Path) -> str | None:
    """The importer identity key a file *opens* with, or None.

    Answered only for a file that already has no opening fence, to tell a source whose
    ``---`` a human deleted from the ordinary no-front-matter file it otherwise looks
    exactly like. Reads the **first non-empty line** — not the first line — because
    removing only the three dashes can leave the blank line that followed them behind
    (measured: ``\\n`` after ``---`` survives a fence deletion), and a first-line test
    would then read the blank and miss the key.

    An observation, not a verdict (following #422/#430): a hand-written note that
    happens to open with ``pmid:`` answers the same way. That is a false positive, but
    the caller only *warns* — the exit code does not move — so the cost is a line of
    output against catching the real deletion. The converse is a gap, not a lie: a
    source whose opening fence was removed in a way that does not leave an identity key
    first is not caught here. No writer emits such a shape today (every one puts its
    identity key first), so the gap is empty in practice and named rather than hidden.
    """
    try:
        with path.open("r", encoding="utf-8") as fh:
            head = fh.read(_IDENTITY_SCAN_CHARS)
    except (OSError, UnicodeDecodeError):
        return None
    for line in head.splitlines():
        if not line.strip():
            continue
        match = _OPENING_IDENTITY_KEY_RE.match(line)
        return match.group(1) if match else None
    return None


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def slugify_heading(heading: str) -> str:
    """GitHub-style anchor for a markdown heading: lowercase, drop punctuation
    (keep Unicode word chars, spaces, hyphens), then spaces -> hyphens. Unicode
    letters are kept so non-ASCII headings (e.g. Korean) still anchor.

    The previous slug only did spaces -> hyphens, so a heading like '## Plan (v2)'
    yielded 'plan-(v2)' and a legitimate '#plan-v2' citation was flagged absent.
    """
    text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", text).strip("-")


def heading_slugs(text: str) -> set[str]:
    """Every anchor a markdown body exposes.

    Headings that slugify identically are GitHub duplicate-suffixed (foo, foo-1,
    foo-2, ...). The legacy naive slug (spaces -> hyphens only) is also included
    so refs authored against the pre-fix convention keep validating.
    """
    seen: dict[str, int] = {}
    slugs: set[str] = set()
    for line in text.splitlines():
        if not line.startswith("#"):
            continue
        title = line.lstrip("#").strip()
        base = slugify_heading(title)
        n = seen.get(base, 0)
        seen[base] = n + 1
        slugs.add(base if n == 0 else f"{base}-{n}")
        slugs.add(re.sub(r"\s+", "-", title.lower()))  # legacy naive slug
    return slugs


def validate_source_ref(root: Path, source_ref: str) -> str | None:
    filename, _, section = source_ref.partition("#")
    path = root / filename
    if not path.is_file():
        return f"source file does not exist: {source_ref}"
    if section:
        if section.lower() not in heading_slugs(read(path)):
            return f"source section does not exist: {source_ref}"
    return None


def validate_confidence(value: str) -> str | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return f"confidence must be a number between 0.00 and 1.00: {value!r}"
    if not 0.0 <= score <= 1.0:
        return f"confidence must be between 0.00 and 1.00: {value!r}"
    return None


def validate_questions(text: str) -> list[str]:
    errors: list[str] = []
    seen: set[str] = set()
    count = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or not re.match(r"^(?:[-*]|\d+\.)", stripped):
            continue
        item = re.sub(r"^[-*]\s+", "", stripped)
        item = re.sub(r"^\d+\.\s+", "", item)
        if re.match(r"^\[[ xX]\]\s+", item):
            errors.append(f"policy/questions.md line {lineno} use '- [q1] 질문', not an Obsidian task checkbox")
            continue
        match = re.match(r"^\[([A-Za-z0-9_-]+)\]\s*(.+)$", item)
        if not match:
            errors.append(f"policy/questions.md line {lineno} should look like '- [q1] 질문'")
            continue
        question_id, question = match.groups()
        if question_id in seen:
            errors.append(f"policy/questions.md line {lineno} duplicate id: {question_id}")
        seen.add(question_id)
        if not question.strip():
            errors.append(f"policy/questions.md line {lineno} has no question text")
        count += 1
    if count == 0:
        errors.append("policy/questions.md has no question list items")
    return errors


def validate_logic_policy(root: Path) -> list[str]:
    script = Path(__file__).parent / "generate_logic_policy.py"
    if not script.is_file():
        return ["missing generate_logic_policy.py"]
    env = os.environ.copy()
    env["FACTLOG_ROOT"] = str(root)
    completed = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode == 0:
        return []
    detail = (completed.stderr or completed.stdout).strip()
    return [f"policy/logic-policy.dl does not match policy/logic-policy.md: {detail}"]


def validate(root: Path) -> list[str]:
    errors: list[str] = []
    for dirname in ["sources", "pages", "facts", "decisions", "policy"]:
        if not (root / dirname).is_dir():
            errors.append(f"missing directory: {dirname}/")

    policy = root / "policy" / "prompts" / "text_to_fact.md"
    if not policy.is_file() or not read(policy).strip():
        errors.append("missing or empty policy/prompts/text_to_fact.md")

    questions = root / "policy" / "questions.md"
    if not questions.is_file() or not read(questions).strip():
        errors.append("missing or empty policy/questions.md")
    else:
        errors.extend(validate_questions(read(questions)))

    datalog_prompt = root / "policy" / "prompts" / "text_to_datalog.md"
    if not datalog_prompt.is_file() or not read(datalog_prompt).strip():
        errors.append("missing or empty policy/prompts/text_to_datalog.md")
    else:
        prompt_text = read(datalog_prompt)
        for placeholder in ["{{SCHEMA_CONTEXT}}", "{{QUESTION}}"]:
            if prompt_text.count(placeholder) != 1:
                errors.append(f"policy/prompts/text_to_datalog.md must contain {placeholder} exactly once")

    repair_prompt = root / "policy" / "prompts" / "self_correct.md"
    if not repair_prompt.is_file() or not read(repair_prompt).strip():
        errors.append("missing or empty policy/prompts/self_correct.md")
    else:
        prompt_text = read(repair_prompt)
        for placeholder in ["{{SCHEMA_CONTEXT}}", "{{LOGIC_REPORT}}", "{{DRAFT_QUERY}}"]:
            if prompt_text.count(placeholder) != 1:
                errors.append(f"policy/prompts/self_correct.md must contain {placeholder} exactly once")
        allowed = {"{{SCHEMA_CONTEXT}}", "{{LOGIC_REPORT}}", "{{DRAFT_QUERY}}"}
        unknown = sorted(set(re.findall(r"{{[^}]+}}", prompt_text)) - allowed)
        if unknown:
            errors.append(f"policy/prompts/self_correct.md contains unknown placeholder(s): {', '.join(unknown)}")

    policy_source = root / "policy" / "logic-policy.md"
    if not policy_source.is_file() or not read(policy_source).strip():
        errors.append("missing or empty policy/logic-policy.md")

    policy_prompt = root / "policy" / "prompts" / "natural_language_to_policy.md"
    if not policy_prompt.is_file() or not read(policy_prompt).strip():
        errors.append("missing or empty policy/prompts/natural_language_to_policy.md")
    elif read(policy_prompt).count("{{POLICY_TEXT}}") != 1:
        errors.append("policy/prompts/natural_language_to_policy.md must contain {{POLICY_TEXT}} exactly once")

    logic_policy = root / "policy" / "logic-policy.dl"
    if policy_source.is_file() and policy_prompt.is_file():
        # Whether an absent .dl is a fault depends on whether logic-policy.md defines
        # rules, and generate_logic_policy --check is the one place that decides (#491):
        # absent + no rules is a freshly `init`ed KB and passes, absent + rules is #190's
        # loud path and still fails. Validating .dl EXISTENCE here first made every fresh
        # KB fail validation before the compiler was ever consulted. A 0-byte .dl is not
        # special-cased either — --check reports it as stale, which names the fix.
        errors.extend(validate_logic_policy(root))
    elif not logic_policy.is_file() or not read(logic_policy).strip():
        # No .md (or no prompt) to judge the .dl against, so --check cannot run and its
        # own "missing or empty policy/logic-policy.md" error is already queued above.
        errors.append("missing or empty policy/logic-policy.dl")

    facts = root / "facts" / "candidates.csv"
    if not facts.is_file():
        errors.append("missing facts/candidates.csv")
        rows = []
    else:
        with facts.open(newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
        if reader.fieldnames != FACT_HEADER:
            errors.append(f"facts/candidates.csv header must be {','.join(FACT_HEADER)}")
        for idx, row in enumerate(rows, start=2):
            if row.get("status") not in VALID_STATUSES:
                errors.append(f"facts/candidates.csv line {idx} invalid status: {row.get('status')!r}")
            confidence_error = validate_confidence(row.get("confidence", ""))
            if confidence_error:
                errors.append(f"facts/candidates.csv line {idx} {confidence_error}")
            source = row.get("source", "")
            if not (source.startswith("sources/") or source.startswith("runs/sources/")):
                errors.append(
                    f"facts/candidates.csv line {idx} source must start with sources/ or runs/sources/"
                )
            else:
                source_error = validate_source_ref(root, source)
                if source_error:
                    errors.append(f"facts/candidates.csv line {idx} {source_error}")

    decisions = root / "decisions" / "open-questions.md"
    if not decisions.is_file():
        errors.append("missing decisions/open-questions.md")
        decision_text = ""
    else:
        decision_text = read(decisions)
        # Heading-based, off the same list the producer and `init` read (#495). The
        # old test was `keyword not in decision_text` over the whole document, which
        # any bullet mentioning the word answered — a file could lose the section
        # itself and still pass.
        for section in missing_review_sections(decision_text):
            errors.append(f"decisions/open-questions.md should keep a {section!r} review section")
        # From review_sections, so "is anything filed here" means the same thing to
        # this check and to the writer that files it. Counting every `- ` line let a
        # bullet written inside a code fence — a format example — stand in for the
        # review queue: measured, the producer skipped the one real row as a duplicate
        # of that example and this check called the result complete.
        decision_bullets = bullets(decision_text)
        if any(row.get("status") == "needs_review" for row in rows) and not decision_bullets:
            errors.append("needs_review facts exist but decisions/open-questions.md has no review bullets")

    stale_pages = []
    pages = sorted((root / "pages").glob("*.md"))
    if facts.is_file():
        with facts.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        page_text = "\n".join(read(page) for page in pages)
        referenced_subjects = {
            value
            for row in rows
            for value in [row.get("subject", ""), row.get("object", "")]
            if value and value in page_text
        }
        if rows and not pages:
            errors.append("facts exist but pages/ has no concept pages")
        if rows and not referenced_subjects:
            errors.append("facts exist but pages/ does not appear to organize fact subjects or objects")

    # What counts as "already recorded", read the way the writer writes it. This was a
    # substring test over the whole document: neither line-based nor fence-aware, so a
    # `- stale_source: …` line written as an example inside a code fence was
    # indistinguishable from a real record and silenced the error for a KB that had
    # recorded nothing. Same shape as the review-bullet miscount above, same reader.
    recorded_bullets = bullets(decision_text)
    for page in pages:
        text = read(page)
        # md/txt/csv: pages may cite text sources or pdftotext/textutil .txt
        # conversions, not only .md — keep in sync with merge_candidates.existing_source_refs.
        for source_ref in re.findall(r"(?:runs/)?sources/[^\s`)>,]+?\.(?:md|txt|csv)(?:#[^\s`)>,]+)?", text):
            source_error = validate_source_ref(root, source_ref)
            stale_record = f"stale_source: {page.relative_to(root).as_posix()} references removed source {source_ref}"
            if source_error and not any(stale_record in line for line in recorded_bullets):
                stale_pages.append(f"{page.relative_to(root)} {source_error}")
    errors.extend(stale_pages)
    return errors


def review_section_warnings(root: Path) -> list[tuple[str, str]]:
    """What is wrong with open-questions.md that the errors do not say, as ``(tag, msg)``.

    Two shapes, both reading hazards the structural checks are blind to.

    ``unclosed_fence`` is the diagnosis for an otherwise baffling error. A file that
    opens a code fence and never closes it has every line below read as code, so the
    errors above can say a review section is missing while the operator is looking
    straight at it. Nothing writes to such a file either — see merge_candidates.

    Whether the exit code moves depends on *where* the fence opens: below the four
    headings it hides nothing any check requires, and the run passes with only this
    warning to show for it. So the message says what the fence does, not what the
    errors say — it used to claim it was "why sections you can see are reported
    missing", which is false in exactly that case, the common one where someone
    pasted a fragment onto the end of the file.

    ``split_review_section`` is not an error at all: a split file is structurally
    complete and every check above passes on it. The earlier section reads
    "- 현재 없음." while the queue accumulates under the later one, so the operator who
    skims the top concludes the review queue is empty (#495). Warning rather than
    repair because the fix moves bullets a human wrote and filed, which is theirs to
    do; warning rather than silence because nothing said it at all before.
    """
    decisions = root / "decisions" / "open-questions.md"
    if not decisions.is_file():
        return []
    warnings: list[tuple[str, str]] = []
    fence_line = unclosed_fence_line(read(decisions))
    if fence_line is not None:
        warnings.append((
            "unclosed_fence",
            f"decisions/open-questions.md opens a code fence on line {fence_line} and "
            f"never closes it — every line below reads as code, so headings there are "
            f"not sections and bullets there are not filed, and nothing is written to "
            f"this file while that holds. Close the fence.",
        ))
    for keyword, headings in split_review_sections(read(decisions)):
        warnings.append((
            "split_review_section",
            f"decisions/open-questions.md has {len(headings)} {keyword!r} sections "
            f"({', '.join(repr(h.title) for h in headings)}); new bullets go to the "
            f"first, so the others keep whatever they already hold — merge them by hand",
        ))
    return warnings


def front_matter_warnings(root: Path) -> list[tuple[str, str]]:
    """Sources whose front matter a human damaged, as ``(tag, message)`` pairs.

    Two damage shapes, one per grep tag, both fail-closed to the same silent cost.
    The reader treats either as carrying no front matter at all, and that choice is
    right — trusting a block it cannot delimit let a user's own note hand its body
    lines to the writers' caches as a paper's identity, which fails silently (#409).
    But the cost lands the other way: a tool-written source whose fence a human
    deleted stops being recognised as imported, and the operator finds out when a
    ``…-2.md`` appears next to it.

    * ``no_closing_fence`` — the block opens with ``---`` and no closing fence is
      found (#422). Reported for both reasons that shape produces: an unclosed block
      and one whose fence sits past the search cap.
    * ``no_opening_fence`` — the block's *opening* ``---`` is gone but its first line
      is still an importer identity key, the shape a source takes when a human deletes
      the opening fence (#445). Distinguished from the ordinary no-front-matter file
      by :func:`_opening_identity_key`, because a bare missing opening fence is the
      normal shape of an ingest conversion and a hand-written note.

    Nothing in the KB is invalid meanwhile — the facts, the refs and the schema all
    still hold — so neither can fail the run without breaking every KB that has one
    such file. They are reported so the cost is visible *before* the duplicate, which
    is the whole of what the fail-closed trade was missing.

    The file set is ``common.source_files``, the single enumeration point every
    sources/ walker shares (#67) — both source roots, hidden paths excluded. A
    private pair of globs here would have reported on ``sources/.obsidian/…`` and
    other files that ``factlog sources``, ``sync`` and ``export`` all agree are not
    sources, which is exactly the disagreement that enumerator exists to prevent.
    Everything it lists is checked, not only what ``facts/candidates.csv`` cites:
    de-duplication walks the tree, so a source no fact cites is exactly as able to
    be re-imported into a duplicate.

    Conversions written as ``.txt``/``.csv`` are skipped, matching ``export``'s own
    suffix test — YAML front matter is a markdown convention and none of the
    writers put a block in one, so scanning them would report on files that were
    never meant to carry one.
    """
    warnings: list[tuple[str, str]] = []
    for path in source_files(root):
        if path.suffix.lower() != ".md":
            continue
        reason = front_matter_absence(path)
        rel = path.relative_to(root).as_posix()
        if reason in WARNED_FRONT_MATTER_ABSENCES:
            warnings.append(
                ("no_closing_fence", f"{rel}: {reason} — {FRONT_MATTER_CONSEQUENCE}")
            )
        elif reason == FRONT_MATTER_NO_OPENING_FENCE:
            key = _opening_identity_key(path)
            if key is not None:
                warnings.append((
                    "no_opening_fence",
                    f"{rel}: {reason}, but its first line is the importer identity "
                    f"key {key!r}, the shape of a source whose opening --- was "
                    f"deleted — {FRONT_MATTER_CONSEQUENCE}",
                ))
    return warnings


def main() -> int:
    # Windows console defaults to the legacy code page (cp949); force UTF-8 so
    # Korean output isn't mangled. No-op elsewhere. Files are always UTF-8.
    if sys.platform == "win32":
        for _stream in (sys.stdout, sys.stderr):
            try:
                _stream.reconfigure(encoding="utf-8")
            except (AttributeError, ValueError, OSError):
                pass
    parser = argparse.ArgumentParser(description="Validate factlog KB outputs.")
    parser.add_argument("root", nargs="?", default=".")
    args = parser.parse_args()
    root = Path(args.root).expanduser().resolve()
    errors = validate(root)
    # Printed on both outcomes and ahead of the verdict, so a failing run does not
    # swallow them and a passing one does not bury them under its own last line.
    # They never move the exit code: see front_matter_warnings for why.
    for tag, warning in front_matter_warnings(root) + review_section_warnings(root):
        # The tag is what a script greps for, and each is chosen not to assert more
        # than the reader knows. ``no_closing_fence`` holds for both closing-fence
        # reasons (unclosed and search-stopped), and ``no_opening_fence`` names the
        # deleted-opening-fence shape without calling it a verdict on the file's owner.
        # ``split_review_section`` names a shape, not a mistake: two headings for one
        # category is a state a human can have arrived at deliberately.
        print(f"warning: {tag}: {warning}")
    if errors:
        print("Fact sync validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Fact sync validation passed: {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
