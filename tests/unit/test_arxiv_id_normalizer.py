# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the arXiv id normalizer (#57 §2.1).

Every case here is a shape measured against the live API during the #57 spike.
The ones that matter most are the *silent* failures: `arXiv:1706.03762` and
`math.GT/0309136` are both answered with HTTP 200 and zero entries, so if the
normalizer does not fix them nothing downstream ever learns that the lookup was
wrong. Those are the tests that earn their keep.
"""
from __future__ import annotations

import pytest

from factlog.integrations.arxiv.id_normalizer import (
    ArxivId,
    ArxivIdError,
    normalize_arxiv_id,
    parse_entry_id,
)


@pytest.mark.parametrize(
    ("raw", "base", "version"),
    [
        # -- new-style, the plain cases
        ("2311.09277", "2311.09277", None),
        ("1706.03762v7", "1706.03762", 7),
        # Four-digit sequence (2007-04..2014) and five-digit (2015-01..).
        ("0704.0001", "0704.0001", None),
        ("1512.03385v1", "1512.03385", 1),
        # -- the form arXiv itself prints, which returns 0 entries unnormalized
        ("arXiv:1706.03762", "1706.03762", None),
        ("arxiv:1706.03762v2", "1706.03762", 2),
        ("ARXIV:1706.03762", "1706.03762", None),
        # -- URLs, with and without version, pdf and abs
        ("https://arxiv.org/abs/2311.09277", "2311.09277", None),
        ("https://arxiv.org/abs/2311.09277v2", "2311.09277", 2),
        ("http://arxiv.org/abs/1706.03762v7", "1706.03762", 7),
        ("https://arxiv.org/pdf/2311.09277v1", "2311.09277", 1),
        ("https://arxiv.org/pdf/2311.09277v1.pdf", "2311.09277", 1),
        # -- arXiv's own DataCite DOI form (the API answers this one with 400)
        ("10.48550/arXiv.1706.03762", "1706.03762", None),
        ("10.48550/arxiv.1706.03762v3", "1706.03762", 3),
        # -- an abs URL as the browser leaves it, with the context query string
        ("https://arxiv.org/abs/2311.09277?context=cs.CL", "2311.09277", None),
        ("https://arxiv.org/abs/2311.09277v2?context=cs", "2311.09277", 2),
        ("https://arxiv.org/abs/2311.09277#comments", "2311.09277", None),
        # -- an uppercase version suffix
        ("1706.03762V7", "1706.03762", 7),
        ("arXiv:1706.03762V7", "1706.03762", 7),
        # -- whitespace
        ("  2311.09277  ", "2311.09277", None),
    ],
)
def test_new_style_forms_normalize(raw, base, version):
    assert normalize_arxiv_id(raw) == ArxivId(base, version)


@pytest.mark.parametrize(
    ("raw", "base", "version"),
    [
        # The API accepts `archive/YYMMNNN` and echoes it back without the
        # subject class. `math.GT/0309136` silently returns zero entries.
        ("math/0309136", "math/0309136", None),
        ("math.GT/0309136", "math/0309136", None),
        ("math.GT/0309136v1", "math/0309136", 1),
        # A hyphenated archive with no subject class at all. The spec's
        # `^[a-z-]+\.[A-Z]{2}/[0-9]{7}$` regex rejects this valid id.
        ("hep-th/9901001", "hep-th/9901001", None),
        ("hep-th/9901001v3", "hep-th/9901001", 3),
        # Hyphenated archive *and* hyphenated subject class: `[A-Z]{2}` is wrong
        # in both halves.
        ("cond-mat.stat-mech/0512456", "cond-mat/0512456", None),
        ("physics.flu-dyn/0605001", "physics/0605001", None),
        ("nlin.CD/0402044", "nlin/0402044", None),
        # cs is an archive as well as a modern prefix.
        ("cs.CL/0108005", "cs/0108005", None),
        # URL and case
        ("https://arxiv.org/abs/math/0309136v1", "math/0309136", 1),
        ("HEP-TH/9901001", "hep-th/9901001", None),
    ],
)
def test_old_style_forms_drop_the_subject_class(raw, base, version):
    assert normalize_arxiv_id(raw) == ArxivId(base, version)


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "notanid",
        "1706.037",            # sequence too short
        "1706.037621",         # sequence too long
        "170.03762",           # yymm too short
        "1706.03762v",         # dangling v
        "1706.03762v0",        # arXiv numbers versions from v1
        "1706.03762vx",
        "math/03091360",       # eight digits
        "math/030913",         # six digits
        "math/abcdefg",
        "notanarchive/0309136",
        "hep-tth/9901001",     # a typo'd archive must not reach the API
    ],
)
def test_malformed_ids_raise_rather_than_being_sent(raw):
    with pytest.raises(ArxivIdError):
        normalize_arxiv_id(raw)


def test_prefix_and_old_style_combine():
    assert normalize_arxiv_id("arXiv:math.GT/0309136v2") == ArxivId("math/0309136", 2)


def test_url_marker_is_located_without_lowercasing_the_original():
    # `str.lower()` is not length-preserving for every character, so finding
    # `/abs/` in a lowercased copy and slicing the original by that index can
    # cut in the wrong place. 'İ'.lower() is two characters.
    assert normalize_arxiv_id("https://İ.example/abs/1706.03762v7") == ArxivId("1706.03762", 7)


def test_non_string_input_raises():
    with pytest.raises(ArxivIdError):
        normalize_arxiv_id(None)  # type: ignore[arg-type]


def test_str_round_trips_to_the_query_value():
    assert str(normalize_arxiv_id("arXiv:1706.03762v7")) == "1706.03762v7"
    assert normalize_arxiv_id("1706.03762").query_value == "1706.03762"
    assert normalize_arxiv_id("math.GT/0309136v2").query_value == "math/0309136v2"


def test_abs_url_uses_the_canonical_form():
    assert normalize_arxiv_id("math.GT/0309136v1").abs_url == (
        "https://arxiv.org/abs/math/0309136v1"
    )


def test_parse_entry_id_reads_the_version_from_the_echoed_url():
    # The Atom response carries no version field; it lives only in <id>.
    assert parse_entry_id("http://arxiv.org/abs/1706.03762v7") == ArxivId("1706.03762", 7)
    assert parse_entry_id("http://arxiv.org/abs/math/0309136v1") == ArxivId("math/0309136", 1)


def test_parse_entry_id_rejects_an_unversioned_entry():
    # Every response entry pins a version. One that does not means the parser's
    # assumption about the feed has broken, and silently defaulting would make
    # arxiv-check-versions compare against nothing.
    with pytest.raises(ArxivIdError):
        parse_entry_id("http://arxiv.org/abs/1706.03762")
