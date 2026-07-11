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


def _record_xml(pmid, *, retracted=False, notice_pmid=None, journal="J", doi=None):
    """One <PubmedArticle> efetch record, optionally carrying retraction markers.

    ``journal`` and ``doi`` set the identifier/journal fields a refresh compares (and
    ``--auto-update`` may write); ``doi=None`` emits no DOI at all (the common shape).
    """
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
    doi_id = f'<ArticleId IdType="doi">{doi}</ArticleId>' if doi else ""
    return f"""
  <PubmedArticle>
    <MedlineCitation>
      <PMID>{pmid}</PMID>
      <Article>
        <Journal><Title>{journal}</Title>
          <JournalIssue><PubDate><Year>2020</Year></PubDate></JournalIssue></Journal>
        <ArticleTitle>Paper {pmid}</ArticleTitle>
        {pub_types}
      </Article>
      {comments}
    </MedlineCitation>
    <PubmedData><ArticleIdList>
      <ArticleId IdType="pubmed">{pmid}</ArticleId>
      {doi_id}
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


def _add_paper(
    kb, name, pmid, *, retracted=None, notice_pmid=None, sub="", ledger=True,
    journal="J", doi=None, extra_fields=None,
):
    """Write a source .md (front matter) and, when ``ledger``, its PubMed sidecar.

    ``retracted`` records the ledger/front-matter retraction status (None -> absent).
    ``journal``/``doi`` are the identifier/journal fields a refresh compares; ``doi=None``
    records no DOI. ``extra_fields`` seeds additional ledger fields (e.g. mesh) so a test
    can assert ``--auto-update`` leaves them untouched.
    """
    src_dir = kb / "sources"
    if sub:
        src_dir = src_dir / sub
        src_dir.mkdir(parents=True, exist_ok=True)
    fm = [f"pmid: {pmid}", "imported_from: pubmed"]
    if journal:
        fm.append(f"journal: {journal}")
    if doi:
        fm.append(f"doi: {doi}")
    if retracted and not ledger:
        fm.append("pubmed_retracted: true")
        if notice_pmid:
            fm.append(f"pubmed_retraction_notice_pmid: {notice_pmid}")
    md = src_dir / f"{name}.md"
    md.write_text("---\n" + "\n".join(fm) + "\n---\n\n# Paper\n", encoding="utf-8")
    if ledger:
        fields: dict = {}
        if journal:
            fields["journal"] = journal
        if doi:
            fields["doi"] = doi
        if extra_fields:
            fields.update(extra_fields)
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
    def __init__(self, *, retracted=False, notice=None, doi=None, journal=None):
        self.retracted = retracted
        self.retraction_notice_pmid = notice
        self.doi = doi
        self.journal = journal


class TestMergedDeletedClassification:
    """#170: check_entries consumes the parser's four signals; neither merged nor deleted
    writes, and an unparseable record is an error, not a deletion."""

    def test_merged_is_status_merged_with_both_ids(self):
        client = FakeClient({"111": _set(_record_xml("999"))})
        [result] = rf.check_entries([rf.LedgerEntry(pmid="111")], client)
        assert result.status == rf.STATUS_MERGED
        assert result.pmid == "111"  # requested id kept
        assert result.returned_pmid == "999"  # survivor id exposed
        assert "merged into PMID 999" in result.reason

    def test_deleted_is_status_deleted(self):
        client = FakeClient({})  # empty response for 111
        [result] = rf.check_entries([rf.LedgerEntry(pmid="111")], client)
        assert result.status == rf.STATUS_DELETED
        assert result.pmid == "111"
        assert result.returned_pmid is None

    def test_present_is_still_diffed(self):
        client = FakeClient({"111": _set(_record_xml("111", retracted=True))})
        [result] = rf.check_entries([rf.LedgerEntry(pmid="111")], client)
        assert result.status == rf.STATUS_CHANGED and result.newly_retracted

    def test_unparseable_record_is_error_not_deleted(self):
        # A record came back for the request but has no <PMID>: it lands in the parser's
        # `unparseable` bucket AND the requested id lands in `deleted` (nothing matched).
        # This must be a per-id ERROR, never a false "deleted upstream".
        body = "<PubmedArticleSet><PubmedArticle><MedlineCitation></MedlineCitation></PubmedArticle></PubmedArticleSet>"
        client = FakeClient({"111": body})
        [result] = rf.check_entries([rf.LedgerEntry(pmid="111")], client)
        assert result.status == rf.STATUS_ERROR
        assert result.status != rf.STATUS_DELETED
        assert "could not be parsed" in result.reason and "not a deletion" in result.reason

    def test_deleted_network_unparseable_are_three_distinct_states(self):
        empty = FakeClient({})  # deleted: empty well-formed response
        [deleted] = rf.check_entries([rf.LedgerEntry(pmid="111")], empty)
        assert deleted.status == rf.STATUS_DELETED

        bad = FakeClient({"111": "<PubmedArticleSet><PubmedArticle><MedlineCitation></MedlineCitation></PubmedArticle></PubmedArticleSet>"})
        [unparse] = rf.check_entries([rf.LedgerEntry(pmid="111")], bad)
        assert unparse.status == rf.STATUS_ERROR

        down = FakeClient({}, raise_exc=PubMedConnectionError("down"))
        # a network failure PROPAGATES (never becomes a per-id status at all)
        with pytest.raises(PubMedConnectionError):
            rf.check_entries([rf.LedgerEntry(pmid="111")], down)

    def test_merged_note_names_both_ids_and_offers_only(self):
        result = rf.RefreshCheck(pmid="111", status=rf.STATUS_MERGED, returned_pmid="999")
        note = rf.merged_note(result)
        assert "111" in note and "999" in note
        assert "NOT rewritten" in note and "human decision" in note

    def test_deleted_note_keeps_the_entry(self):
        result = rf.RefreshCheck(pmid="111", status=rf.STATUS_DELETED)
        note = rf.deleted_note(result)
        assert "deleted upstream" in note and "kept, not dropped" in note

    def test_summary_counts_merged_and_deleted_separately(self):
        results = [
            rf.RefreshCheck(pmid="1", status=rf.STATUS_MERGED, returned_pmid="9"),
            rf.RefreshCheck(pmid="2", status=rf.STATUS_DELETED),
            rf.RefreshCheck(pmid="3", status=rf.STATUS_UNCHANGED),
        ]
        summary = rf.summarize(results, [])
        assert summary.merged == 1 and summary.deleted == 1
        assert summary.checked == 1  # merged/deleted excluded from "checked"


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

    def test_deleted_pmid_is_flagged_not_dropped_exit_1(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        _add_paper(kb, "b", "222")
        before = _snapshot(kb)
        # 111 answers, 222 comes back empty (deleted upstream) -> a review flag, not a drop
        fake(FakeClient({"111": _set(_record_xml("111"))}))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 1  # an unresolvable id reaches the exit code (#112 principle)
        assert "Deleted upstream" in out and "222" in out
        assert "the KB entry is kept, not dropped" in out
        assert "Deleted upstream:     1" in out
        # the KB entry for the deleted PMID is UNTOUCHED: sources/*.md and sidecar immutable
        assert _snapshot(kb) == before
        assert (kb / "sources" / "b.md").exists()
        # the record that DID answer advanced the check-log; the deleted one did NOT, so it
        # keeps surfacing every run until a human acts (never silently dropped)
        log = read_check_log(check_log_path(kb))
        assert "111" in log.entries and "222" not in log.entries

    def test_merged_pmid_reports_both_ids_and_never_rewrites_kb(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        before = _snapshot(kb)
        # request 111, PubMed returns the record under 999 (NCBI merged the two)
        fake(FakeClient({"111": _set(_record_xml("999"))}))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 1  # unresolvable under the recorded PMID -> exit code
        assert "Merged upstream" in out
        assert "111" in out and "999" in out  # BOTH ids reported
        assert "is NOT rewritten" in out
        assert "Merged upstream:      1" in out
        # the KB is not silently re-keyed: files byte-identical, ledger keeps original pmid
        assert _snapshot(kb) == before
        rec = read_provenance(sidecar_path(kb / "sources" / "a.md", kb)).records[0]
        assert rec.id == "111"
        # a merged PMID does not advance the check-log either: it keeps surfacing
        log = read_check_log(check_log_path(kb))
        assert "111" not in log.entries

    def test_merged_surfaces_survivor_retraction_when_present(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        # the survivor 999 is itself reported RETRACTED — a fact-checking signal to surface
        fake(FakeClient({"111": _set(_record_xml("999", retracted=True, notice_pmid="777"))}))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "surviving record is currently reported RETRACTED" in out
        assert "777" in out

    def test_merged_porcelain_row_carries_survivor_pmid(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        fake(FakeClient({"111": _set(_record_xml("999"))}))
        rc = run(["pubmed-refresh", "--target", str(kb), "--porcelain"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "check\t111\tmerged\t0\t0\tmerged into PMID 999" in out
        assert "merged\t1" in out

    def test_deleted_porcelain_row(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        fake(FakeClient({}))  # empty response for 111 -> deleted
        rc = run(["pubmed-refresh", "--target", str(kb), "--porcelain"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "check\t111\tdeleted\t0\t0\t" in out
        assert "deleted\t1" in out

    def test_unparseable_response_is_error_not_deleted(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        before = _snapshot(kb)
        # a record comes back but has no <PMID> — parseable-as-XML, unreducible-as-record
        body = "<PubmedArticleSet><PubmedArticle><MedlineCitation></MedlineCitation></PubmedArticle></PubmedArticleSet>"
        fake(FakeClient({"111": body}))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 1
        # reported as a per-id error, NEVER as a deletion (the record did come back)
        assert "Could not check" in out and "111" in out
        assert "not a deletion" in out
        assert "Deleted upstream (flagged for review" not in out
        assert "has been deleted upstream" not in out
        assert "Deleted upstream:     0" in out  # tally shows zero deletions
        # KB untouched and the errored id did NOT advance the check-log
        assert _snapshot(kb) == before
        log = read_check_log(check_log_path(kb))
        assert "111" not in log.entries

    def test_connection_failure_aborts_without_writing_checklog(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111")
        fake(FakeClient({}, raise_exc=PubMedConnectionError("down")))
        rc = run(["pubmed-refresh", "--target", str(kb)])
        out, err = capsys.readouterr()
        assert rc == 2
        assert not check_log_path(kb).exists()
        # #170: a network failure is reported as a network failure, never flagged as a
        # deleted PMID — a flaky connection must not mark a live paper as gone.
        assert "Deleted upstream" not in out
        assert "down" in err

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


# --------------------------------------------------------------------------- #
# --auto-update (#169): writes only the narrow identifier/journal fields, never
# retraction, never a source .md; unchanged means byte-identical (#121).
# --------------------------------------------------------------------------- #
DOI = "10.1234/new"
OTHER_DOI = "10.9999/other"


class TestAutoUpdateEnumeration:
    """The narrow-field enumeration lives in one place and a test reads it (#169)."""

    def test_enumeration_is_doi_and_journal_only(self):
        assert rf.AUTO_UPDATE_FIELDS == ("doi", "journal")

    def test_enumeration_never_includes_retraction(self):
        # The whole reason --auto-update can exist: it writes transcription corrections,
        # not the human-gate retraction signal. If retraction ever entered the enumeration
        # this assertion is the tripwire.
        for field in ("retracted", "retraction_notice_pmid", "retraction_verified_at"):
            assert field not in rf.AUTO_UPDATE_FIELDS

    def test_writer_moves_exactly_the_enumerated_fields(self, tmp_path):
        # The report's changed_fields and the writer's moved fields both derive from
        # AUTO_UPDATE_FIELDS, so they cannot name different sets.
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", journal="Old", doi=None)
        entries, _ = rf.collect_ledger_entries(kb)
        check = rf._diff(entries[0], _Work(journal="New", doi=DOI))
        assert set(check.changed_fields) == set(rf.AUTO_UPDATE_FIELDS)
        outcomes = rf.apply_auto_update([check], kb)
        assert set(outcomes[0].fields) == set(rf.AUTO_UPDATE_FIELDS)


class TestUnchangedIsByteIdentical:
    """#121: a record the report calls unchanged is one --auto-update leaves byte-identical."""

    def test_report_unchanged_implies_writer_byte_identical(self, tmp_path):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", journal="J", doi=DOI)
        entries, _ = rf.collect_ledger_entries(kb)
        check = rf._diff(entries[0], _Work(journal="J", doi=DOI))  # live == recorded
        assert check.status == rf.STATUS_UNCHANGED
        before = _snapshot(kb)
        outcomes = rf.apply_auto_update([check], kb)
        assert outcomes[0].status == rf.UPDATE_UNCHANGED
        assert _snapshot(kb) == before  # ledger AND source .md byte- and mtime-identical

    def test_cli_unchanged_record_writes_nothing_but_checklog(self, tmp_path, fake):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", journal="J", doi=DOI)
        before = _snapshot(kb)
        fake(FakeClient({"111": _set(_record_xml("111", journal="J", doi=DOI))}))
        rc = run(["pubmed-refresh", "--target", str(kb), "--auto-update"])
        assert rc == 0
        assert _snapshot(kb) == before  # nothing under sources/ or the ledger moved

    def test_mixed_batch_rewrites_only_the_changed_record(self, tmp_path, fake):
        kb = _kb(tmp_path)
        _add_paper(kb, "same", "111", journal="J", doi=DOI)      # will be unchanged
        _add_paper(kb, "drift", "222", journal="Old", doi=None)  # will drift
        same_side = sidecar_path(kb / "sources" / "same.md", kb)
        same_before = (same_side.read_bytes(), same_side.stat().st_mtime_ns)
        fake(FakeClient({
            "111": _set(_record_xml("111", journal="J", doi=DOI)),
            "222": _set(_record_xml("222", journal="New", doi=DOI)),
        }))
        rc = run(["pubmed-refresh", "--target", str(kb), "--auto-update"])
        assert rc == 0
        # the unchanged record's sidecar is byte- and mtime-identical
        assert (same_side.read_bytes(), same_side.stat().st_mtime_ns) == same_before
        # the drifted record's sidecar recorded the new identifier/journal
        rec = read_provenance(sidecar_path(kb / "sources" / "drift.md", kb)).records[0]
        assert rec.fields["journal"] == "New" and rec.fields["doi"] == DOI


class TestAutoUpdateWrites:
    def test_writes_doi_and_journal_into_the_ledger(self, tmp_path, fake):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", journal="Old", doi=None)
        md = kb / "sources" / "a.md"
        md_before = (md.read_bytes(), md.stat().st_mtime_ns)
        fake(FakeClient({"111": _set(_record_xml("111", journal="New", doi=DOI))}))
        rc = run(["pubmed-refresh", "--target", str(kb), "--auto-update"])
        assert rc == 0
        rec = read_provenance(sidecar_path(md, kb)).records[0]
        assert rec.fields["journal"] == "New"
        assert rec.fields["doi"] == DOI
        assert rec.imported_at == IMPORTED_AT  # provenance clock preserved
        # P4: the original .md is never opened — byte- and mtime_ns-identical.
        assert (md.read_bytes(), md.stat().st_mtime_ns) == md_before

    def test_leaves_unenumerated_fields_untouched(self, tmp_path, fake):
        kb = _kb(tmp_path)
        _add_paper(
            kb, "a", "111", journal="Old",
            extra_fields={"pubmed_mesh_major": ["Neoplasms"]},
        )
        fake(FakeClient({"111": _set(_record_xml("111", journal="New"))}))
        rc = run(["pubmed-refresh", "--target", str(kb), "--auto-update"])
        assert rc == 0
        rec = read_provenance(sidecar_path(kb / "sources" / "a.md", kb)).records[0]
        assert rec.fields["journal"] == "New"
        assert rec.fields["pubmed_mesh_major"] == ["Neoplasms"]  # not an identifier: untouched

    def test_porcelain_emits_update_rows_and_changed_fields_column(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", journal="Old", doi=None)
        fake(FakeClient({"111": _set(_record_xml("111", journal="New", doi=DOI))}))
        rc = run(["pubmed-refresh", "--target", str(kb), "--auto-update", "--porcelain"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "update\t111\tupdated\t" in out
        assert "updated\t1" in out
        assert "doi,journal" in out  # the check row's changed_fields column


class TestAutoUpdateNeverWritesRetraction:
    """--auto-update is not an acknowledgement (#169): retraction is copied through verbatim."""

    def test_newly_retracted_record_keeps_no_retracted_field_and_is_reported(
        self, tmp_path, fake, capsys
    ):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", journal="J", doi=None)  # ledger records NOT retracted
        # PubMed now reports a retraction AND a new DOI: the DOI is written, retraction is not.
        fake(FakeClient({
            "111": _set(_record_xml("111", retracted=True, notice_pmid="999", journal="J", doi=DOI))
        }))
        rc = run(["pubmed-refresh", "--target", str(kb), "--auto-update"])
        out = capsys.readouterr().out
        assert rc == 0  # a retraction is news, not an error
        assert "RETRACTED" in out and "111" in out  # surfaced for a human
        rec = read_provenance(sidecar_path(kb / "sources" / "a.md", kb)).records[0]
        assert rec.fields["doi"] == DOI          # the transcription correction was written
        assert "retracted" not in rec.fields     # the human-gate signal was NOT

    def test_no_source_md_mtime_changes_over_a_newly_retracted_kb(self, tmp_path, fake):
        # Done-when: no sources/*.md mtime_ns changes across an --auto-update run over a KB
        # with a newly retracted paper (even one whose identifier/journal also drifted).
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", journal="Old", doi=None, sub="2020")
        _add_paper(kb, "b", "222", journal="J", doi=DOI)
        md_before = {
            p: p.stat().st_mtime_ns
            for p in (kb / "sources").rglob("*.md")
        }
        fake(FakeClient({
            "111": _set(_record_xml("111", retracted=True, journal="New", doi=DOI)),
            "222": _set(_record_xml("222", journal="J", doi=DOI)),
        }))
        rc = run(["pubmed-refresh", "--target", str(kb), "--auto-update"])
        assert rc == 0
        md_after = {p: p.stat().st_mtime_ns for p in (kb / "sources").rglob("*.md")}
        assert md_after == md_before  # not one original .md was touched


class TestAutoUpdateNoLedger:
    def test_front_matter_only_record_gets_no_ledger(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "fm", "444", ledger=False, journal="Old")
        fake(FakeClient({"444": _set(_record_xml("444", journal="New", doi=DOI))}))
        rc = run(["pubmed-refresh", "--target", str(kb), "--auto-update"])
        out = capsys.readouterr().out
        assert rc == 0
        assert not (kb / "source-provenance").exists()  # no ledger fabricated
        assert "pubmed-backfill-provenance" in out


class TestAutoUpdateFailureIsolation:
    def test_write_error_is_a_per_id_error_not_a_batch_crash(self, tmp_path, monkeypatch):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", journal="Old", doi=None)
        check = rf.RefreshCheck(
            pmid="111",
            status=rf.STATUS_CHANGED,
            recorded_journal="Old",
            current_journal="New",
            current_doi=DOI,
            changed_fields=("doi", "journal"),
            sources=("source-provenance/a.json",),
        )

        def _boom(*_a, **_k):
            raise OSError("read-only ledger")

        monkeypatch.setattr(rf, "write_provenance", _boom)
        outcomes = rf.apply_auto_update([check], kb)
        assert outcomes[0].status == rf.UPDATE_ERROR
        assert "read-only ledger" in outcomes[0].reason

    def test_error_result_is_skipped_by_auto_update(self, tmp_path):
        # An error result has nothing confirmed: --auto-update writes nothing for it.
        kb = _kb(tmp_path)
        err = rf.RefreshCheck(pmid="111", status=rf.STATUS_ERROR, reason="unparseable")
        assert rf.apply_auto_update([err], kb) == []

    def test_merged_and_deleted_results_are_skipped_by_auto_update(self, tmp_path):
        # #170: a merged PMID is surface-only — --auto-update NEVER re-keys it (a re-key is a
        # human's P1 decision). A deleted PMID is gone. Both are skipped entirely, even when
        # a ledger sidecar exists for the record.
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", journal="J", doi=DOI)
        merged = rf.RefreshCheck(
            pmid="111", status=rf.STATUS_MERGED, returned_pmid="999",
            recorded_journal="J", recorded_doi=DOI,
            sources=("source-provenance/a.json",),
        )
        deleted = rf.RefreshCheck(
            pmid="111", status=rf.STATUS_DELETED,
            recorded_journal="J", recorded_doi=DOI,
            sources=("source-provenance/a.json",),
        )
        assert rf.apply_auto_update([merged], kb) == []
        assert rf.apply_auto_update([deleted], kb) == []


class TestAutoUpdateMergedSurfaceOnly:
    """#170 decision, wired to #169's flag: --auto-update surfaces a merged PMID (offer),
    never follows it. Neither the recorded PMID nor the ledger is rewritten."""

    def test_merged_under_auto_update_offers_only_and_writes_nothing(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", journal="J", doi=DOI)
        before = _snapshot(kb)
        # request 111, PubMed returns the record under 999, and we pass --auto-update
        fake(FakeClient({"111": _set(_record_xml("999", journal="NewJournal", doi=OTHER_DOI))}))
        rc = run(["pubmed-refresh", "--target", str(kb), "--auto-update"])
        out = capsys.readouterr().out
        assert rc == 1
        # offered, not followed: both ids surfaced, and NO ledger write happened even under
        # --auto-update (the survivor's journal/doi are NOT written to 111's record)
        assert "Merged upstream" in out and "999" in out
        assert "Ledger updated" not in out
        assert _snapshot(kb) == before  # sources/*.md AND sidecar byte-identical
        rec = read_provenance(sidecar_path(kb / "sources" / "a.md", kb)).records[0]
        assert rec.id == "111"  # PMID unchanged
        assert rec.fields.get("journal") == "J"  # survivor's journal NOT absorbed
        assert rec.fields.get("doi") == DOI  # survivor's doi NOT absorbed

    def test_deleted_under_auto_update_writes_nothing(self, tmp_path, fake, capsys):
        kb = _kb(tmp_path)
        _add_paper(kb, "a", "111", journal="J", doi=DOI)
        before = _snapshot(kb)
        fake(FakeClient({}))  # empty -> deleted
        rc = run(["pubmed-refresh", "--target", str(kb), "--auto-update"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "Deleted upstream" in out
        assert _snapshot(kb) == before  # KB entry kept, nothing written
