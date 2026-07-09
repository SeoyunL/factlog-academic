#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn one arXiv Atom ``<entry>`` into a :class:`ParsedArxivWork`.

**Withdrawal detection reads ``<summary>``, not ``<arxiv:comment>``.** The spec
(§6.3) assumed the comment announces a withdrawal. Measured on four withdrawn
papers (#57), the marker is in the summary 4/4 times and in the comment only
1/4; one paper carries no comment element at all, and two carry a comment that
explains the reason without ever using the word ("there is some discrepancy
between some contributors with respect to the order of the authors"). A
comment-keyed detector misses three of four.

**The marker must be anchored.** A substring search for "withdrawn" anywhere in
the summary false-positives on papers that merely *discuss* withdrawal. Real
withdrawal notices open the abstract, optionally wrapped in ``[...]`` or led by
``arXiv admin note:``.

**Withdrawal has an agent, and it is not always the author.** §6.3 calls
withdrawal "the author's own action". arXiv administrators also withdraw papers,
for authorship disputes and for inflammatory content. Reporting a bare boolean
would force downstream text to claim the author withdrew a paper the
administrators pulled. :attr:`ParsedArxivWork.withdrawn_by` records which.

Like ``openalex_is_retracted``, this flag is a *source-scoped signal* derived by
parsing, and it feeds the P1 human gate. It is never promoted into an accepted
claim, and withdrawal is not retraction: arXiv has no peer-reviewed retraction
process (§6.3).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime

from factlog.integrations.arxiv.id_normalizer import ArxivId, parse_entry_id

__all__ = [
    "ParsedArxivWork",
    "WITHDRAWN_BY_ADMIN",
    "WITHDRAWN_BY_AUTHOR",
    "detect_withdrawal",
    "parse_entry",
]

WITHDRAWN_BY_AUTHOR = "author"
WITHDRAWN_BY_ADMIN = "admin"

# An administrator withdrawal opens with this lead-in, sometimes inside the
# bracket. Consumed before the marker is matched so both agents share one regex.
_ADMIN_NOTE_RE = re.compile(r"^\[?\s*arxiv admin note:\s*", re.IGNORECASE)

# Anchored at the first meaningful character of the summary. The `[^.]{0,80}?`
# spans qualifiers like "from consideration for publication" without crossing a
# sentence boundary, which is what keeps a paper *about* withdrawal from matching.
_WITHDRAWN_RE = re.compile(
    r"^(?:this|the following)\s+"
    r"(?:paper|submission|manuscript|article|work|version|preprint)\b"
    r"[^.]{0,80}?\bhas been withdrawn\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedArxivWork:
    """One arXiv work, as factlog needs it.

    ``categories`` carries the primary category at index 0, then the rest in the
    order arXiv lists them. Unlike OpenAlex concepts these need no score filter:
    the vocabulary is controlled, moderator-curated, and typically 1-3 per paper
    (spec §4.3).
    """

    arxiv_id: str
    version: int
    title: str
    authors: tuple[str, ...]
    abstract: str
    primary_category: str
    categories: tuple[str, ...]
    submitted: date | None
    last_updated: date | None
    doi: str | None = None
    journal_ref: str | None = None
    comment: str | None = None
    withdrawn_by: str | None = None
    abs_url: str = ""
    pdf_url: str = ""

    @property
    def withdrawn(self) -> bool:
        return self.withdrawn_by is not None

    @property
    def year(self) -> int | None:
        return self.submitted.year if self.submitted else None

    @property
    def versioned_id(self) -> str:
        return f"{self.arxiv_id}v{self.version}"


def detect_withdrawal(summary: str) -> str | None:
    """Return who withdrew the paper, or None.

    ``"admin"`` when arXiv's administrators pulled it, ``"author"`` when the
    authors did, ``None`` when the abstract does not open with a withdrawal
    notice. See the module docstring for why this reads the summary and why the
    agent is reported rather than assumed.
    """
    if not isinstance(summary, str):
        return None
    # arXiv wraps abstracts across lines; the marker spans them.
    text = " ".join(summary.split()).lstrip()

    admin = _ADMIN_NOTE_RE.match(text)
    if admin is not None:
        remainder = text[admin.end():].lstrip()
        # The lead-in alone is not a withdrawal: admin notes also announce
        # text overlap, duplicate submissions, and other housekeeping.
        return WITHDRAWN_BY_ADMIN if _WITHDRAWN_RE.match(remainder) else None

    # A leading '[' wraps some author notices: "[This paper has been withdrawn...]"
    return WITHDRAWN_BY_AUTHOR if _WITHDRAWN_RE.match(text.lstrip("[").lstrip()) else None


def _clean(value: object) -> str | None:
    """Collapse arXiv's line-wrapped text fields; absent/blank becomes None."""
    if not isinstance(value, str):
        return None
    collapsed = " ".join(value.split())
    return collapsed or None


def _parse_date(value: object) -> date | None:
    """``2023-11-15T18:54:01Z`` -> date(2023, 11, 15)."""
    text = value.strip() if isinstance(value, str) else ""
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _authors(entry) -> tuple[str, ...]:
    names = []
    for author in entry.get("authors") or ():
        name = _clean(author.get("name") if isinstance(author, dict) else None)
        if name:
            names.append(name)
    return tuple(names)


def _categories(entry, primary: str) -> tuple[str, ...]:
    """Primary category first, then the rest in arXiv's order, deduplicated."""
    ordered = [primary] if primary else []
    for tag in entry.get("tags") or ():
        term = _clean(tag.get("term") if isinstance(tag, dict) else None)
        if term and term not in ordered:
            ordered.append(term)
    return tuple(ordered)


def _primary_category(entry) -> str:
    raw = entry.get("arxiv_primary_category")
    if isinstance(raw, dict):
        return _clean(raw.get("term")) or ""
    return _clean(raw) or ""


def _pdf_url(entry, identifier: ArxivId) -> str:
    for link in entry.get("links") or ():
        if isinstance(link, dict) and link.get("title") == "pdf" and link.get("href"):
            return str(link["href"])
    return f"https://arxiv.org/pdf/{identifier}"


def parse_entry(entry) -> ParsedArxivWork:
    """Build a :class:`ParsedArxivWork` from one feedparser entry.

    ``entry`` is a mapping in feedparser's shape: namespaced elements arrive
    flattened (``arxiv:doi`` -> ``arxiv_doi``), and ``<id>`` is the abs URL from
    which the version is read — the Atom response carries no version field.
    """
    identifier = parse_entry_id(entry.get("id") or "")
    summary = entry.get("summary") or ""
    primary = _primary_category(entry)

    return ParsedArxivWork(
        arxiv_id=identifier.base,
        version=identifier.version,
        title=_clean(entry.get("title")) or "",
        authors=_authors(entry),
        abstract=_clean(summary) or "",
        primary_category=primary,
        categories=_categories(entry, primary),
        submitted=_parse_date(entry.get("published")),
        last_updated=_parse_date(entry.get("updated")),
        doi=_clean(entry.get("arxiv_doi")),
        journal_ref=_clean(entry.get("arxiv_journal_ref")),
        # Stored verbatim, never parsed for structured meaning (§6.1, §10).
        comment=_clean(entry.get("arxiv_comment")),
        withdrawn_by=detect_withdrawal(summary),
        abs_url=identifier.abs_url,
        pdf_url=_pdf_url(entry, identifier),
    )
