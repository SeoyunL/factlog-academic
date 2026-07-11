# SPDX-License-Identifier: Apache-2.0
"""`factlog pubmed-refresh` — report-only retraction drift (issue #168).

The real PubMed client is replaced via `_make_pubmed_client` so the command runs without
the network; the fake replays canned efetch XML keyed by requested PMID. A temp KB carries
source `.md` originals and their provenance ledgers; the tests assert the originals and
ledgers stay byte- and `mtime_ns`-identical (report-only writes nothing but the check-log),
that a newly-reported retraction is surfaced and never written, that a reversal surfaces,
that `--older-than` reads only the check-log, that `--only-flagged` re-checks only flagged
records, that a nested `sources/` subtree is walked, that a front-matter-only paper points
at `pubmed-backfill-provenance` (#110), and that the time estimate reflects the configured
rate with/without a key.
"""
from __future__ import annotations

import json

import pytest

from factlog import cli
from factlog.integrations.common.provenance import (
    Provenance,
    SourceRecord,
    read_provenance,
    sidecar_path,
    write_provenance,
)
from factlog.integrations.pubmed import refresh as rf
from factlog.integrations.pubmed.check_log import (
    CheckLog,
    CheckRecord,
    check_log_path,
    read_check_log,
    record_check,
    write_check_log,
)
from factlog.integrations.pubmed.client import (
    PubMedClient,
    PubMedConnectionError,
    PubMedServiceError,
)

IMPORTED_AT = "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _kb(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "policy").mkdir()
    (tmp_path / "policy" / "pubmed-config.toml").write_text(
        '[client]\nemail = "test@example.com"\n', encoding="utf-8"
    )
    return tmp_path


def _record_xml(pmid, *, retracted=False, notice_pmid=None):
    """One <PubmedArticle> efetch record, optionally carrying retraction markers."""
    pub_types = ""
    comments = ""
    if retracted:
        pub_types = (
            '<PublicationTypeList>'
            '<PublicationType UI="D016441">Retracted Publication</PublicationType>'
            '</PublicationTypeList>'
        )
        notice = f"<PMID>{notice_pmid}</PMID>" if notice_pmid else ""
        comments = (
            '<CommentsCorrectionsList>'
            f'<CommentsCorrections RefType="RetractionIn">{notice}</CommentsCorrections>'
            '</CommentsCorrectionsList>'
        )
    return f"""
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <Journal><Title>J</Title>
          <JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>Paper {pmid}</ArticleTitle>
        {pub_types}
      </Article>
      {comments}
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">{pmid}</ArticleId>
    </ArticleIdList></PubmedData>
  </PubmedArticle>"""


def _set(*records):
    return "<PubmedArticleSet>" + "".join(records) + "</PubmedArticleSet>"


class FakeClient:
    """Replays a canned efetch body per requested PMID; records the ids asked for.

    ``bodies`` maps a pmid to the raw efetch XML returned for it. An unknown pmid yields an
    empty `<PubmedArticleSet/>` (the deleted signal). ``raise_exc`` forces a transport
    failure on the next call.
    """

    def __init__(self, bodies=None, *, raise_exc=None):
        self._bodies = bodies or {}
        self._raise = raise_exc
        self.calls: list[list[str]] = []

    def efetch(self, pmids):
        ids = [str(p) for p in pmids]
        self.calls.append(ids)
        if self._raise is not None:
            raise self._raise
        return self._bodies.get(ids[0], _set())


@pytest.fixture
def fake(monkeypatch):
    def install(client):
        monkeypatch.setattr(cli, "_make_pubmed_client", lambda config: client)
        return client
    return install


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def _add_paper(kb, name, pmid, *, retracted=None, notice_pmid=None, sub="", ledger=True):
    """Write a source .md (front matter) and, when ``ledger``, its PubMed sidecar.

    ``retracted`` records the ledger/front-matter retraction status (None -> absent).
    """
    src_dir = kb / "sources"
    if sub:
        src_dir = src_dir / sub
        src_dir.mkdir(parents=True, exist_ok=True)
    fm = [f"pmid: {pmid}", "imported_from: pubmed"]
    if retracted and not ledger:
        fm.append("pubmed_retracted: true")
        if notice_pmid:
            fm.append(f"pubmed_retraction_notice_pmid: {notice_pmid}")
    md = src_dir / f"{name}.md"
    md.write_text("---\n" + "\n".join(fm) + "\n---\n\n# Paper\n", encoding="utf-8")
    if ledger:
        fields: dict = {"journal": "J"}
        if retracted:
            fields["retracted"] = True
            if notice_pmid:
                fields["retraction_notice_pmid"] = notice_pmid
        rec = SourceRecord(type="pubmed", id=pmid, imported_at=IMPORTED_AT, fields=fields)
        write_provenance(sidecar_path(md, kb), Provenance(records=[rec]))
    return md


def _snapshot(kb):
    """(path -> (bytes, mtime_ns)) for every file under sources/ and source-provenance/."""
    snap = {}
    for root in ("sources", "source-provenance"):
        base = kb / root
        if base.is_dir():
            for p in base.rglob("*"):
                if p.is_file():
                    snap[p] = (p.read_bytes(), p.stat().st_mtime_ns)
    return snap


# --------------------------------------------------------------------------- #
# module-level: collection, diff, freshness, only-flagged (#121 unchanged def)
# --------------------------------------------------------------------------- #
class TestCollect:
    def test_ledger_record_is_collected_with_recorded_status(self, tmp_path):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", retracted=True, notice_pmid="999")
        _add_paper(kb, "b", "222")
        entries, errors = rf.collect_ledger_entries(kb)
        assert errors == []
        by_pmid = {e.pmid: e for e in entries}
        assert by_pmid["111"].recorded_retracted is True
        assert by_pmid["111"].recorded_notice_pmid == "999"
        assert by_pmid["222"].recorded_retracted is False

    def test_nested_sources_subtree_is_walked(self, tmp_path):
        kb = _kb(tmp_path)
        _add_paper(kb, "deep", "333", sub="2020/journal")
        entries, _ = rf.collect_ledger_entries(kb)
        assert [e.pmid for e in entries] == ["333"]
        assert entries[0].sources == ("source-provenance/2020/journal/deep.json",)

    def test_front_matter_only_paper_is_collected(self, tmp_path):
        kb = _kb(tmp_path)
        _add_paper(kb, "fm", "444", retracted=True, ledger=False)
        entries, _ = rf.collect_ledger_entries(kb)
        assert entries[0].pmid == "444"
        assert entries[0].recorded_retracted is True
        assert rf.provenance_of(entries[0].sources) == "front-matter"

    def test_corrupt_ledger_is_a_per_id_error_not_a_crash(self, tmp_path):
        kb = _kb(tmp_path)
        _add_paper(kb, "ok", "111")
        bad = kb / "source-provenance" / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        entries, errors = rf.collect_ledger_entries(kb)
        assert [e.pmid for e in entries] == ["111"]
        assert len(errors) == 1 and errors[0].status == rf.STATUS_ERROR


class TestUnchangedDefinition:
    """#121: 'unchanged' means the same thing to the report and to a future writer."""

    def test_recorded_true_and_live_true_is_unchanged(self):
        entry = rf.LedgerEntry(pmid="1", recorded_retracted=True)
        work = _Work(retracted=True)
        check = rf._diff(entry, work)
        assert check.status == rf.STATUS_UNCHANGED
        assert not check.newly_retracted and not check.un_retracted

    def test_recorded_false_and_live_true_is_newly_retracted(self):
        check = rf._diff(rf.LedgerEntry(pmid="1"), _Work(retracted=True))
        assert check.status == rf.STATUS_CHANGED and check.newly_retracted

    def test_recorded_true_and_live_false_is_un_retracted(self):
        entry = rf.LedgerEntry(pmid="1", recorded_retracted=True)
        check = rf._diff(entry, _Work(retracted=False))
        assert check.status == rf.STATUS_CHANGED and check.un_retracted


class _Work:
    def __init__(self, *, retracted=False, notice=None):
        self.retracted = retracted
        self.retraction_notice_pmid = notice


class TestFreshnessAndFlagged:
    def test_recent_check_is_skipped_older_forces_recheck(self):
        entries = [rf.LedgerEntry(pmid="1")]
        log = CheckLog(entries={"1": CheckRecord(last_checked_at="2026-07-01T00:00:00+00:00")})
        now = _dt("2026-07-10T00:00:00+00:00")
        to_check, skipped = rf.partition_by_freshness(entries, log, 30.0, now)
        assert not to_check and [s.pmid for s in skipped] == ["1"]
        to_check, skipped = rf.partition_by_freshness(entries, log, 0.0, now)
        assert [e.pmid for e in to_check] == ["1"] and not skipped

    def test_only_flagged_keeps_recorded_retractions(self):
        entries = [
            rf.LedgerEntry(pmid="1", recorded_retracted=True),
            rf.LedgerEntry(pmid="2", recorded_retracted=False),
        ]
        assert [e.pmid for e in rf.flagged_only(entries)] == ["1"]


def _dt(iso):
    from datetime import datetime

    return datetime.fromisoformat(iso)


class TestEstimate:
    def test_no_key_line_shows_the_keyed_alternative(self):
        lines = rf.estimate_lines(
            100,
            interval=PubMedClient.min_interval(has_api_key=False),
            keyed_interval=PubMedClient.min_interval(has_api_key=True),
            has_key=False,
        )
        assert lines[0] == "Refreshing retraction status for 100 PMID(s)..."
        # 100 * 0.34 = 34s ; 100 * 0.10 = 10s
        assert lines[1] == "Estimated time: ~34s (would be ~10s with an NCBI API key)"

    def test_keyed_run_omits_the_alternative(self):
        lines = rf.estimate_lines(
            100,
            interval=PubMedClient.min_interval(has_api_key=True),
            keyed_interval=PubMedClient.min_interval(has_api_key=True),
            has_key=True,
        )
        assert lines[1] == "Estimated time: ~10s"

    def test_interval_is_derived_from_the_client_not_a_copy(self):
        assert PubMedClient(None).request_interval == PubMedClient.min_interval(has_api_key=False)
        assert rf.format_eta(0, 0.34) == "~0s"
        assert rf.format_eta(200, 0.34) == "~1m08s"


# --------------------------------------------------------------------------- #
# CLI: report-only, writes nothing but the check-log
# --------------------------------------------------------------------------- #
class TestReportOnly:
    def test_newly_retracted_is_reported_and_nothing_is_written(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")  # ledger records not-retracted
        before = _snapshot(kb)
        fake(FakeClient({"111": _set(_record_xml("111", retracted=True, notice_pmid="999"))}))

        rc = run(["pubmed-refresh", "--target", str(kb)])

        out = capsys.readouterr().out
        assert rc == 0  # a retraction is news, not an error
        assert "RETRACTED" in out and "111" in out
        assert "retraction notice (PMID 999)" in out
        # sources/ and the ledger are byte- and mtime-identical: report-only wrote nothing.
        assert _snapshot(kb) == before
        # the ledger still records NO retraction (never absorbed)
        rec = read_provenance(sidecar_path(kb / "sources" / "a.md", kb)).records[0]
        assert "retracted" not in rec.fields
        # the check-log DID advance
        log = read_check_log(check_log_path(kb))
        assert "111" in log.entries

    def test_unchanged_retraction_is_not_reported(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", retracted=True)  # ledger already records retracted
        fake(FakeClient({"111": _set(_record_xml("111", retracted=True))}))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Newly reported as retracted" not in out
        assert "Up to date:           1" in out

    def test_reversed_retraction_is_reported(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", retracted=True)
        fake(FakeClient({"111": _set(_record_xml("111", retracted=False))}))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "No longer reported as retracted" in out

    def test_front_matter_only_retraction_points_to_backfill(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "fm", "444", ledger=False)  # front matter only, no ledger
        fake(FakeClient({"444": _set(_record_xml("444", retracted=True))}))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "pubmed-backfill-provenance" in out
        # still nothing written under source-provenance/ (no ledger fabricated)
        assert not (kb / "source-provenance").exists()

    def test_older_than_skips_recent_and_does_not_fetch(self, tmp_path, fake):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        # seed a fresh check
        log = CheckLog()
        from datetime import datetime, timezone

        record_check(log, "111", datetime.now(timezone.utc).isoformat())
        write_check_log(check_log_path(kb), log)
        client = fake(FakeClient({"111": _set(_record_xml("111"))}))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        assert rc == 0
        assert client.calls == []  # skipped: never fetched

    def test_only_flagged_rechecks_only_flagged_records(self, tmp_path, fake):
        kb = _kb(tmp_path)
        _add_paper(kb, "flagged", "111", retracted=True)
        _add_paper(kb, "clean", "222")
        client = fake(FakeClient({
            "111": _set(_record_xml("111", retracted=True)),
            "222": _set(_record_xml("222")),
        }))
        rc = run(["pubmed-refresh", "--target", str(kb), "--only-flagged"])
        assert rc == 0
        assert client.calls == [["111"]]  # only the flagged pmid was fetched

    def test_dry_run_hits_no_network_and_writes_nothing(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        before = _snapshot(kb)
        client = fake(FakeClient({}, raise_exc=AssertionError("should not fetch")))
        rc = run(["pubmed-refresh", "--target", str(kb), "--dry-run"])
        err = capsys.readouterr().err
        assert rc == 0
        assert client.calls == []
        assert "Estimated time:" in err
        assert _snapshot(kb) == before
        assert not check_log_path(kb).exists()

    def test_deleted_pmid_is_a_per_id_error_exit_1(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        _add_paper(kb, "b", "222")
        # 111 answers, 222 comes back empty (deleted) -> per-id error
        fake(FakeClient({"111": _set(_record_xml("111"))}))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 1  # an unresolvable id is an error
        assert "Could not check" in out and "222" in out
        # the record that DID answer still advanced the check-log; the errored one did not
        log = read_check_log(check_log_path(kb))
        assert "111" in log.entries and "222" not in log.entries

    def test_connection_failure_aborts_without_writing_checklog(self, tmp_path, fake):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        fake(FakeClient({}, raise_exc=PubMedConnectionError("down")))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        assert rc == 2
        assert not check_log_path(kb).exists()

    def test_porcelain_rows(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        fake(FakeClient({"111": _set(_record_xml("111", retracted=True))}))
        rc = run(["pubmed-refresh", "--target", str(kb), "--porcelain"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "check\t111\tchanged\t1\t0\t" in out
        assert "retracted\t1" in out
        assert f"target\t{kb}" in out


class TestEmailPolicy:
    def test_missing_contact_email_refuses_before_any_request(self, tmp_path, fake, monkeypatch):
        # A refresh hits the network, so NCBI's contact-email policy applies. The command
        # checks it inline (not relying on the shared resolver), so it refuses regardless.
        kb = tmp_path
        (kb / "sources").mkdir()
        (kb / "policy").mkdir()
        (kb / "policy" / "pubmed-config.toml").write_text("[client]\n", encoding="utf-8")
        monkeypatch.delenv("NCBI_API_KEY", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(kb / "empty-xdg"))
        _add_paper(kb, "a", "111")
        client = fake(FakeClient({}, raise_exc=AssertionError("should not fetch")))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        assert rc == 1
        assert client.calls == []


class TestConfirmation:
    def test_interactive_no_aborts_and_writes_nothing(self, tmp_path, fake, monkeypatch, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        client = fake(FakeClient({"111": _set(_record_xml("111"))}))
        monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda *a: "n")
        rc = run(["pubmed-refresh", "--target", str(kb)])
        err = capsys.readouterr().err
        assert rc == 0
        assert client.calls == []  # declined before any fetch
        assert "aborted" in err
        assert not check_log_path(kb).exists()
