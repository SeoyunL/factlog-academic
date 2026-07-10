# SPDX-License-Identifier: Apache-2.0
"""`factlog openalex-refresh` — report-only drift + narrow --auto-update (issue #83).

The real OpenAlex client is replaced via `_make_openalex_client` so the command runs
without the network. A temp KB carries source `.md` originals and their provenance
ledgers; the tests assert the originals and ledgers stay byte- and `mtime_ns`-identical,
that a newly-set retraction is surfaced under both modes and never written, that a
superseded id is reported and never followed, that `--auto-update` moves exactly
doi/work_type/journal, that a NotFound / a corrupt-or-unwritable ledger is a per-id
error (guarding BOTH the read and the write), and that `--older-than` reads only the
check-log.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from factlog import cli
from factlog.integrations.common.provenance import (
    Provenance,
    SourceRecord,
    read_provenance,
    sidecar_path,
    write_provenance,
)
from factlog.integrations.openalex import refresh as rf
from factlog.integrations.openalex.api_client import (
    OpenAlexConnectionError,
    OpenAlexNotFoundError,
)
from factlog.integrations.openalex.check_log import (
    CheckLog,
    CheckRecord,
    check_log_path,
    read_check_log,
    write_check_log,
)

IMPORTED_AT = "2026-01-01T00:00:00+00:00"


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _raw_work(oid="W123", *, doi=None, journal=None, work_type="article",
              is_retracted=False, cited_by_count=None):
    """A raw /works payload parse_work reduces to the fields a refresh compares."""
    raw: dict = {
        "id": f"https://openalex.org/{oid}",
        "type": work_type,
        "is_retracted": is_retracted,
    }
    if doi is not None:
        raw["doi"] = f"https://doi.org/{doi}"
    if journal is not None:
        raw["primary_location"] = {"source": {"display_name": journal}}
    if cited_by_count is not None:
        raw["cited_by_count"] = cited_by_count
    return raw


def _seed(kb, oid="W123", *, doi=None, work_type="article", journal=None,
          is_retracted=False, name=None, extra_records=()):
    """Write a source .md and its OpenAlex provenance ledger. Returns the .md path."""
    (kb / "sources").mkdir(exist_ok=True)
    name = name or oid
    md = kb / "sources" / f"{name}.md"
    fm = [f"openalex_id: {oid}", f"type: {work_type}"]
    if journal:
        fm.append(f"journal: {journal}")
    if doi:
        fm.append(f"doi: {doi}")
    if is_retracted:
        fm.append("openalex_is_retracted: true")
    md.write_text("---\n" + "\n".join(fm) + "\n---\n# body\n", encoding="utf-8")
    fields = {
        "doi": doi,
        "work_type": work_type,
        "journal": journal,
        "is_retracted": True if is_retracted else None,
    }
    records = [SourceRecord(type="openalex", id=oid, imported_at=IMPORTED_AT, fields=fields),
               *extra_records]
    write_provenance(sidecar_path(md), Provenance(records=records))
    return md


class FakeClient:
    """Maps requested id -> raw work dict. Records every id it was asked for; a listed
    id in `not_found` raises OpenAlexNotFoundError, and `raise_exc` raises unconditionally."""

    def __init__(self, works=None, *, not_found=(), raise_exc=None):
        self._works = dict(works or {})
        self._not_found = set(not_found)
        self._raise = raise_exc
        self.calls: list[str] = []

    def get_work(self, work_id):
        self.calls.append(work_id)
        if self._raise is not None:
            raise self._raise
        if work_id in self._not_found:
            raise OpenAlexNotFoundError(f"no record for {work_id}")
        return self._works[work_id]


@pytest.fixture
def fake(monkeypatch):
    def install(client):
        monkeypatch.setattr(cli, "_make_openalex_client", lambda config: client)
        return client
    return install


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def _snapshot(kb):
    snap = {}
    for sub in ("sources", "source-provenance"):
        root = kb / sub
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file():
                    st = path.stat()
                    snap[path] = (path.read_bytes(), st.st_mtime_ns)
    return snap


def _ledger(md):
    prov = read_provenance(sidecar_path(md))
    return {(r.type, r.id): r.to_dict() for r in prov.records}


def _md_stat(md):
    st = md.stat()
    return (md.read_bytes(), st.st_mtime_ns)


def _run_check(kb, works):
    entries, _ = rf.collect_ledger_entries(kb)
    return rf.check_entries(entries, FakeClient(works))


# --------------------------------------------------------------------------- #
# collection
# --------------------------------------------------------------------------- #
class TestCollect:
    def test_gathers_openalex_records_and_dedups(self, tmp_path):
        _seed(tmp_path, "W1", doi="10.1234/a", journal="J", name="a")
        # A second ledger cites the same work + a non-openalex record.
        _seed(tmp_path, "W1", doi="10.1234/a", journal="J", name="b",
              extra_records=[SourceRecord(type="arxiv", id="1706.03762",
                                          imported_at=IMPORTED_AT, fields={"version": 1})])
        entries, errors = rf.collect_ledger_entries(tmp_path)
        assert errors == []
        assert len(entries) == 1
        e = entries[0]
        assert e.openalex_id == "W1"
        assert (e.recorded_doi, e.recorded_journal, e.recorded_work_type) == ("10.1234/a", "J", "article")
        assert set(e.sources) == {"source-provenance/a.json", "source-provenance/b.json"}

    def test_retraction_is_read_from_the_ledger(self, tmp_path):
        _seed(tmp_path, "W9", is_retracted=True)
        (entry,), _ = rf.collect_ledger_entries(tmp_path)
        assert entry.recorded_is_retracted is True

    def test_corrupt_ledger_is_a_per_id_error_not_a_crash(self, tmp_path):
        _seed(tmp_path, "W1", name="good")
        (tmp_path / "source-provenance" / "bad.json").write_text("{not json")
        entries, errors = rf.collect_ledger_entries(tmp_path)
        assert [e.openalex_id for e in entries] == ["W1"]
        assert len(errors) == 1
        assert errors[0].status == rf.STATUS_ERROR
        assert "corrupt provenance ledger" in errors[0].reason

    def test_front_matter_only_work_is_still_collected(self, tmp_path):
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "old.md").write_text(
            '---\nopenalex_id: "W5"\ntype: "preprint"\ndoi: "10.1234/old"\n'
            'openalex_is_retracted: true\nimported_from: openalex\n---\n# T\n',
            encoding="utf-8")
        entries, errors = rf.collect_ledger_entries(tmp_path)
        assert errors == []
        (e,) = entries
        assert (e.openalex_id, e.recorded_doi, e.recorded_work_type) == ("W5", "10.1234/old", "preprint")
        assert e.recorded_is_retracted is True
        assert e.sources == ("sources/old.md",)

    def test_ledger_wins_over_front_matter(self, tmp_path):
        _seed(tmp_path, "W1", doi="10.1234/ledger", name="a")
        (tmp_path / "sources" / "old.md").write_text(
            '---\nopenalex_id: "W1"\ndoi: "10.1234/fm"\n---\n', encoding="utf-8")
        entries, _ = rf.collect_ledger_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0].recorded_doi == "10.1234/ledger"
        assert entries[0].sources == ("source-provenance/a.json",)


# --------------------------------------------------------------------------- #
# freshness (reads only the check-log)
# --------------------------------------------------------------------------- #
class TestFreshness:
    def _entries(self):
        return [rf.LedgerEntry("W1", recorded_doi="10.1234/a")]

    def test_recently_checked_is_skipped(self):
        now = datetime(2026, 7, 9, tzinfo=timezone.utc)
        log = CheckLog(entries={"W1": CheckRecord((now - timedelta(days=3)).isoformat())})
        to_check, skipped = rf.partition_by_freshness(self._entries(), log, 30, now)
        assert to_check == []
        assert [s.openalex_id for s in skipped] == ["W1"]

    def test_never_checked_is_due(self):
        now = datetime(2026, 7, 9, tzinfo=timezone.utc)
        to_check, skipped = rf.partition_by_freshness(self._entries(), CheckLog(), 30, now)
        assert len(to_check) == 1 and skipped == []

    def test_older_than_zero_forces_recheck(self):
        now = datetime(2026, 7, 9, tzinfo=timezone.utc)
        log = CheckLog(entries={"W1": CheckRecord(now.isoformat())})
        to_check, skipped = rf.partition_by_freshness(self._entries(), log, 0, now)
        assert len(to_check) == 1 and skipped == []


# --------------------------------------------------------------------------- #
# check_entries: diff, retraction, supersede, notfound, cited_by_count
# --------------------------------------------------------------------------- #
class TestCheckEntries:
    def test_doi_journal_type_divergence_is_reported(self, tmp_path):
        _seed(tmp_path, "W1", doi=None, work_type="preprint", journal=None)
        (result,) = _run_check(tmp_path, {
            "W1": _raw_work("W1", doi="10.1234/new", work_type="article", journal="Nature")})
        assert result.status == rf.STATUS_CHANGED
        assert set(result.changed_fields) == {"doi", "work_type", "journal"}
        assert result.current_doi == "10.1234/new"

    def test_unchanged_when_fields_match(self, tmp_path):
        _seed(tmp_path, "W1", doi="10.1234/a", work_type="article", journal="J")
        (result,) = _run_check(tmp_path, {
            "W1": _raw_work("W1", doi="10.1234/a", work_type="article", journal="J")})
        assert result.status == rf.STATUS_UNCHANGED
        assert result.changed_fields == ()

    def test_cited_by_count_never_becomes_a_divergence(self, tmp_path):
        # It is not in the ledger, so a wildly different live count cannot be compared.
        _seed(tmp_path, "W1", doi="10.1234/a", work_type="article", journal="J")
        (result,) = _run_check(tmp_path, {
            "W1": _raw_work("W1", doi="10.1234/a", work_type="article", journal="J",
                            cited_by_count=99999)})
        assert result.status == rf.STATUS_UNCHANGED
        assert "cited_by_count" not in result.changed_fields

    def test_newly_retracted_without_field_change(self, tmp_path):
        _seed(tmp_path, "W1", doi="10.1234/a", work_type="article", journal="J")
        (result,) = _run_check(tmp_path, {
            "W1": _raw_work("W1", doi="10.1234/a", work_type="article", journal="J",
                            is_retracted=True)})
        assert result.status == rf.STATUS_UNCHANGED  # no venue/id field moved
        assert result.newly_retracted is True

    def test_already_retracted_is_not_newly_retracted(self, tmp_path):
        _seed(tmp_path, "W1", doi="10.1234/a", work_type="article", journal="J", is_retracted=True)
        (result,) = _run_check(tmp_path, {
            "W1": _raw_work("W1", doi="10.1234/a", work_type="article", journal="J",
                            is_retracted=True)})
        assert result.newly_retracted is False

    def test_superseded_id_is_flagged(self, tmp_path):
        _seed(tmp_path, "W1", doi="10.1234/a")
        # get_work(W1) follows a redirect and answers with a work whose id is W2.
        (result,) = _run_check(tmp_path, {"W1": _raw_work("W2", doi="10.1234/a")})
        assert result.id_superseded is True
        assert result.returned_id == "W2"
        assert result.status == rf.STATUS_CHANGED

    def test_notfound_is_a_per_id_error(self, tmp_path):
        _seed(tmp_path, "W1")
        entries, _ = rf.collect_ledger_entries(tmp_path)
        (result,) = rf.check_entries(entries, FakeClient(not_found=["W1"]))
        assert result.status == rf.STATUS_ERROR
        assert "no record" in result.reason.lower()

    def test_connection_error_propagates(self, tmp_path):
        _seed(tmp_path, "W1")
        entries, _ = rf.collect_ledger_entries(tmp_path)
        with pytest.raises(OpenAlexConnectionError):
            rf.check_entries(entries, FakeClient(raise_exc=OpenAlexConnectionError("down")))


# --------------------------------------------------------------------------- #
# --auto-update: exactly doi/work_type/journal; never is_retracted; guards
# --------------------------------------------------------------------------- #
class TestApplyAutoUpdate:
    def test_writes_exactly_doi_work_type_journal_and_nothing_else(self, tmp_path):
        md = _seed(tmp_path, "W1", doi="10.1234/old", work_type="preprint", journal=None,
                   is_retracted=True,
                   extra_records=[SourceRecord(type="arxiv", id="1706.03762",
                                               imported_at=IMPORTED_AT,
                                               fields={"version": 1})])
        md_before = _md_stat(md)
        results = _run_check(tmp_path, {
            "W1": _raw_work("W1", doi="10.1234/new", work_type="article", journal="Nature",
                            is_retracted=True)})
        (update,) = rf.apply_auto_update(results, tmp_path)
        assert update.status == rf.UPDATE_WRITTEN

        ledger = _ledger(md)
        # Exactly the three venue/id fields moved; is_retracted copied verbatim; nothing
        # else touched.
        assert ledger[("openalex", "W1")] == {
            "type": "openalex", "id": "W1", "imported_at": IMPORTED_AT,
            "doi": "10.1234/new", "work_type": "article", "journal": "Nature",
            "is_retracted": True,
        }
        # The co-resident arXiv record is untouched.
        assert ledger[("arxiv", "1706.03762")] == {
            "type": "arxiv", "id": "1706.03762", "imported_at": IMPORTED_AT, "version": 1,
        }
        # The original .md is byte- AND mtime_ns-identical: it was never opened.
        assert _md_stat(md) == md_before

    def test_is_retracted_is_never_written_even_when_newly_true(self, tmp_path):
        # OpenAlex now retracts a work the ledger did not record. --auto-update writes
        # the venue fields but NEVER records the retraction (H1).
        md = _seed(tmp_path, "W1", doi="10.1234/a", work_type="article", journal="J")
        results = _run_check(tmp_path, {
            "W1": _raw_work("W1", doi="10.1234/a", work_type="review", journal="J",
                            is_retracted=True)})
        (result,) = results
        assert result.newly_retracted is True  # surfaced
        rf.apply_auto_update(results, tmp_path)
        assert "is_retracted" not in _ledger(md)[("openalex", "W1")]

    def test_no_upstream_change_is_a_byte_identical_noop(self, tmp_path):
        md = _seed(tmp_path, "W1", doi="10.1234/a", work_type="article", journal="J")
        ledger_path = sidecar_path(md)
        before = (ledger_path.read_bytes(), ledger_path.stat().st_mtime_ns)
        results = _run_check(tmp_path, {
            "W1": _raw_work("W1", doi="10.1234/a", work_type="article", journal="J")})
        (update,) = rf.apply_auto_update(results, tmp_path)
        assert update.status == rf.UPDATE_UNCHANGED
        assert (ledger_path.read_bytes(), ledger_path.stat().st_mtime_ns) == before

    def test_a_dropped_doi_is_reflected_not_frozen(self, tmp_path):
        md = _seed(tmp_path, "W1", doi="10.1234/a", work_type="article", journal="J")
        results = _run_check(tmp_path, {
            "W1": _raw_work("W1", doi=None, work_type="article", journal="J")})
        (update,) = rf.apply_auto_update(results, tmp_path)
        assert update.status == rf.UPDATE_WRITTEN
        assert "doi" not in _ledger(md)[("openalex", "W1")]

    def test_superseded_id_is_reported_and_never_written(self, tmp_path):
        md = _seed(tmp_path, "W1", doi="10.1234/a", work_type="preprint")
        before = _md_stat(md)
        ledger_before = sidecar_path(md).read_bytes()
        results = _run_check(tmp_path, {"W1": _raw_work("W2", doi="10.1234/new", work_type="article")})
        (update,) = rf.apply_auto_update(results, tmp_path)
        assert update.status == rf.UPDATE_ID_SUPERSEDED
        # The (type, id) key W1 is untouched; the fields of W2 are not written under it.
        assert _ledger(md)[("openalex", "W1")]["doi"] == "10.1234/a"
        assert sidecar_path(md).read_bytes() == ledger_before
        assert _md_stat(md) == before

    def test_front_matter_only_work_gets_no_ledger(self, tmp_path):
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "old.md").write_text(
            '---\nopenalex_id: "W1"\ntype: "preprint"\n---\n', encoding="utf-8")
        results = _run_check(tmp_path, {"W1": _raw_work("W1", work_type="article")})
        (update,) = rf.apply_auto_update(results, tmp_path)
        assert update.status == rf.UPDATE_NO_LEDGER
        assert "openalex-import" in update.reason
        assert not (tmp_path / "source-provenance").exists()

    def test_corrupt_ledger_read_is_a_per_id_error(self, tmp_path):
        # Guard on the READ. Removing the read guard makes this a traceback.
        (tmp_path / "source-provenance").mkdir(parents=True)
        (tmp_path / "source-provenance" / "bad.json").write_text("{ broken")
        result = rf.RefreshCheck(
            openalex_id="W1", status=rf.STATUS_CHANGED, returned_id="W1",
            current_doi="10.1234/new", changed_fields=("doi",),
            sources=("source-provenance/bad.json",))
        (update,) = rf.apply_auto_update([result], tmp_path)
        assert update.status == rf.UPDATE_ERROR
        assert "bad.json" in update.reason

    def test_unwritable_ledger_write_is_a_per_id_error_and_healthy_still_refresh(
        self, tmp_path, monkeypatch
    ):
        # Guard on the WRITE (#94). One ledger's write raises OSError; the other still
        # refreshes. Removing the write guard makes the whole batch crash.
        good = _seed(tmp_path, "W1", doi="10.1234/a", work_type="preprint", name="good")
        bad = _seed(tmp_path, "W2", doi="10.1234/b", work_type="preprint", name="bad")
        results = _run_check(tmp_path, {
            "W1": _raw_work("W1", doi="10.1234/a", work_type="article"),
            "W2": _raw_work("W2", doi="10.1234/b", work_type="article")})

        real_write = rf.write_provenance
        bad_sidecar = sidecar_path(bad)

        def guarded(path, provenance):
            if str(path) == str(bad_sidecar):
                raise OSError("disk full")
            return real_write(path, provenance)

        monkeypatch.setattr(rf, "write_provenance", guarded)
        outcomes = {u.openalex_id: u for u in rf.apply_auto_update(results, tmp_path)}
        assert outcomes["W2"].status == rf.UPDATE_ERROR
        assert "disk full" in outcomes["W2"].reason
        # The healthy paper was still refreshed despite the sibling's write failure.
        assert outcomes["W1"].status == rf.UPDATE_WRITTEN
        assert _ledger(good)[("openalex", "W1")]["work_type"] == "article"


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
class TestCli:
    def test_reports_divergence_and_leaves_files_immutable(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1", doi=None, work_type="preprint", journal=None)
        before = _snapshot(tmp_path)
        fake(FakeClient({"W1": _raw_work("W1", doi="10.1234/new", work_type="article",
                                         journal="Nature")}))
        code = run(["openalex-refresh", "--target", str(tmp_path)])
        assert code == 0
        out = capsys.readouterr().out
        assert "Metadata diverged" in out
        assert "work_type: preprint -> article" in out
        assert _snapshot(tmp_path) == before  # sources/ + ledgers untouched

    def test_retraction_surfaced_without_flag_and_never_calls_it_withdrawal(
        self, tmp_path, fake, capsys
    ):
        _seed(tmp_path, "W1", doi="10.1234/a", work_type="article", journal="J")
        fake(FakeClient({"W1": _raw_work("W1", doi="10.1234/a", work_type="article",
                                         journal="J", is_retracted=True)}))
        run(["openalex-refresh", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert "RETRACTED" in out
        assert "OpenAlex" in out  # attributed as OpenAlex's opinion
        assert "withdraw" not in out.lower()  # NOT arXiv withdrawal
        assert "retracted:" not in out  # never a bare `retracted:` claim

    def test_retraction_surfaced_with_auto_update_and_not_written(
        self, tmp_path, fake, capsys
    ):
        md = _seed(tmp_path, "W1", doi="10.1234/a", work_type="article", journal="J")
        fake(FakeClient({"W1": _raw_work("W1", doi="10.1234/a", work_type="review",
                                         journal="J", is_retracted=True)}))
        run(["openalex-refresh", "--target", str(tmp_path), "--auto-update"])
        out = capsys.readouterr().out
        assert "RETRACTED" in out
        assert "withdraw" not in out.lower()
        # venue field updated, retraction NOT written
        assert _ledger(md)[("openalex", "W1")]["work_type"] == "review"
        assert "is_retracted" not in _ledger(md)[("openalex", "W1")]

    def test_retraction_of_front_matter_only_work_points_at_the_backfill(
        self, tmp_path, fake, capsys
    ):
        # A pre-#84 work (front matter only, no ledger) newly flagged retracted cannot be
        # acknowledged — `openalex-acknowledge-retraction` writes a sidecar and there is
        # none — so the note must name the missing ledger and prescribe the command that
        # builds one (#115), while the warning stays loud. The word is OpenAlex's
        # "retracted", never "withdrawn".
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "old.md").write_text(
            '---\nopenalex_id: "W1"\ntype: "preprint"\n---\n# T\n', encoding="utf-8")
        fake(FakeClient({"W1": _raw_work("W1", work_type="preprint", is_retracted=True)}))
        code = run(["openalex-refresh", "--target", str(tmp_path)])
        assert code == 0
        out = capsys.readouterr().out
        # Still loud, still OpenAlex's opinion.
        assert "RETRACTED" in out
        assert "OpenAlex" in out
        # Now actionable.
        assert "no provenance ledger (imported before #84)" in out
        assert "cannot be acknowledged" in out
        assert "openalex-backfill-provenance" in out
        # OpenAlex's word is "retracted", never "withdrawn" (#57 §6.3).
        assert "withdraw" not in out.lower()

    def test_auto_update_writes_ledger_and_leaves_md_identical(self, tmp_path, fake, capsys):
        md = _seed(tmp_path, "W1", doi="10.1234/old", work_type="preprint", journal=None)
        md_before = _md_stat(md)
        fake(FakeClient({"W1": _raw_work("W1", doi="10.1234/new", work_type="article",
                                         journal="Nature")}))
        code = run(["openalex-refresh", "--target", str(tmp_path), "--auto-update"])
        assert code == 0
        rec = _ledger(md)[("openalex", "W1")]
        assert (rec["doi"], rec["work_type"], rec["journal"]) == ("10.1234/new", "article", "Nature")
        assert _md_stat(md) == md_before
        assert "Ledger updated" in capsys.readouterr().out

    def test_superseded_id_reported_and_key_untouched(self, tmp_path, fake, capsys):
        md = _seed(tmp_path, "W1", doi="10.1234/a")
        fake(FakeClient({"W1": _raw_work("W2", doi="10.1234/a")}))
        run(["openalex-refresh", "--target", str(tmp_path), "--auto-update"])
        out = capsys.readouterr().out
        assert "superseded" in out.lower()
        assert ("openalex", "W1") in _ledger(md)
        assert ("openalex", "W2") not in _ledger(md)

    def test_notfound_sets_exit_code_and_no_checklog_entry(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1")
        fake(FakeClient(not_found=["W1"]))
        code = run(["openalex-refresh", "--target", str(tmp_path)])
        assert code == 1
        out = capsys.readouterr().out
        assert "Could not check" in out
        # A per-id error never advances the check-log for that work (retried next run).
        assert not check_log_path(tmp_path).exists()

    def test_older_than_reads_only_checklog_and_touches_nothing(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1", doi="10.1234/a")
        now = datetime.now(timezone.utc)
        write_check_log(check_log_path(tmp_path),
                        CheckLog(entries={"W1": CheckRecord((now - timedelta(days=2)).isoformat())}))
        before = _snapshot(tmp_path)
        log_before = check_log_path(tmp_path).read_bytes()
        client = fake(FakeClient(raise_exc=AssertionError("must not hit the API")))
        code = run(["openalex-refresh", "--target", str(tmp_path)])
        assert code == 0
        assert client.calls == []  # a skip queried nothing
        assert _snapshot(tmp_path) == before
        assert check_log_path(tmp_path).read_bytes() == log_before
        assert "Skipped:" in capsys.readouterr().out

    def test_a_work_with_no_checklog_entry_is_due(self, tmp_path, fake):
        _seed(tmp_path, "W1", doi="10.1234/a")
        client = fake(FakeClient({"W1": _raw_work("W1", doi="10.1234/a")}))
        run(["openalex-refresh", "--target", str(tmp_path)])
        assert client.calls == ["W1"]  # never checked -> checked now

    def test_corrupt_check_log_is_a_clear_failure(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1")
        check_log_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        check_log_path(tmp_path).write_text("{ not a check log")
        client = fake(FakeClient({"W1": _raw_work("W1")}))
        code = run(["openalex-refresh", "--target", str(tmp_path)])
        assert code == 1
        assert client.calls == []  # never reached the API
        err = capsys.readouterr().err
        assert "openalex-refresh" in err
        assert "Traceback" not in err

    def test_no_records_is_a_clean_zero(self, tmp_path, capsys):
        (tmp_path / "sources").mkdir()
        code = run(["openalex-refresh", "--target", str(tmp_path)])
        assert code == 0
        assert "no OpenAlex records" in capsys.readouterr().out

    def test_connection_failure_is_exit_2(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1")
        fake(FakeClient(raise_exc=OpenAlexConnectionError("down")))
        code = run(["openalex-refresh", "--target", str(tmp_path)])
        assert code == 2
        # The check-log is left untouched so a re-run starts clean.
        assert not check_log_path(tmp_path).exists()

    def test_porcelain_is_machine_parseable(self, tmp_path, fake, capsys):
        _seed(tmp_path, "W1", doi=None, work_type="preprint", name="a")
        _seed(tmp_path, "W2", doi="10.1234/b", work_type="article", name="b")
        fake(FakeClient({
            "W1": _raw_work("W1", doi="10.1234/new", work_type="article"),
            "W2": _raw_work("W2", doi="10.1234/b", work_type="article")}))
        code = run(["openalex-refresh", "--target", str(tmp_path), "--porcelain"])
        assert code == 0
        captured = capsys.readouterr()
        checks, tallies = {}, {}
        for line in captured.out.strip().splitlines():
            fields = line.split("\t")
            if fields[0] == "check":
                checks[fields[1]] = fields
            else:
                tallies[fields[0]] = fields[1]
        assert tallies["checked"] == "2"
        assert tallies["changed"] == "1"
        assert tallies["target"].endswith(str(tmp_path))
        assert checks["W1"][2] == "changed"
        assert "doi" in checks["W1"][4] and "work_type" in checks["W1"][4]
        assert "checked 2/2" in captured.err
        assert "checked 2/2" not in captured.out

    def test_records_the_checklog_timestamp_for_answered_works(self, tmp_path, fake):
        _seed(tmp_path, "W1", doi="10.1234/a")
        fake(FakeClient({"W1": _raw_work("W1", doi="10.1234/a")}))
        run(["openalex-refresh", "--target", str(tmp_path)])
        log = read_check_log(check_log_path(tmp_path))
        assert "W1" in log.entries
        stamped = datetime.fromisoformat(log.entries["W1"].last_checked_at)
        assert stamped.tzinfo is not None


# --------------------------------------------------------------------------- #
# retraction_note: the backfill branch (#110) must not move the ledger path
# --------------------------------------------------------------------------- #
class TestRetractionNote:
    def test_front_matter_only_note_names_the_missing_ledger_and_the_backfill_command(self):
        note = rf.retraction_note(
            rf.RefreshCheck(
                openalex_id="W1",
                status=rf.STATUS_UNCHANGED,
                newly_retracted=True,
                current_is_retracted=True,
                recorded_from="front-matter",
                sources=("sources/old.md",),
            )
        )
        assert "no provenance ledger (imported before #84)" in note
        assert "cannot be acknowledged" in note
        assert "openalex-backfill-provenance" in note
        # OpenAlex's opinion, never bare fact; never arXiv's word.
        assert "OpenAlex's opinion" in note
        assert "withdraw" not in note.lower()

    def test_ledger_backed_note_is_byte_for_byte_unchanged(self):
        # The regression that matters: the front-matter branch must not move the ledger
        # path by a single byte. This locks the exact string `main` produces.
        note = rf.retraction_note(
            rf.RefreshCheck(
                openalex_id="W1",
                status=rf.STATUS_UNCHANGED,
                newly_retracted=True,
                current_is_retracted=True,
                recorded_from="ledger",
                sources=("source-provenance/a.json",),
            )
        )
        assert note == (
            "OpenAlex now flags W1 as RETRACTED, which the ledger did not record. This "
            "is OpenAlex's opinion — it has false positives, and PubMed (which owns "
            "retraction status) may disagree, as with the Lancet Commission dementia "
            "report. It is a different process from an arXiv preprint being pulled by "
            "its authors, with no shared handling. Confirm before trusting any claim "
            "from this work."
        )
        assert "openalex-backfill-provenance" not in note
        assert "no provenance ledger" not in note
