# SPDX-License-Identifier: Apache-2.0
"""CLI tests for ``factlog pubmed-import`` (#166).

The real PubMed client is replaced via ``_make_pubmed_client`` so the command
runs without the network; the fake replays canned efetch XML. A temp KB (with a
``policy/pubmed-config.toml`` supplying the required contact email) is the target.
"""
from __future__ import annotations

import pytest

from factlog import cli
from factlog.integrations.pubmed.client import PubMedConnectionError, PubMedError


def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "pubmed-config.toml").write_text(
        '[client]\nemail = "test@example.com"\n', encoding="utf-8"
    )
    return tmp_path


def _record(pmid, *, title="A paper", doi="10.1/x", year="2020"):
    doi_xml = f'<ArticleId IdType="doi">{doi}</ArticleId>' if doi else ""
    return f"""
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <Journal><Title>J</Title>
          <JournalIssue><PubDate><Year>{year}</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>{title}</ArticleTitle>
        <AuthorList><Author><LastName>Author</LastName><ForeName>Ann</ForeName></Author></AuthorList>
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">{pmid}</ArticleId>{doi_xml}
    </ArticleIdList></PubmedData>
  </PubmedArticle>"""


def _set(*records):
    return "<PubmedArticleSet>" + "".join(records) + "</PubmedArticleSet>"


class FakeClient:
    """Replays a canned efetch body; records the ids it was asked for."""

    def __init__(self, xml=None, raise_exc=None):
        self._xml = xml if xml is not None else _set(_record("32738937"))
        self._raise = raise_exc
        self.calls: list[list[str]] = []

    def efetch(self, pmids):
        self.calls.append([str(p) for p in pmids])
        if self._raise is not None:
            raise self._raise
        return self._xml


@pytest.fixture
def fake(monkeypatch):
    def install(client):
        monkeypatch.setattr(cli, "_make_pubmed_client", lambda config: client)
        return client
    return install


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def sources(kb):
    return sorted(p.name for p in (kb / "sources").glob("*.md"))


class TestImport:
    def test_import_by_pmid_writes_a_source(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        assert run(["pubmed-import", "--pmid", "32738937", "--target", str(kb)]) == 0
        assert client.calls == [["32738937"]]
        assert len(sources(kb)) == 1
        assert "Imported: 1" in capsys.readouterr().out

    def test_pmid_prefix_is_accepted(self, tmp_path, fake):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        run(["pubmed-import", "--pmid", "pmid:32738937", "--target", str(kb)])
        assert client.calls == [["32738937"]]  # normalized before the request

    def test_batch_import_is_one_request(self, tmp_path, fake):
        kb = _kb(tmp_path)
        client = fake(FakeClient(_set(_record("111", title="A"), _record("222", title="B"))))
        assert run(["pubmed-import", "--pmid", "111", "--pmid", "222", "--target", str(kb)]) == 0
        # The client owns pacing; a batch efetch is a single call.
        assert client.calls == [["111", "222"]]
        assert len(sources(kb)) == 2

    def test_pmid_is_required(self):
        with pytest.raises(SystemExit):
            run(["pubmed-import", "--target", "x"])

    def test_a_deleted_pmid_is_an_error_exit(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(_set()))  # empty set == deleted
        assert run(["pubmed-import", "--pmid", "999", "--target", str(kb)]) == 1
        assert "Errors:   1" in capsys.readouterr().out
        assert sources(kb) == []


class TestDryRun:
    def test_dry_run_writes_nothing(self, tmp_path, fake):
        kb = _kb(tmp_path)
        fake(FakeClient())
        assert run(["pubmed-import", "--pmid", "32738937", "--target", str(kb), "--dry-run"]) == 0
        assert sources(kb) == []
        assert not (kb / "source-provenance").exists()

    def test_dry_run_still_queries(self, tmp_path, fake):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        run(["pubmed-import", "--pmid", "32738937", "--target", str(kb), "--dry-run"])
        assert client.calls == [["32738937"]]


class TestPorcelain:
    def test_porcelain_emits_the_summary_contract(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient())
        assert run(["pubmed-import", "--pmid", "32738937", "--target", str(kb), "--porcelain"]) == 0
        out = capsys.readouterr().out
        assert "imported\t1" in out
        assert "skipped\t0" in out
        assert "merged\t0" in out
        assert "errors\t0" in out
        assert "dry_run\t0" in out
        assert f"target\t{kb / 'sources'}" in out

    def test_porcelain_dry_run_emits_a_per_record_row(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient())
        run(["pubmed-import", "--pmid", "32738937", "--target", str(kb), "--porcelain", "--dry-run"])
        rows = [l for l in capsys.readouterr().out.splitlines() if l.startswith("work\t")]
        assert len(rows) == 1
        fields = rows[0].split("\t")
        assert fields[1] == "imported"
        assert fields[2] == "32738937"

    def test_a_tab_in_a_bad_pmid_never_splits_a_porcelain_row(self, tmp_path, fake, capsys):
        # An invalid PMID's raw text becomes an error outcome's key. A tab in it
        # would add a column to a positional row unless neutralized (#141).
        kb = _kb(tmp_path)
        fake(FakeClient())
        run(["pubmed-import", "--pmid", "12\t34", "--target", str(kb), "--porcelain", "--dry-run"])
        rows = [l for l in capsys.readouterr().out.splitlines() if l.startswith("work\t")]
        assert len(rows) == 1
        # Exactly four fields: work, status, key, name — the tab was neutralized.
        assert len(rows[0].split("\t")) == 4


class TestConfigAndTransport:
    def test_missing_email_refuses_before_any_request(self, tmp_path, fake, capsys):
        # A KB with sources/ but no email configured; NCBI must not see anonymous traffic.
        (tmp_path / "sources").mkdir()
        client = fake(FakeClient())
        # No XDG email either (isolate the env).
        import os
        old = os.environ.get("XDG_CONFIG_HOME")
        os.environ["XDG_CONFIG_HOME"] = str(tmp_path / "empty-config")
        try:
            code = run(["pubmed-import", "--pmid", "32738937", "--target", str(tmp_path)])
        finally:
            if old is None:
                os.environ.pop("XDG_CONFIG_HOME", None)
            else:
                os.environ["XDG_CONFIG_HOME"] = old
        assert code == 1
        assert "email" in capsys.readouterr().err
        assert client.calls == []  # never reached the wire

    def test_connection_error_exits_2(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(raise_exc=PubMedConnectionError("cannot reach eutils")))
        assert run(["pubmed-import", "--pmid", "32738937", "--target", str(kb)]) == 2
        assert "cannot reach" in capsys.readouterr().err

    def test_a_request_error_exits_1(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        fake(FakeClient(raise_exc=PubMedError("E-utilities rejected the request")))
        assert run(["pubmed-import", "--pmid", "32738937", "--target", str(kb)]) == 1
        assert "rejected" in capsys.readouterr().err


class TestCrossSourceMergeThroughCli:
    def test_a_pubmed_import_merges_onto_an_existing_openalex_doi(self, tmp_path, fake, capsys):
        # Seed an OpenAlex-primary paper sharing the DOI, then import from PubMed:
        # it must merge, not write a second file.
        from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
        from factlog.integrations.openalex.work_parser import ParsedWork

        doi = "10.1016/s0140-6736(20)30367-6"
        kb = _kb(tmp_path)
        OpenAlexSourceWriter().write(
            ParsedWork(openalex_id="W1", title="A paper", authors=("Ann Author",),
                       year=2020, journal="J", doi=doi, pmid=None, arxiv_id=None,
                       work_type="article"),
            kb, imported_at="t")
        before = sources(kb)
        fake(FakeClient(_set(_record("32738937", doi=doi))))
        assert run(["pubmed-import", "--pmid", "32738937", "--target", str(kb)]) == 0
        out = capsys.readouterr().out
        assert "Merged:   1" in out
        assert sources(kb) == before  # no second .md
