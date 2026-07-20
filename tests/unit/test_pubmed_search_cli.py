# SPDX-License-Identifier: Apache-2.0
"""CLI tests for `factlog pubmed-search` (#167, wired to #166's importer).

The real PubMed client is replaced via ``_make_pubmed_client`` so the command runs
without the network. A temp KB (with sources/ and a policy file supplying the
required NCBI contact email) is the target. The focus is the silent-zero guard,
the --show-query/--dry-run distinction (the issue's Done-when), and — now that the
seam is connected — that a selection actually imports through #166's path.
"""
from __future__ import annotations

import pytest

from factlog import cli


def _kb(tmp_path):
    # A KB with the contact email `_pubmed_prepare` requires for any request-spending
    # run (mirrors test_pubmed_cli.py's fixture); NCBI must not see anonymous traffic.
    (tmp_path / "sources").mkdir()
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "pubmed-config.toml").write_text(
        '[client]\nemail = "test@example.com"\n', encoding="utf-8"
    )
    return tmp_path


def _kb_no_email(tmp_path):
    # No email configured: used to prove --show-query needs neither KB nor email
    # (it sends nothing), and that a request-spending run refuses without one.
    (tmp_path / "sources").mkdir()
    return tmp_path


def _sources(kb):
    return sorted(p.name for p in (kb / "sources").glob("*.md"))


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


# What NCBI actually sends for *every* zero: the count, and boilerplate. A zero-count
# esearch body without it is a shape PubMed never returns (#271).
_NCBI_ZERO_BOILERPLATE = [("OutputMessage", "No items found.")]

# A live capture: `sepsis` + the valid MeSH descriptor `Sepsis` + year 1810 — a real
# filter that legitimately narrows to zero, carrying no diagnostic, only boilerplate.
# The issue's reproduction command replayed through the CLI.
_LIVE_VALID_MESH_ZERO = (
    "<eSearchResult><Count>0</Count><RetMax>0</RetMax><RetStart>0</RetStart><IdList/>"
    "<TranslationSet><Translation>     <From>sepsis</From>     "
    '<To>"sepsis"[MeSH Terms] OR "sepsis"[All Fields]</To>    </Translation>'
    "<Translation>     <From>Sepsis[MeSH Terms]</From>     "
    '<To>"sepsis"[MeSH Terms]</To>    </Translation></TranslationSet>'
    '<QueryTranslation>("sepsis"[MeSH Terms] OR "sepsis"[All Fields]) AND '
    '"sepsis"[MeSH Terms] AND 1810/01/01:1810/12/31[Date - Publication]'
    "</QueryTranslation><WarningList><OutputMessage>No items found.</OutputMessage>"
    "</WarningList></eSearchResult>"
)

# A live capture: `--query qzxwvunonsenseterm`, sent WITHOUT quotes. PubMed quotes the
# phrase in its own warning; QueryTranslation shows no quotes, so ATM was never off (#272).
_LIVE_UNQUOTED_NONSENSE_ZERO = (
    "<eSearchResult><Count>0</Count><RetMax>0</RetMax><RetStart>0</RetStart><IdList/>"
    "<TranslationSet/>"
    "<QueryTranslation>qzxwvunonsenseterm</QueryTranslation>"
    "<WarningList><QuotedPhraseNotFound>\"qzxwvunonsenseterm\"</QuotedPhraseNotFound>"
    "<OutputMessage>No items found.</OutputMessage></WarningList></eSearchResult>"
)


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


def _print_electronic_article(pmid, *, issue_year, article_date, title="A paper"):
    """A `PubModel="Print-Electronic"` record: online one year, in a print issue later.

    Shaped after PMID 41620285 (#387). `<ArticleDate DateType="Electronic">` is what
    PubMed's [Date - Publication] filter matched; `JournalIssue/PubDate/Year` is what
    reaches front matter. Their disagreement is the case under test, so both are real
    elements here rather than a preset `.year` on a stub.
    """
    year, month, day = article_date.split("-")
    return (
        "<PubmedArticle><MedlineCitation>"
        f"<PMID Version='1'>{pmid}</PMID>"
        "<Article PubModel='Print-Electronic'>"
        f"<ArticleTitle>{title}</ArticleTitle>"
        "<AuthorList><Author><LastName>Doe</LastName><ForeName>Jane</ForeName></Author></AuthorList>"
        "<Journal><Title>Nature</Title><JournalIssue CitedMedium='Internet'>"
        f"<PubDate><Year>{issue_year}</Year></PubDate></JournalIssue></Journal>"
        f"<ArticleDate DateType='Electronic'><Year>{year}</Year>"
        f"<Month>{month}</Month><Day>{day}</Day></ArticleDate>"
        "</Article></MedlineCitation></PubmedArticle>"
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
        # --show-query sends nothing, so it needs nothing: no client, and (below) no
        # email either. A KB without email is deliberately used to prove that.
        def boom(config):
            raise AssertionError("--show-query built the client / sent a request")
        monkeypatch.setattr(cli, "_make_pubmed_client", boom)
        rc = run(["pubmed-search", "--query", "crispr cas9", "--mesh", "Neoplasms",
                  "--show-query", "--target", str(_kb_no_email(tmp_path))])
        assert rc == 0
        out = capsys.readouterr().out
        assert "no request sent" in out
        assert "crispr cas9 AND Neoplasms[MeSH Terms]" in out

    def test_show_query_porcelain_is_a_query_row(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_make_pubmed_client",
                            lambda config: (_ for _ in ()).throw(AssertionError("built")))
        rc = run(["pubmed-search", "--query", "brca1", "--show-query", "--porcelain",
                  "--target", str(_kb_no_email(tmp_path))])
        assert rc == 0
        assert capsys.readouterr().out.strip() == "query\tbrca1"

    def test_a_request_spending_run_refuses_without_an_email(self, tmp_path, monkeypatch, capsys):
        # The other side of the same gate: an actual search (not --show-query) refuses
        # before a request when no contact email is configured (#166's rule, now shared
        # by search). Isolate XDG so no ambient ~/.config email leaks in.
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        monkeypatch.delenv("NCBI_API_KEY", raising=False)
        monkeypatch.setattr(cli, "_make_pubmed_client",
                            lambda config: (_ for _ in ()).throw(AssertionError("built")))
        rc = run(["pubmed-search", "--query", "crispr", "--target", str(_kb_no_email(tmp_path))])
        assert rc == 1
        assert "email" in capsys.readouterr().err


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
        # The real body: PhraseNotFound *and* the boilerplate every zero carries. Both
        # the diagnostic and the filter line are owed; the boilerplate is not.
        fake(FakePubMedClient(esearch_body=_esearch(
            count=0,
            errors=[("PhraseNotFound", "notamesh")],
            warnings=_NCBI_ZERO_BOILERPLATE,
        )))
        rc = run(["pubmed-search", "--query", "cancer", "--mesh", "notamesh",
                  "--target", str(_kb(tmp_path))])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Found 0 results." in captured.out
        assert "notamesh" in captured.err
        assert "nonexistent MeSH term" in captured.err
        assert "filter was applied" in captured.err
        assert "OutputMessage" not in captured.err

    def test_filtered_zero_names_the_filter_through_ncbi_boilerplate(self, tmp_path, fake, capsys):
        fake(FakePubMedClient(esearch_body=_esearch(count=0, warnings=_NCBI_ZERO_BOILERPLATE)))
        rc = run(["pubmed-search", "--query", "cancer", "--year", "2020",
                  "--target", str(_kb(tmp_path))])
        assert rc == 0
        err = capsys.readouterr().err
        assert "filter was applied" in err and "'2020'" in err
        assert "OutputMessage" not in err

    def test_valid_mesh_filtered_zero_names_the_filter(self, tmp_path, fake, capsys):
        # The issue's own reproduction (#271), replayed end-to-end through the CLI with
        # the body NCBI actually returned: a valid MeSH term filtered to zero carries no
        # diagnostic, only boilerplate — the filter line must still reach the operator.
        fake(FakePubMedClient(esearch_body=_LIVE_VALID_MESH_ZERO))
        rc = run(["pubmed-search", "--query", "sepsis", "--mesh", "Sepsis",
                  "--year", "1810", "--target", str(_kb(tmp_path))])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Found 0 results." in captured.out
        assert "filter was applied" in captured.err
        assert "'Sepsis'" in captured.err and "'1810'" in captured.err
        assert "OutputMessage" not in captured.err
        assert "No items found" not in captured.err

    def test_a_recorded_year_outside_the_requested_range_is_surfaced(
            self, tmp_path, fake, capsys):
        # #387 end-to-end: --year 2022-2025 matched PMID 41620285 on its electronic
        # date (2025-04-16), but the issue year it will be recorded with is 2026.
        # The operator hears about it before the file lands, on stderr.
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=["41620285"]),
            efetch_body=_efetch(_print_electronic_article("41620285", issue_year="2026",
                                                          article_date="2025-04-16")),
        ))
        rc = run(["pubmed-search", "--query", "base editing", "--year", "2022-2025",
                  "--target", str(_kb(tmp_path))])
        assert rc == 0
        err = capsys.readouterr().err
        assert "41620285" in err
        assert "2026" in err and "2022-2025" in err
        # It must read as an explanation, not as a factlog bug.
        assert "electronic" in err and "journal issue" in err

    def test_the_out_of_range_year_never_blocks_the_import(self, tmp_path, fake, capsys):
        # The record is a genuine match; surfacing it must not drop it. --all still
        # writes the file, and the exit code stays a success.
        kb = _kb(tmp_path)
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=["41620285"]),
            efetch_body=_efetch(_print_electronic_article("41620285", issue_year="2026",
                                                          article_date="2025-04-16")),
        ))
        rc = run(["pubmed-search", "--query", "base editing", "--year", "2022-2025",
                  "--all", "--target", str(kb)])
        assert rc == 0
        assert _sources(kb) != []
        assert "41620285" in capsys.readouterr().err

    def test_the_warning_rides_stderr_under_porcelain(self, tmp_path, fake, capsys):
        # --porcelain stdout must stay parseable: the warning belongs on stderr, and
        # no prose may leak into the result rows.
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=["41620285"]),
            efetch_body=_efetch(_print_electronic_article("41620285", issue_year="2026",
                                                          article_date="2025-04-16")),
        ))
        rc = run(["pubmed-search", "--query", "base editing", "--year", "2022-2025",
                  "--porcelain", "--target", str(_kb(tmp_path))])
        assert rc == 0
        captured = capsys.readouterr()
        assert "41620285" in captured.err
        for line in captured.out.splitlines():
            assert line.startswith(("result\t", "found\t"))

    def test_a_result_inside_the_range_triggers_no_year_warning(self, tmp_path, fake, capsys):
        # The counterexample at the CLI seam: an in-range issue year stays quiet.
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=["40000001"]),
            efetch_body=_efetch(_print_electronic_article("40000001", issue_year="2024",
                                                          article_date="2023-11-02")),
        ))
        rc = run(["pubmed-search", "--query", "base editing", "--year", "2022-2025",
                  "--target", str(_kb(tmp_path))])
        assert rc == 0
        assert "will be recorded as year" not in capsys.readouterr().err

    def test_honest_empty_set_prints_no_guard_warning(self, tmp_path, fake, capsys):
        # No filter, no diagnostic — only the boilerplate every zero carries. Neither the
        # guard nor the boilerplate may make a plain "0 results" noisy.
        fake(FakePubMedClient(esearch_body=_esearch(count=0, warnings=_NCBI_ZERO_BOILERPLATE)))
        rc = run(["pubmed-search", "--query", "asdfqwerzxcv", "--target", str(_kb(tmp_path))])
        assert rc == 0
        captured = capsys.readouterr()
        assert "Found 0 results." in captured.out
        assert "filter was applied" not in captured.err
        assert "nonexistent MeSH" not in captured.err
        assert "OutputMessage" not in captured.err

    def test_unquoted_query_is_never_told_to_drop_quotes(self, tmp_path, fake, capsys):
        # The issue's reproduction (#272), replayed end-to-end with the body NCBI
        # actually returned for an *unquoted* single token. The CLI must hand the raw
        # `--query` to the guard — only the request knows whether the user quoted, and
        # a unit test on the guard alone cannot catch that wiring being missing.
        fake(FakePubMedClient(esearch_body=_LIVE_UNQUOTED_NONSENSE_ZERO))
        rc = run(["pubmed-search", "--query", "qzxwvunonsenseterm",
                  "--target", str(_kb(tmp_path))])
        assert rc == 0
        captured = capsys.readouterr()
        assert "qzxwvunonsenseterm" in captured.err
        assert "drop the quotes" not in captured.err
        assert "disables Automatic Term Mapping" not in captured.err
        assert "Found 0 results." in captured.out
        assert "PubMed read the query as: qzxwvunonsenseterm" in captured.out

    def test_zero_results_still_surfaces_the_query_translation(self, tmp_path, fake, capsys):
        # A zero is where "how did PubMed read my words" matters most; the
        # QueryTranslation must not be hidden by the empty-result early return.
        fake(FakePubMedClient(esearch_body=_esearch(
            count=0,
            query_translation="asdfqwerzxcv[All Fields]",
            warnings=_NCBI_ZERO_BOILERPLATE,
        )))
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


# -- selection -> import through #166's path --------------------------------

class TestSelectionImport:
    def test_all_imports_every_result_into_sources(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=("111",)),
            efetch_body=_efetch(_article("111", title="A CRISPR paper"))))
        rc = run(["pubmed-search", "--query", "crispr", "--all", "--target", str(kb)])
        assert rc == 0
        # A real source file was written through #166's PubMedSourceWriter.
        assert len(_sources(kb)) == 1
        assert "Imported: 1" in capsys.readouterr().out

    def test_dry_run_all_writes_nothing(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=("111",)),
            efetch_body=_efetch(_article("111"))))
        rc = run(["pubmed-search", "--query", "crispr", "--all", "--dry-run",
                  "--target", str(kb)])
        assert rc == 0
        assert _sources(kb) == []
        assert "Would import: 1" in capsys.readouterr().out

    def test_all_porcelain_emits_the_import_summary_after_the_listing(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=("111",)),
            efetch_body=_efetch(_article("111", title="A paper"))))
        rc = run(["pubmed-search", "--query", "crispr", "--all", "--porcelain",
                  "--target", str(kb)])
        assert rc == 0
        out = capsys.readouterr().out
        # Two blocks in one stream (as arxiv-search does): the listing rows then the
        # import summary rows.
        assert "result\t1\t111\t-\tA paper" in out
        assert "found\t1" in out
        assert "imported\t1" in out
        assert len(_sources(kb)) == 1

    def test_non_tty_without_all_imports_nothing(self, tmp_path, fake, monkeypatch, capsys):
        # A non-interactive run that is not --all must not guess; it selects nothing
        # rather than silently importing every hit (the CI silent-zero, #167).
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
        kb = _kb(tmp_path)
        fake(FakePubMedClient(
            esearch_body=_esearch(count=1, ids=("111",)),
            efetch_body=_efetch(_article("111"))))
        rc = run(["pubmed-search", "--query", "crispr", "--target", str(kb)])
        assert rc == 0
        captured = capsys.readouterr()
        assert _sources(kb) == []
        assert "Nothing selected" in captured.out


# -- connection failure exits 2 ---------------------------------------------

def test_connection_failure_exits_two(tmp_path, fake, capsys):
    from factlog.integrations.pubmed.client import PubMedConnectionError

    fake(FakePubMedClient(raise_on={"esearch": PubMedConnectionError("no network")}))
    rc = run(["pubmed-search", "--query", "crispr", "--target", str(_kb(tmp_path))])
    assert rc == 2
    assert "no network" in capsys.readouterr().err
