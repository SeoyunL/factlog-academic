# SPDX-License-Identifier: Apache-2.0
"""CLI tests for `factlog pubmed-search` (#167).

The real PubMed client is replaced via ``_make_pubmed_client`` so the command runs
without the network. A temp KB (with sources/) is the target. The focus is the
silent-zero guard and the --show-query/--dry-run distinction — the issue's
Done-when. Import-on-select is deferred to #166; this lands listing + guards.
"""
from __future__ import annotations

import pytest

from factlog import cli


def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    return tmp_path


def _esearch(count=0, ids=(), query_translation=None, errors=(), warnings=()):
    parts = [f"<Count>{count}</Count>"]
    parts.append("<IdList>" + "".join(f"<Id>{i}</Id>" for i in ids) + "</IdList>")
    if query_translation is not None:
        parts.append(f"<QueryTranslation>{query_translation}</QueryTranslation>")
    if errors:
        parts.append("<ErrorList>" + "".join(
            f"<{t}>{v}</{t}>" for t, v in errors) + "</ErrorList>")
    if warnings:
        parts.append("<WarningList>" + "".join(
            f"<{t}>{v}</{t}>" for t, v in warnings) + "</WarningList>")
    return f"<eSearchResult>{''.join(parts)}</eSearchResult>"


def _article(pmid, title="A paper", retracted=False):
    retr = (
        "<CommentsCorrectionsList><CommentsCorrections RefType='RetractionIn'>"
        "<PMID Version='1'>99999999</PMID></CommentsCorrections></CommentsCorrectionsList>"
        if retracted else ""
    )
    ptype = (
        "<PublicationTypeList><PublicationType UI='D016441'>Retracted Publication"
        "</PublicationType></PublicationTypeList>" if retracted else ""
    )
    return (
        "<PubmedArticle><MedlineCitation>"
        f"<PMID Version='1'>{pmid}</PMID>"
        f"<Article><ArticleTitle>{title}</ArticleTitle>"
        "<AuthorList><Author><LastName>Doe</LastName><ForeName>Jane</ForeName></Author></AuthorList>"
        f"<Journal><Title>Nature</Title></Journal>{ptype}</Article>"
        f"{retr}</MedlineCitation></PubmedArticle>"
    )


def _efetch(*articles):
    return f"<PubmedArticleSet>{''.join(articles)}</PubmedArticleSet>"


class FakePubMedClient:
    """Replays canned esearch/efetch bodies; records the calls it received."""

    def __init__(self, esearch_body=None, efetch_body=None, raise_on=None):
        self._esearch = esearch_body if esearch_body is not None else _esearch(count=0)
        self._efetch = efetch_body if efetch_body is not None else _efetch()
        self._raise_on = raise_on or {}
        self.calls: list = []

    def esearch(self, term, retmax=None, retstart=0):
        self.calls.append(("esearch", term, retmax))
        if "esearch" in self._raise_on:
            raise self._raise_on["esearch"]
        return self._esearch

    def efetch(self, ids):
        self.calls.append(("efetch", tuple(ids)))
        if "efetch" in self._raise_on:
            raise self._raise_on["efetch"]
        return self._efetch


@pytest.fixture
def fake(monkeypatch):
    def install(client):
        monkeypatch.setattr(cli, "_make_pubmed_client", lambda config: client)
        return client
    return install


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


# -- --show-query sends no request (proven by a client that fails if built) --

class TestShowQuery:
    def test_show_query_never_builds_the_client(self, tmp_path, monkeypatch, capsys):
        def boom(config):
            raise AssertionError("--show-query built the client / sent a request")
        monkeypatch.setattr(cli, "_make_pubmed_client", boom)
        rc = run(["pubmed-search", "--query", "crispr cas9", "--mesh", "Neoplasms",
                  "--show-query", "--target", str(_kb(tmp_path))])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no request sent" in out
        assert "crispr cas9 AND Neoplasms[MeSH Terms]" in out

    def test_show_query_porcelain_is_a_query_row(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_make_pubmed_client",
                            lambda config: (_ for _ in ()).throw(AssertionError("built")))
        rc = run(["pubmed-search", "--query", "brca1", "--show-query", "--porcelain",
                  "--target", str(_kb(tmp_path))])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "query\tbrca1"


# -- field-tag rejection before a request -----------------------------------

class TestFieldTagRejection:
    def test_unknown_field_tag_never_calls_the_client(self, tmp_path, monkeypatch, capsys):
        def boom(config):
            raise AssertionError("a bad field tag reached the transport")
        monkeypatch.setattr(cli, "_make_pubmed_client", boom)
        rc = run(["pubmed-search", "--query", "x[NotARealTag]", "--target", str(_kb(tmp_path))])
        assert rc == 1
        assert "NotARealTag" in capsys.readouterr().err

    def test_reversed_year_is_rejected_before_a_request(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_make_pubmed_client",
                            lambda config: (_ for _ in ()).throw(AssertionError("built")))
        rc = run(["pubmed-search", "--query", "cancer", "--year", "2015-2010",
                  "--target", str(_kb(tmp_path))])
        assert rc == 1
        assert "backwards" in capsys.readouterr().err


# -- the silent-zero guard --------------------------------------------------

class TestSilentZeroGuard:
    def test_nonexistent_mesh_term_surfaces_a_warning_not_bare_zero(self, tmp_path, fake, capsys):
        fake(FakePubMedClient(
            esearch_body=_esearch(count=0, errors=[("PhraseNotFound", "notamesh")])))
        rc = run(["pubmed-search", "--query", "cancer", "--mesh", "notamesh",
                  "--target", str(_kb(tmp_path))])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Found 0 results." in captured.out
        assert "notamesh" in captured.err
        assert "nonexistent MeSH term" in captured.err

    def test_filtered_zero_is_surfaced_even_without_a_pubmed_signal(self, tmp_path, fake, capsys):
        fake(FakePubMedClient(esearch_body=_esearch(count=0)))
        rc = run(["pubmed-search", "--query", "cancer", "--year", "2020",
                  "--target", str(_kb(tmp_path))])
        assert rc == 0
        err = capsys.readouterr().err
        assert "filter was applied" in err and "'2020'" in err

    def test_honest_empty_set_prints_no_guard_warning(self, tmp_path, fake, capsys):
        fake(FakePubMedClient(esearch_body=_esearch(count=0)))
        rc = run(["pubmed-search", "--query", "asdfqwerzxcv", "--target", str(_kb(tmp_path))])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Found 0 results." in captured.out
        assert "filter was applied" not in captured.err
        assert "nonexistent MeSH" not in captured.err

    def test_zero_results_still_surfaces_the_query_translation(self, tmp_path, fake, capsys):
        # A zero is where "how did PubMed read my words" matters most; the
        # QueryTranslation must not be hidden by the empty-result early return.
        fake(FakePubMedClient(
            esearch_body=_esearch(count=0, query_translation="asdfqwerzxcv[All Fields]")))
        rc = run(["pubmed-search", "--query", "asdfqwerzxcv", "--target", str(_kb(tmp_path))])
        assert rc == 0
        out = capsys.readouterr().out
        assert "Found 0 results." in out
        assert "PubMed read the query as: asdfqwerzxcv[All Fields]" in out

    def test_top_level_error_exits_nonzero(self, tmp_path, fake, capsys):
        fake(FakePubMedClient(esearch_body="<eSearchResult><ERROR>Invalid db name</ERROR></eSearchResult>"))
        rc = run(["pubmed-search", "--query", "cancer", "--target", str(_kb(tmp_path))])
        assert rc == 1
        assert "Invalid db name" in capsys.readouterr().err


# -- listing ----------------------------------------------------------------

class TestListing:
    def test_results_are_listed_with_title_and_query_translation(self, tmp_path, fake, capsys):
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=("111",), query_translation="crispr[All Fields]"),
            efetch_body=_efetch(_article("111", title="A CRISPR paper"))))
        rc = run(["pubmed-search", "--query", "crispr", "--target", str(_kb(tmp_path))])
        assert rc == 0
        out = capsys.readouterr().out
        assert "PMID 111" in out and "A CRISPR paper" in out
        assert "PubMed read the query as: crispr[All Fields]" in out

    def test_porcelain_result_rows_and_found_count(self, tmp_path, fake, capsys):
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=("111",)),
            efetch_body=_efetch(_article("111", title="A paper"))))
        rc = run(["pubmed-search", "--query", "crispr", "--porcelain",
                  "--target", str(_kb(tmp_path)), "--all"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "result\t1\t111\t-\tA paper" in out
        assert "found\t1" in out

    def test_retracted_result_is_flagged(self, tmp_path, fake, capsys):
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=("111",)),
            efetch_body=_efetch(_article("111", retracted=True))))
        rc = run(["pubmed-search", "--query", "crispr", "--target", str(_kb(tmp_path))])
        assert rc == 0
        assert "RETRACTED" in capsys.readouterr().out

    def test_efetch_is_asked_for_the_returned_pmids(self, tmp_path, fake):
        client = fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=("111",)),
            efetch_body=_efetch(_article("111"))))
        run(["pubmed-search", "--query", "crispr", "--target", str(_kb(tmp_path))])
        assert ("efetch", ("111",)) in client.calls


# -- selection seam (import deferred to #166) -------------------------------

class TestSelectionSeam:
    def test_all_selects_every_result_and_reports_the_deferred_import(self, tmp_path, fake, capsys):
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=("111",)),
            efetch_body=_efetch(_article("111"))))
        rc = run(["pubmed-search", "--query", "crispr", "--all", "--target", str(_kb(tmp_path))])
        assert rc == 0
        err = capsys.readouterr().err
        assert "#166" in err and "no files were written" in err

    def test_non_tty_without_all_selects_nothing(self, tmp_path, fake, monkeypatch, capsys):
        # A non-interactive run that is not --all must not guess; it selects nothing
        # rather than silently importing every hit (the CI silent-zero, #167).
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=("111",)),
            efetch_body=_efetch(_article("111"))))
        rc = run(["pubmed-search", "--query", "crispr", "--target", str(_kb(tmp_path))])
        assert rc == 0
        captured = capsys.readouterr()
        assert "no files were written" not in captured.err
        assert "Nothing selected" in captured.out


# -- connection failure exits 2 ---------------------------------------------

def test_connection_failure_exits_two(tmp_path, fake, capsys):
    from factlog.integrations.pubmed.client import PubMedConnectionError

    fake(FakePubMedClient(raise_on={"esearch": PubMedConnectionError("no network")}))
    rc = run(["pubmed-search", "--query", "crispr", "--target", str(_kb(tmp_path))])
    assert rc == 2
    assert "no network" in capsys.readouterr().err
