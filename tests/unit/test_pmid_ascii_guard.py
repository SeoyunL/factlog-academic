# SPDX-License-Identifier: Apache-2.0
"""Both ``normalize_pmid`` validators reject non-ASCII digit spellings (#427).

``str.isdigit`` is true of every Unicode decimal digit, so the shared guard
``candidate.isdigit()`` admitted full-width, Arabic-Indic and Devanagari ids —
and, being wider than ``Nd``, also category-``No`` characters like ``²`` and
``①``. Two consequences these tests pin: a full-width id also slipped the
leading-zero rule (``lstrip("0")`` strips only ASCII ``0``), and a user-typed
full-width ``--pmid`` spent a live request that NCBI can only answer emptily.

The two functions are deliberately identical in *policy* and different in
*role* — the parity test states the first, and the two path tests the second:
``pubmed.client``'s callers are all request-side, while ``openalex``'s single
caller sits on the write path behind ``_optional``, so rejection there means the
record stores no pmid rather than a respelled one. Neither folds; see the
``api_client.normalize_pmid`` docstring for why the repository folds on the
derived join key (#421) instead of at these gates.
"""
from __future__ import annotations

import pytest

from factlog import cli
from factlog.integrations.openalex.api_client import OpenAlexError
from factlog.integrations.openalex.api_client import normalize_pmid as openalex_pmid
from factlog.integrations.openalex.work_parser import parse_work
from factlog.integrations.pubmed.client import PubMedError
from factlog.integrations.pubmed.client import normalize_pmid as pubmed_pmid
from factlog.text_norm import fold_decimal_digits

# Every one of these satisfies ``str.isdigit()`` and so passed both gates before
# the ASCII guard. The last two are category ``No``: ``isdigit()`` is true but
# ``isdecimal()`` is false, which is why folding could not have covered them.
NON_ASCII_DIGITS = [
    pytest.param("１２３４５６７８", id="fullwidth"),
    pytest.param("٢٢٢٢", id="arabic-indic"),
    pytest.param("२२२२", id="devanagari"),
    pytest.param("۱۲۳۴", id="extended-arabic-indic"),
    pytest.param("１2３4", id="mixed-ascii-and-fullwidth"),
    pytest.param("０１２３", id="fullwidth-leading-zero"),
    pytest.param("²²", id="superscript-No"),
    pytest.param("①", id="circled-No"),
]

NORMALIZERS = [
    pytest.param(openalex_pmid, OpenAlexError, id="openalex"),
    pytest.param(pubmed_pmid, PubMedError, id="pubmed"),
]


@pytest.mark.parametrize("normalize,error", NORMALIZERS)
@pytest.mark.parametrize("raw", NON_ASCII_DIGITS)
def test_non_ascii_digits_are_rejected(normalize, error, raw):
    with pytest.raises(error, match="invalid PMID"):
        normalize(raw)


@pytest.mark.parametrize("normalize,error", NORMALIZERS)
@pytest.mark.parametrize("raw", ["32738937", "1", "16354850"])
def test_ascii_pmids_still_pass(normalize, error, raw):
    assert normalize(raw) == raw


@pytest.mark.parametrize("raw", NON_ASCII_DIGITS)
def test_the_two_normalizers_agree(raw):
    """One id must not mean different things in an OpenAlex and a PubMed command."""
    with pytest.raises(OpenAlexError):
        openalex_pmid(raw)
    with pytest.raises(PubMedError):
        pubmed_pmid(raw)


class TestWhyRejectAndNotFold:
    """The two facts that decided the policy, kept as executable claims."""

    @pytest.mark.parametrize("raw", ["²²", "①"])
    def test_folding_would_not_have_closed_the_hole(self, raw):
        # ``fold_decimal_digits`` is exactly as wide as ``Nd`` by design, so these
        # survive it unchanged and still satisfy ``isdigit()``. A fold-then-digit
        # check would keep admitting them; only the ASCII guard rejects them.
        assert fold_decimal_digits(raw) == raw
        assert raw.isdigit() and not raw.isdecimal()

    def test_fullwidth_zero_also_slipped_the_leading_zero_rule(self):
        # ``lstrip("0")`` strips ASCII ``0`` only, so the pre-existing rejection of
        # zero-padded ids never applied to a full-width spelling of one.
        assert "０１２３".lstrip("0") == "０１２３"


class TestPubMedGuardsTheRequest:
    """All three callers are request-side; nothing may reach the transport."""

    def test_a_full_width_id_never_reaches_the_transport(self):
        from tests.unit.test_pubmed_client import client as pubmed_client

        api, calls, _ = pubmed_client([])
        with pytest.raises(PubMedError, match="invalid PMID"):
            api.efetch(["１２３４５６７８"])
        assert calls == []

    def test_pubmed_import_rejects_before_spending_a_request(self, tmp_path, monkeypatch):
        from tests.unit.test_pubmed_cli import _kb

        kb = _kb(tmp_path)

        # If the id is rejected at validation time the command never builds a
        # client at all, so no network is reachable even in principle. Assert that
        # by failing loudly if one is ever asked for.
        def refuse(config):  # pragma: no cover - only runs if the guard regresses
            raise AssertionError("a client was built for a full-width PMID")

        monkeypatch.setattr(cli, "_make_pubmed_client", refuse)
        args = cli.build_parser().parse_args(
            ["pubmed-import", "--pmid", "１２３４５６７８", "--target", str(kb)]
        )
        assert args.func(args) == 1
        assert list((kb / "sources").glob("*.md")) == []

    def test_acknowledge_retraction_rejects_the_id(self, tmp_path, capsys):
        from tests.unit.test_pubmed_cli import _kb

        kb = _kb(tmp_path)
        args = cli.build_parser().parse_args(
            ["pubmed-acknowledge-retraction", "--id", "１２３４５６７８", "--target", str(kb)]
        )
        assert args.func(args) == 1
        assert "invalid PMID" in capsys.readouterr().err


class TestOpenAlexGuardsTheStoredValue:
    """The single caller is the write path, wrapped in ``_optional``."""

    def test_a_full_width_ids_pmid_is_dropped_not_stored(self):
        work = {
            "id": "https://openalex.org/W123",
            "display_name": "A paper",
            "publication_year": 2020,
            "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/１２３４５６７８"},
        }
        parsed = parse_work(work)
        # Rejected, and degraded to None by ``_optional`` rather than aborting the
        # import: the record survives, minus a pmid it could not trust.
        assert parsed.pmid is None
        assert parsed.title == "A paper"

    def test_an_ascii_ids_pmid_is_still_stored(self):
        work = {
            "id": "https://openalex.org/W123",
            "display_name": "A paper",
            "publication_year": 2020,
            "ids": {"pmid": "https://pubmed.ncbi.nlm.nih.gov/32738937"},
        }
        assert parse_work(work).pmid == "32738937"
