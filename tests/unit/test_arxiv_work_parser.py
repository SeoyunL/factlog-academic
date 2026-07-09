# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the arXiv Atom entry parser (#57 §6.3, §4.1).

The withdrawal fixtures are the *verbatim* summary and comment text of four real
withdrawn papers, recorded during the #57 spike. They are the evidence for the
spec correction: the marker lives in `<summary>` (4/4), not in `<arxiv:comment>`
(1/4), and one paper carries no comment element at all. A comment-keyed detector
passes on none of these.

Entries are plain dicts in feedparser's shape (namespaced elements flattened:
`arxiv:doi` -> `arxiv_doi`), so these tests need neither the network nor the
optional dependency.
"""
from __future__ import annotations

from datetime import date

import pytest

from factlog.integrations.arxiv.work_parser import (
    WITHDRAWN_BY_ADMIN,
    WITHDRAWN_BY_AUTHOR,
    detect_withdrawal,
    parse_entry,
)

# A trimmed real entry: 1906.01157, one of the few cs.CL papers carrying a DOI.
ENTRY = {
    "id": "http://arxiv.org/abs/1906.01157v2",
    "title": "A Review of Automated Speech and Language Features for Assessment",
    "summary": "Speech and language have been used\n  to assess cognition.",
    "published": "2019-06-04T02:17:18Z",
    "updated": "2019-11-05T04:23:20Z",
    "authors": [{"name": "Rohit Voleti"}, {"name": "Julie M. Liss"}],
    "arxiv_primary_category": {"term": "cs.CL"},
    "tags": [{"term": "cs.CL"}, {"term": "cs.SD"}, {"term": "eess.AS"}],
    "arxiv_doi": "10.1109/JSTSP.2019.2952087",
    "arxiv_comment": "\\c{opyright} 2019 IEEE. Personal use of this material is permitted.",
    "links": [{"title": "pdf", "href": "http://arxiv.org/pdf/1906.01157v2"}],
}


def test_parse_entry_reads_every_field_the_writer_needs():
    work = parse_entry(ENTRY)
    assert work.arxiv_id == "1906.01157"
    assert work.version == 2
    assert work.versioned_id == "1906.01157v2"
    assert work.authors == ("Rohit Voleti", "Julie M. Liss")
    assert work.submitted == date(2019, 6, 4)
    assert work.last_updated == date(2019, 11, 5)
    assert work.year == 2019
    assert work.doi == "10.1109/JSTSP.2019.2952087"
    assert work.journal_ref is None
    assert work.pdf_url == "http://arxiv.org/pdf/1906.01157v2"
    assert work.abs_url == "https://arxiv.org/abs/1906.01157v2"
    assert not work.withdrawn


def test_line_wrapped_text_is_collapsed():
    # arXiv wraps <summary> and <title> across lines with leading indentation.
    assert parse_entry(ENTRY).abstract == "Speech and language have been used to assess cognition."


def test_primary_category_leads_the_category_tuple():
    # §4.3: primary first, then the rest, deduplicated. No score filter is needed
    # — unlike OpenAlex concepts the vocabulary is curated and unscored.
    work = parse_entry({**ENTRY, "arxiv_primary_category": {"term": "eess.AS"}})
    assert work.primary_category == "eess.AS"
    assert work.categories == ("eess.AS", "cs.CL", "cs.SD")


def test_missing_optional_fields_become_none_not_empty_string():
    # 96% of recent cs.CL papers carry no DOI and no journal_ref (#57).
    work = parse_entry({k: v for k, v in ENTRY.items()
                        if k not in ("arxiv_doi", "arxiv_comment")})
    assert work.doi is None
    assert work.comment is None


def test_pdf_url_is_derived_when_the_link_is_absent():
    work = parse_entry({k: v for k, v in ENTRY.items() if k != "links"})
    assert work.pdf_url == "https://arxiv.org/pdf/1906.01157v2"


def test_comment_is_stored_verbatim_never_interpreted():
    # §6.1, §10: the comment is unstructured author prose. Whitespace is
    # collapsed; nothing else is touched, LaTeX escapes included.
    assert parse_entry(ENTRY).comment == (
        "\\c{opyright} 2019 IEEE. Personal use of this material is permitted."
    )


# -- withdrawal ------------------------------------------------------------
# Verbatim summaries of four real withdrawn papers (#57). Two of them have a
# comment that never says "withdrawn"; one has no comment at all.

@pytest.mark.parametrize(
    ("paper", "summary", "expected"),
    [
        ("1910.09022", "this paper has been withdrawn", WITHDRAWN_BY_AUTHOR),
        (
            "1806.06446",
            "This paper has been withdrawn as we discovered a bug in our tensorflow "
            "implementation that involved accidental mixing of vectors across batches.",
            WITHDRAWN_BY_AUTHOR,
        ),
        (
            "1811.03758",
            "[This paper has been withdrawn by the author due to updated research "
            "available on arXiv (arXiv:1811.01918)] As the modern open-source paradigm",
            WITHDRAWN_BY_AUTHOR,
        ),
        (
            "2212.05167",
            "The following paper has been withdrawn from consideration for publication "
            "because there are mistakes. In particular, Theorem 3.9 does not hold.",
            WITHDRAWN_BY_AUTHOR,
        ),
        (
            "2512.23783",
            "arXiv admin note: This paper has been withdrawn by arXiv due to disputed "
            "and unverifiable authorship and affiliation",
            WITHDRAWN_BY_ADMIN,
        ),
        (
            "1904.09773",
            "arXiv admin note: This submission has been withdrawn by arXiv "
            "administrators due to inflammatory content and unprofessional language",
            WITHDRAWN_BY_ADMIN,
        ),
    ],
)
def test_detect_withdrawal_finds_the_marker_and_names_the_agent(paper, summary, expected):
    assert detect_withdrawal(summary) == expected, paper


def test_admin_withdrawal_is_not_reported_as_the_authors_action():
    # §6.3 calls withdrawal "the author's own action". For an admin withdrawal
    # that is false, and a bare bool would force downstream text to assert it.
    admin = "arXiv admin note: This paper has been withdrawn by arXiv due to bad authorship"
    assert detect_withdrawal(admin) == WITHDRAWN_BY_ADMIN
    assert detect_withdrawal(admin) != WITHDRAWN_BY_AUTHOR


@pytest.mark.parametrize(
    "summary",
    [
        # A paper *about* withdrawal. `abs:"has been withdrawn"` surfaces these,
        # and an unanchored substring search would flag them.
        "Language models deployed in high-stakes settings face conflicting demands. "
        "We study whether a paper has been withdrawn from a benchmark.",
        "We show that when a submission has been withdrawn, reviewers update priors.",
        # An admin note that is not a withdrawal: they also announce text overlap.
        "arXiv admin note: substantial text overlap with arXiv:1234.5678",
        "",
        "   ",
    ],
)
def test_detect_withdrawal_ignores_unanchored_and_non_withdrawal_text(summary):
    assert detect_withdrawal(summary) is None


def test_detect_withdrawal_tolerates_line_wrapping():
    assert detect_withdrawal("This paper\n  has been withdrawn\n  by the authors.") == (
        WITHDRAWN_BY_AUTHOR
    )


def test_detect_withdrawal_on_non_string_is_none():
    assert detect_withdrawal(None) is None


def test_withdrawn_flag_reaches_the_parsed_work():
    work = parse_entry({**ENTRY, "summary": "this paper has been withdrawn"})
    assert work.withdrawn is True
    assert work.withdrawn_by == WITHDRAWN_BY_AUTHOR
    # The comment is unchanged: it is provenance, not the detection signal.
    assert work.comment == ENTRY["arxiv_comment"]


def test_paper_whose_comment_never_says_withdrawn_is_still_detected():
    # 1910.09022: comment explains an authorship dispute; only the summary says it.
    entry = {
        **ENTRY,
        "summary": "this paper has been withdrawn",
        "arxiv_comment": "1. there is some discrepancy between some contributors "
                         "with respect to the order of the authors",
    }
    assert parse_entry(entry).withdrawn is True


def test_paper_with_no_comment_element_is_still_detected():
    # 2212.05167 carries no <arxiv:comment> at all.
    entry = {k: v for k, v in ENTRY.items() if k != "arxiv_comment"}
    entry["summary"] = "The following paper has been withdrawn from consideration."
    work = parse_entry(entry)
    assert work.comment is None
    assert work.withdrawn is True
