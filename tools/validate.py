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

from common import FACT_HEADER, KNOWN_STATUSES

# An unregistered status is an *error* here, not a warning — so this set drifting
# from the vocabulary is worse than the #208 warning bug. Derive, never restate.
VALID_STATUSES = KNOWN_STATUSES


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
    if not logic_policy.is_file() or not read(logic_policy).strip():
        errors.append("missing or empty policy/logic-policy.dl")
    elif policy_source.is_file() and policy_prompt.is_file():
        errors.extend(validate_logic_policy(root))

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
        for section in ["중복", "모호", "출처", "충돌"]:
            if section not in decision_text:
                errors.append(f"decisions/open-questions.md should keep a {section!r} review section")
        decision_bullets = [line for line in decision_text.splitlines() if line.lstrip().startswith("- ")]
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

    for page in pages:
        text = read(page)
        # md/txt/csv: pages may cite text sources or pdftotext/textutil .txt
        # conversions, not only .md — keep in sync with merge_candidates.existing_source_refs.
        for source_ref in re.findall(r"(?:runs/)?sources/[^\s`)>,]+?\.(?:md|txt|csv)(?:#[^\s`)>,]+)?", text):
            source_error = validate_source_ref(root, source_ref)
            stale_record = f"stale_source: {page.relative_to(root).as_posix()} references removed source {source_ref}"
            if source_error and stale_record not in decision_text:
                stale_pages.append(f"{page.relative_to(root)} {source_error}")
    errors.extend(stale_pages)
    return errors


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
    if errors:
        print("Fact sync validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print(f"Fact sync validation passed: {root}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
