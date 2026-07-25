# SPDX-License-Identifier: Apache-2.0
"""Markdown-structure scanning must not regrow outside its two SSOT modules (#513).

:mod:`factlog.md_lines` owns the raw "what is this line" judgements (heading, fence,
section boundary, bullet) and :mod:`factlog.review_sections` owns the review-category
contract. Both docstrings enumerate, in prose, the exact scans that #495/#504/#515
pulled out of their callers — "where does a section start", "which lines are list
items", "is this record already written". Prose does not fail a build. During the
#504 session the same disease ("re-scan open-questions.md's structure inline") came
back three times, because nothing forced the enumeration to be updated together with
the code. This test moves that list out of prose and into test data: reintroduce one
of those idioms as executable code anywhere in ``factlog/`` or ``tools/`` (outside the
two SSOT modules) and this contract fails deterministically — a grep match, not a
model judgement.

**What this contract is and is not.** It is a recurrence tripwire, not a proof. The
grep fingerprints only catch *verbatim reintroduction* of the same idiom. Equivalent
rephrasings slip past on purpose-of-scope grounds: ``'## ' in line`` (substring
instead of ``startswith``), ``startswith('##')`` (no trailing space), or the same
scan spelled over a differently-named variable are all invisible here. That is the
known limit of a grep contract; the response to a newly discovered shape is to add
its fingerprint to ``CONTRACT_PATTERNS``, not to trust this test as a guarantee.

Precedent for the text-read, no-import approach: ``test_reserved_predicate_parity``.
Reading files as text (never importing them) also sidesteps the main-tree import
trap entirely — no ``PYTHONPATH`` and no worktree confusion.
"""
from __future__ import annotations

from pathlib import Path

# Repo root: tests/unit/<this file> -> repo root is two parents up.
_REPO_ROOT = Path(__file__).resolve().parents[2]

# The exact idioms #495/#504/#515 extracted into the two SSOT modules. Each is the
# fingerprint of one recurring inline re-scan of a document's markdown structure.
CONTRACT_PATTERNS = (
    'startswith("## ")',  # section-heading boundary walk (md_lines.section_body_end)
    'startswith("- ")',  # bullet / list-item count (md_lines.bullets)
    "lines.index(",  # "where does this section start" (md_lines.insert_bullet)
    "in decision_text",  # "is this record already written" whole-doc substring test
)

# Files that legitimately contain these idioms because they ARE the single source of
# truth for them. Everything else in factlog/ and tools/ must route through these.
_EXEMPT = frozenset(
    {
        _REPO_ROOT / "factlog" / "md_lines.py",
        _REPO_ROOT / "factlog" / "review_sections.py",
    }
)

# Executable-code violations that are knowingly tolerated, as (relative-path, pattern,
# reason) tuples. It is EMPTY on purpose: at base 1adb908 the narrow four patterns have
# zero live (non-comment) violations outside the SSOT modules. An unresolved defect must
# never be parked here — that would pin a falsehood. Add an entry only for an executable
# occurrence that is deliberately accepted, with a reason a reviewer can check.
ALLOWLIST: tuple[tuple[str, str, str], ...] = ()


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for base in ("factlog", "tools"):
        for path in sorted((_REPO_ROOT / base).rglob("*.py")):
            if path in _EXEMPT:
                continue
            files.append(path)
    return files


def _find_violations() -> list[tuple[str, int, str]]:
    """Return (relative-path, 1-based line number, pattern) for each match in
    executable code. Pure comment lines (``#`` after stripping) are skipped
    structurally, so the two fossil comments that describe removed scans
    (merge_candidates.py, validate.py) fall out without any line-number reference —
    issue-503/512 may reshuffle those files and this contract still holds.
    """
    violations: list[tuple[str, int, str]] = []
    allowed = {(p, pat) for (p, pat, _reason) in ALLOWLIST}
    for path in _scanned_files():
        rel = path.relative_to(_REPO_ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if line.strip().startswith("#"):
                continue  # pure comment line: a fossil, not a live scan
            for pattern in CONTRACT_PATTERNS:
                if pattern in line and (rel, pattern) not in allowed:
                    violations.append((rel, lineno, pattern))
    return violations


def test_markdown_scan_contract_has_no_new_violations() -> None:
    """No markdown-structure scan idiom may reappear as executable code outside the
    two SSOT modules and the (currently empty) allowlist."""
    scanned = _scanned_files()
    assert scanned, "scanned no files — repo layout or exempt set changed"

    violations = _find_violations()
    assert not violations, (
        "markdown-structure scan idiom(s) reintroduced outside "
        "factlog/md_lines.py and factlog/review_sections.py:\n"
        + "\n".join(f"  {rel}:{lineno}: {pat!r}" for rel, lineno, pat in violations)
        + "\nRoute the scan through the SSOT module, or (if deliberately kept) add a "
        "(path, pattern, reason) entry to ALLOWLIST in this file."
    )


def test_exempt_ssot_modules_exist() -> None:
    """The exemptions must point at real files, or a rename silently voids the
    contract for the module it was meant to protect."""
    for path in _EXEMPT:
        assert path.is_file(), f"exempt SSOT module missing: {path}"


def test_allowlist_entries_are_still_live() -> None:
    """Every allowlisted (path, pattern) must still occur as executable code, so a
    fix that removes the occurrence forces the stale allowlist entry out too. Vacuous
    while ALLOWLIST is empty; guards against rot once it is not."""
    for rel, pattern, _reason in ALLOWLIST:
        path = _REPO_ROOT / rel
        assert path.is_file(), f"allowlisted file missing: {rel}"
        text = path.read_text(encoding="utf-8")
        live = any(
            pattern in line and not line.strip().startswith("#")
            for line in text.splitlines()
        )
        assert live, f"allowlist entry no longer occurs as code: {rel} {pattern!r}"
