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

# An administrator withdrawal usually opens with this lead-in, sometimes inside
# the bracket. Consumed before the marker is matched so both agents share one
# regex. It is not sufficient on its own: admin notes also announce text overlap
# and duplicate submissions.
_ADMIN_NOTE_RE = re.compile(r"^\[?\s*arxiv admin note:\s*", re.IGNORECASE)

# Some admin withdrawals carry no lead-in and name the agent in the sentence
# itself ("This submissions has been withdrawn by arXiv administrators").
_BY_ARXIV_RE = re.compile(r"\bby\s+arxiv\b", re.IGNORECASE)

_NOUN = r"(?:paper|submission|manuscript|article|work|version|preprint|draft)s?"
_DETERMINER = r"(?:this|that|the|these|our)\s+(?:following\s+)?"
# Withdrawal notices use every tense and voice: "has been withdrawn", "is
# withdrawn", "was withdrawn", "is hereby withdrawn". Keying on the perfect
# aspect alone missed 38% of live withdrawn papers.
_ANY_VERB = r"(?:(?:has|have)\s+been|is\s+hereby|is|are|was|were)\s+withdrawn"
# Without a determiner the phrase must be singular. "Paper is withdrawn" is a
# notice; "Papers are withdrawn from journals when ..." is a sentence about
# withdrawal, and a false positive there marks a live paper as dead.
_SINGULAR_NOUN = r"(?:paper|submission|manuscript|article|work|version|preprint|draft)"
_SINGULAR_VERB = r"(?:has\s+been|is\s+hereby|is|was)\s+withdrawn"

# Anchored at the first meaningful character of the summary. Each `[^.]{0,80}?`
# spans qualifiers like "from consideration for publication" without crossing a
# sentence boundary, which is what keeps a paper *about* withdrawal from
# matching ("We consider the withdrawal of a ball from a fluid reservoir").
_WITHDRAWN_RE = re.compile(
    r"^(?:"
    # "Withdrawn." / "Withdrawn by authors" / "Withdrawn for revision"
    r"withdrawn\b"
    # "This paper has been withdrawn", "The paper is withdrawn",
    # "This submissions has been withdrawn"
    rf"|{_DETERMINER}{_NOUN}\b[^.]{{0,80}}?\b{_ANY_VERB}\b"
    # "Paper is withdrawn due to further studies."
    rf"|{_SINGULAR_NOUN}\b[^.]{{0,80}}?\b{_SINGULAR_VERB}\b"
    # Verbless participle: "Paper withdrawn by the author", "This paper withdrawn".
    rf"|(?:{_DETERMINER})?{_SINGULAR_NOUN}\s+withdrawn\b"
    r")",
    re.IGNORECASE,
)

# Withdrawal notices are often typeset as a bulleted or dashed aside:
# "- Paper withdrawn by the author - CMOS Monolithic Active Pixel Sensors ..."
_LEADING_PUNCT_RE = re.compile(r"^[\s\[\-–—*•]+")


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

    admin_note = _ADMIN_NOTE_RE.match(text)
    if admin_note is not None:
        remainder = text[admin_note.end():].lstrip()
        # The lead-in alone is not a withdrawal: admin notes also announce
        # text overlap, duplicate submissions, and other housekeeping.
        return WITHDRAWN_BY_ADMIN if _WITHDRAWN_RE.match(remainder) else None

    # Some notices are bracketed or bulleted: "[This paper has been withdrawn...]",
    # "- Paper withdrawn by the author - ...".
    if _WITHDRAWN_RE.match(_LEADING_PUNCT_RE.sub("", text)) is None:
        return None
    # "This submission has been withdrawn by arXiv administrators" carries no
    # lead-in but is still not the author's action. Look only within the notice
    # sentence, so a later mention of arXiv in the abstract cannot reassign it.
    sentence = text.split(".", 1)[0]
    return WITHDRAWN_BY_ADMIN if _BY_ARXIV_RE.search(sentence) else WITHDRAWN_BY_AUTHOR


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
