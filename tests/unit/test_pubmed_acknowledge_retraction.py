# SPDX-License-Identifier: Apache-2.0
"""`factlog pubmed-acknowledge-retraction` — the human gate that stops the repeat (#171).

The PubMed sibling of `arxiv-acknowledge-withdrawal` / `openalex-acknowledge-retraction`
on the shared acknowledge primitive, in PubMed's vocabulary only. The real PubMed client
is replaced via `_make_pubmed_client` so the command runs without the network; the fake
replays canned efetch XML. A temp KB carries source `.md` originals and their PubMed
provenance ledgers.

Proven here, with the real CLI (never a hand-rolled assertion of the comparison):

1. The command live-queries PubMed (efetch), refuses without a terminal/`--yes`, and
   refuses BEFORE the query (zero requests) when there is no ledger / an unreadable ledger.
2. It records a retraction, silences the repeat once run (a second run is a
   nothing-to-acknowledge no-op that leaves the ledger byte-identical), writes nothing on a
   deleted/merged/failed query, and never opens the `.md` (byte- and `mtime_ns`-identical).
3. `--yes` may record a retraction but may NEVER clear one (#106); the clear path exists
   only at the interactive prompt.
4. A paper with no ledger is pointed at `pubmed-backfill-provenance` (#107/#110).
"""
from __future__ import annotations

import pytest

from factlog import cli
from factlog.integrations.common.provenance import (
    Provenance,
    SourceRecord,
    read_provenance,
    sidecar_path,
    write_provenance,
)
from factlog.integrations.pubmed.client import PubMedConnectionError, PubMedError

IMPORTED_AT = "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _kb(tmp_path):
    (tmp_path / "sources").mkdir(exist_ok=True)
    (tmp_path / "policy").mkdir(exist_ok=True)
    (tmp_path / "policy" / "pubmed-config.toml").write_text(
        '[client]\nemail = "test@example.com"\n', encoding="utf-8"
    )
    return tmp_path


def _retracted_record(pmid, *, notice_pmid="18842931"):
    notice = (
        f"<CommentsCorrectionsList><CommentsCorrections RefType=\"RetractionIn\">"
        f"<PMID>{notice_pmid}</PMID></CommentsCorrections></CommentsCorrectionsList>"
        if notice_pmid
        else ""
    )
    return f"""
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <Journal><Title>J</Title>
          <JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>A retracted paper</ArticleTitle>
        <AuthorList><Author><LastName>Author</LastName><ForeName>Ann</ForeName></Author></AuthorList>
        <PublicationTypeList>
          <PublicationType UI="D016428">Journal Article</PublicationType>
          <PublicationType UI="D016441">Retracted Publication</PublicationType>
        </PublicationTypeList>
      </Article>
      {notice}
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">{pmid}</ArticleId>
    </ArticleIdList></PubmedData>
  </PubmedArticle>"""


def _plain_record(pmid, *, title="A paper"):
    return f"""
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <Journal><Title>J</Title>
          <JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>{title}</ArticleTitle>
        <AuthorList><Author><LastName>Author</LastName><ForeName>Ann</ForeName></Author></AuthorList>
        <PublicationTypeList>
          <PublicationType UI="D016428">Journal Article</PublicationType>
        </PublicationTypeList>
      </Article>
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">{pmid}</ArticleId>
    </ArticleIdList></PubmedData>
  </PubmedArticle>"""


def _set(*records):
    return "<PubmedArticleSet>" + "".join(records) + "</PubmedArticleSet>"


def _seed(kb, pmid="32738937", *, retracted=False, notice_pmid=None, name=None,
         extra_records=()):
    """Write a source .md and its PubMed provenance ledger. Returns the .md path.

    ``notice_pmid`` seeds the second half of PubMed's retraction signal
    (``retraction_notice_pmid``) alongside ``retracted``, reproducing the real import
    ledger shape (``source_writer._provenance_record`` writes BOTH, #202) — the shape the
    single-field seed did not, which is why the orphaned-notice bug went unseen.
    """
    (kb / "sources").mkdir(exist_ok=True)
    name = name or pmid
    md = kb / "sources" / f"{name}.md"
    fm = [f"pmid: {pmid}", "imported_from: pubmed"]
    if retracted:
        fm.append("pubmed_retracted: true")
        if notice_pmid:
            fm.append(f"pubmed_retraction_notice_pmid: {notice_pmid}")
    md.write_text("---\n" + "\n".join(fm) + "\n---\n# body\n", encoding="utf-8")
    fields = {"journal": "J"}
    if retracted:
        fields["retracted"] = True
        if notice_pmid:
            fields["retraction_notice_pmid"] = notice_pmid
    records = [SourceRecord(type="pubmed", id=pmid, imported_at=IMPORTED_AT, fields=fields),
               *extra_records]
    write_provenance(sidecar_path(md, kb), Provenance(records=records))
    return md


def _seed_front_matter_only(kb, pmid="32738937", *, name=None):
    """A paper imported before its ledger existed: front matter only, no sidecar."""
    (kb / "sources").mkdir(exist_ok=True)
    name = name or pmid
    md = kb / "sources" / f"{name}.md"
    md.write_text(f"---\npmid: {pmid}\nimported_from: pubmed\n---\n# body\n", encoding="utf-8")
    return md


class FakeClient:
    """Replays a canned efetch body; records the ids it was asked for."""

    def __init__(self, xml=None, *, raise_exc=None):
        self._xml = xml if xml is not None else _set(_retracted_record("32738937"))
        self._raise = raise_exc
        self.calls: list[list[str]] = []

    def efetch(self, pmids):
        self.calls.append([str(p) for p in pmids])
        if self._raise is not None:
            raise self._raise
        return self._xml

    @property
    def call_count(self):
        return len(self.calls)


@pytest.fixture
def fake(monkeypatch):
    def install(client):
        monkeypatch.setattr(cli, "_make_pubmed_client", lambda config: client)
        return client
    return install


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def _ledger(md, kb):
    return {(r.type, r.id): r.to_dict()
            for r in read_provenance(sidecar_path(md, kb)).records}


def _stat(path):
    st = path.stat()
    return (path.read_bytes(), st.st_mtime_ns)


# --------------------------------------------------------------------------- #
# recording a retraction, and silencing the repeat
# --------------------------------------------------------------------------- #
class TestRecord:
    def test_records_a_new_retraction_with_yes(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        md = _seed(kb, retracted=False)  # ledger has no retraction yet
        before_md = _stat(md)
        client = fake(FakeClient(_set(_retracted_record("32738937"))))

        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 0
        assert client.calls == [["32738937"]]  # one live efetch
        assert _ledger(md, kb)[("pubmed", "32738937")]["retracted"] is True
        assert _stat(md) == before_md  # P4: the .md is byte- and mtime-identical
        out = capsys.readouterr().out
        assert "Recorded PubMed's retraction for 32738937" in out
        assert "pubmed-refresh will no longer repeat" in out

    def test_a_second_acknowledge_is_a_byte_identical_noop(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        md = _seed(kb, retracted=True)  # already recorded
        before = _stat(sidecar_path(md, kb))
        client = fake(FakeClient(_set(_retracted_record("32738937"))))

        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 0
        assert "nothing to acknowledge" in capsys.readouterr().out
        # The ledger is untouched to the byte and the mtime.
        assert _stat(sidecar_path(md, kb)) == before

    def test_pmid_prefix_is_accepted(self, tmp_path, fake):
        kb = _kb(tmp_path)
        _seed(kb, "32738937", retracted=False)
        client = fake(FakeClient(_set(_retracted_record("32738937"))))
        rc = run(["pubmed-acknowledge-retraction", "--id", "pmid:32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 0
        assert client.calls == [["32738937"]]  # normalized before the request


# --------------------------------------------------------------------------- #
# #202 — the retraction is a TWO-field signal; both move together
# --------------------------------------------------------------------------- #
class TestBothFieldsMoveTogether:
    """`retracted` and `retraction_notice_pmid` are one signal (the import writes both).

    Seeding BOTH — the real import ledger shape — is what the single-field `_seed` did not,
    so these are the tests that would have caught the orphaned notice PMID (#202).
    """

    def test_record_writes_both_fields(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        md = _seed(kb, retracted=False)  # not yet retracted in the ledger
        # Live PubMed flags a retraction and links its notice PMID.
        client = fake(FakeClient(
            _set(_retracted_record("32738937", notice_pmid="18842931"))
        ))
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 0
        record = _ledger(md, kb)[("pubmed", "32738937")]
        # BOTH fields land, matching the import ledger's shape — not `retracted` alone.
        assert record["retracted"] is True
        assert record["retraction_notice_pmid"] == "18842931"

    def test_record_without_a_linkable_notice_omits_the_field(self, tmp_path, fake):
        kb = _kb(tmp_path)
        md = _seed(kb, retracted=False)
        # A real retraction whose notice carries no PMID (spike §1): retracted, no link.
        client = fake(FakeClient(
            _set(_retracted_record("32738937", notice_pmid=None))
        ))
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 0
        record = _ledger(md, kb)[("pubmed", "32738937")]
        assert record["retracted"] is True
        # Absent, exactly as the import omits an unlinkable notice — never a null.
        assert "retraction_notice_pmid" not in record

    def test_interactive_clear_drops_both_fields(self, tmp_path, fake, capsys, monkeypatch):
        kb = _kb(tmp_path)
        # The real import ledger shape: BOTH fields present (the #202 reproduction).
        md = _seed(kb, retracted=True, notice_pmid="18842931")
        assert "retraction_notice_pmid" in _ledger(md, kb)[("pubmed", "32738937")]
        # PubMed reversed the retraction.
        client = fake(FakeClient(_set(_plain_record("32738937"))))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937", "--target", str(kb)])
        assert rc == 0
        record = _ledger(md, kb)[("pubmed", "32738937")]
        # BOTH fields drop — no orphaned notice PMID on a record that says "not retracted".
        assert "retracted" not in record
        assert "retraction_notice_pmid" not in record

    def test_seeded_both_fields_a_second_record_is_a_byte_identical_noop(
        self, tmp_path, fake, capsys
    ):
        kb = _kb(tmp_path)
        md = _seed(kb, retracted=True, notice_pmid="18842931")  # already fully recorded
        before = _stat(sidecar_path(md, kb))
        client = fake(FakeClient(
            _set(_retracted_record("32738937", notice_pmid="18842931"))
        ))
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 0
        assert "nothing to acknowledge" in capsys.readouterr().out
        # Both fields already held: the ledger stays byte- and mtime_ns-identical.
        assert _stat(sidecar_path(md, kb)) == before


# --------------------------------------------------------------------------- #
# #107 — verify before the request
# --------------------------------------------------------------------------- #
class TestRefusesBeforeTheRequest:
    def test_no_tty_without_yes_refuses_and_spends_no_request(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _seed(kb, retracted=False)
        client = fake(FakeClient())
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937", "--target", str(kb)])
        assert rc == 1
        assert client.call_count == 0
        assert "refusing to acknowledge without a terminal" in capsys.readouterr().err

    def test_no_ledger_is_pointed_at_backfill_before_any_request(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _seed_front_matter_only(kb)  # front matter, no sidecar
        client = fake(FakeClient())
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 1
        assert client.call_count == 0  # refused BEFORE the fetch
        err = capsys.readouterr().err
        assert "no PubMed provenance ledger carries id" in err
        assert "pubmed-backfill-provenance" in err
        assert "nothing written" in err

    def test_a_paper_not_in_the_kb_is_refused_before_any_request(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)  # empty KB
        client = fake(FakeClient())
        rc = run(["pubmed-acknowledge-retraction", "--id", "99999",
                  "--target", str(kb), "--yes"])
        assert rc == 1
        assert client.call_count == 0
        assert "pubmed-backfill-provenance" in capsys.readouterr().err

    def test_unreadable_ledger_refused_before_the_request(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _seed(kb, "32738937", retracted=False)
        # A second, corrupt sidecar that might carry the id — its value is unknown.
        bad = _seed(kb, "22222", name="bad")
        sidecar_path(bad, kb).write_text("{ not json", encoding="utf-8")
        client = fake(FakeClient())
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 1
        assert client.call_count == 0
        assert "cannot read every provenance ledger" in capsys.readouterr().err

    def test_invalid_pmid_is_rejected_before_anything(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        client = fake(FakeClient())
        rc = run(["pubmed-acknowledge-retraction", "--id", "0",
                  "--target", str(kb), "--yes"])
        assert rc == 1
        assert client.call_count == 0
        assert "invalid PMID" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# #106 — --yes may record a retraction, never clear one
# --------------------------------------------------------------------------- #
class TestClearIsGatedOnAHuman:
    def test_yes_cannot_clear_a_recorded_retraction(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        md = _seed(kb, retracted=True)  # ledger records a retraction
        before = _stat(sidecar_path(md, kb))
        # PubMed no longer reads as retracted.
        client = fake(FakeClient(_set(_plain_record("32738937"))))
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "refusing to clear the retraction recorded for 32738937 with --yes" in err
        # The recorded retraction is left exactly as it was.
        assert _stat(sidecar_path(md, kb)) == before
        assert _ledger(md, kb)[("pubmed", "32738937")]["retracted"] is True

    def test_interactive_clear_removes_the_field(self, tmp_path, fake, capsys, monkeypatch):
        kb = _kb(tmp_path)
        md = _seed(kb, retracted=True)
        client = fake(FakeClient(_set(_plain_record("32738937"))))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "y")
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937", "--target", str(kb)])
        assert rc == 0
        # Cleared by REMOVING the field, never a literal False.
        assert "retracted" not in _ledger(md, kb)[("pubmed", "32738937")]
        assert "Cleared the retraction recorded for 32738937" in capsys.readouterr().out

    def test_interactive_decline_writes_nothing(self, tmp_path, fake, capsys, monkeypatch):
        kb = _kb(tmp_path)
        md = _seed(kb, retracted=False)
        before = _stat(sidecar_path(md, kb))
        client = fake(FakeClient(_set(_retracted_record("32738937"))))
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a, **k: "n")
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937", "--target", str(kb)])
        assert rc == 0
        assert "Aborted; nothing written." in capsys.readouterr().out
        assert _stat(sidecar_path(md, kb)) == before


# --------------------------------------------------------------------------- #
# a failed / deleted / merged query writes nothing
# --------------------------------------------------------------------------- #
class TestQueryFailures:
    def test_connection_error_is_exit_2_nothing_written(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        md = _seed(kb, retracted=False)
        before = _stat(sidecar_path(md, kb))
        fake(FakeClient(raise_exc=PubMedConnectionError("boom")))
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 2
        assert "Nothing written" in capsys.readouterr().err
        assert _stat(sidecar_path(md, kb)) == before

    def test_service_error_is_exit_1_nothing_written(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _seed(kb, retracted=False)
        fake(FakeClient(raise_exc=PubMedError("bad request")))
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 1
        assert "Nothing written" in capsys.readouterr().err

    def test_deleted_pmid_writes_nothing(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _seed(kb, "32738937", retracted=False)
        fake(FakeClient(_set()))  # empty set == deleted/gone
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 1
        assert "deleted" in capsys.readouterr().err

    def test_merged_pmid_is_refused_under_the_old_key(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _seed(kb, "32738937", retracted=False)
        # Asked for 32738937, PubMed answers under a different id -> a merge.
        fake(FakeClient(_set(_retracted_record("99999"))))
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 1
        err = capsys.readouterr().err
        assert "merged" in err and "re-import" in err


# --------------------------------------------------------------------------- #
# nothing-to-acknowledge (ledger already matches PubMed)
# --------------------------------------------------------------------------- #
class TestNothingToAcknowledge:
    def test_not_retracted_and_no_record_is_a_clean_zero(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        md = _seed(kb, retracted=False)
        before = _stat(sidecar_path(md, kb))
        client = fake(FakeClient(_set(_plain_record("32738937"))))
        rc = run(["pubmed-acknowledge-retraction", "--id", "32738937",
                  "--target", str(kb), "--yes"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "does not flag 32738937 as retracted" in out
        assert _stat(sidecar_path(md, kb)) == before
