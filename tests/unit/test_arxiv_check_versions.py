# SPDX-License-Identifier: Apache-2.0
"""`factlog arxiv-check-versions` — report-only version drift (#78, spec §11 Step 6).

The real arXiv client is replaced via ``_make_arxiv_client`` so the command runs
without the network. A temp KB carries source ``.md`` originals and their
provenance ledgers; the tests assert the originals and ledgers stay byte- and
``mtime_ns``-identical, that a newly-withdrawn paper is surfaced even without a
version change, that a nonexistent id is an error (not "unchanged"), that
``--older-than`` reads only the check-log, and that a corrupt ledger / corrupt
check-log fail per-id / clearly rather than as a traceback.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from factlog import cli
from factlog.integrations.arxiv import check_versions as cv
from factlog.integrations.arxiv.check_log import (
    CheckLog,
    CheckRecord,
    check_log_path,
    read_check_log,
    write_check_log,
)
from factlog.integrations.arxiv.client import BatchResult
from factlog.integrations.arxiv.id_normalizer import ArxivId
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.provenance import (
    Provenance,
    SourceRecord,
    sidecar_path,
    write_provenance,
)


# --------------------------------------------------------------------------- #
# fixtures / helpers
# --------------------------------------------------------------------------- #
def _work(arxiv_id="1706.03762", version=7, withdrawn_by=None) -> ParsedArxivWork:
    return ParsedArxivWork(
        arxiv_id=arxiv_id,
        version=version,
        title="A paper",
        authors=("Ann Author",),
        abstract="An abstract.",
        primary_category="cs.CL",
        categories=("cs.CL",),
        submitted=date(2017, 6, 12),
        last_updated=date(2020, 1, 1),
        withdrawn_by=withdrawn_by,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )


def _seed(kb, arxiv_id, version, *, withdrawn_by=None, name=None, extra_records=()):
    """Write a source ``.md`` and its arXiv provenance ledger. Returns the .md path."""
    (kb / "sources").mkdir(exist_ok=True)
    name = name or arxiv_id.replace("/", "_")
    md = kb / "sources" / f"{name}.md"
    md.write_text(f"---\narxiv_id: {arxiv_id}\narxiv_version: {version}\n---\n# {name}\n")
    fields = {"version": version}
    if withdrawn_by is not None:
        fields["withdrawn_by"] = withdrawn_by
    records = [
        SourceRecord(type="arxiv", id=arxiv_id, imported_at="2026-01-01T00:00:00+00:00",
                     fields=fields),
        *extra_records,
    ]
    write_provenance(sidecar_path(md), Provenance(records=records))
    return md


class FakeClient:
    """Maps base id -> work; returns a BatchResult, reversed to prove the code does
    not rely on response order. Records every id list it was asked for."""

    def __init__(self, works, *, raise_exc=None):
        self._works = {w.arxiv_id: w for w in works}
        self._raise = raise_exc
        self.calls: list[list[str]] = []

    def fetch_works(self, ids):
        self.calls.append([str(i) for i in ids])
        if self._raise is not None:
            raise self._raise
        found, missing = [], []
        for value in ids:
            base = str(value)
            work = self._works.get(base)
            if work is None:
                missing.append(ArxivId(base))
            else:
                found.append(work)
        return BatchResult(list(reversed(found)), missing)


@pytest.fixture
def fake(monkeypatch):
    def install(client):
        monkeypatch.setattr(cli, "_make_arxiv_client", lambda config: client)
        return client

    return install


def run(argv):
    args = cli.build_parser().parse_args(argv)
    return args.func(args)


def _snapshot(kb):
    """Bytes and mtime_ns for every file under sources/ and source-provenance/."""
    snap = {}
    for sub in ("sources", "source-provenance"):
        root = kb / sub
        if root.is_dir():
            for path in root.rglob("*"):
                if path.is_file():
                    st = path.stat()
                    snap[path] = (path.read_bytes(), st.st_mtime_ns)
    return snap


# --------------------------------------------------------------------------- #
# module-level unit tests
# --------------------------------------------------------------------------- #
class TestCollect:
    def test_gathers_arxiv_records_and_dedups_by_id(self, tmp_path):
        _seed(tmp_path, "1706.03762", 5, name="a")
        # A second ledger cites the same paper at a lower version + a non-arxiv record.
        _seed(tmp_path, "1706.03762", 3, name="b",
              extra_records=[SourceRecord(type="openalex", id="W1",
                                          imported_at="2026-01-01T00:00:00+00:00")])
        entries, errors = cv.collect_ledger_entries(tmp_path)
        assert errors == []
        assert len(entries) == 1
        entry = entries[0]
        assert entry.arxiv_id == "1706.03762"
        assert entry.recorded_version == 5  # the highest recorded wins
        assert set(entry.sources) == {"source-provenance/a.json", "source-provenance/b.json"}

    def test_records_withdrawal_agent_from_ledger(self, tmp_path):
        _seed(tmp_path, "1904.09773", 1, withdrawn_by="admin")
        (entry,), _ = cv.collect_ledger_entries(tmp_path)
        assert entry.recorded_withdrawn_by == "admin"

    def test_corrupt_ledger_is_a_per_id_error_not_a_crash(self, tmp_path):
        _seed(tmp_path, "1706.03762", 5, name="good")
        bad = tmp_path / "source-provenance" / "bad.json"
        bad.write_text("{not json")
        entries, errors = cv.collect_ledger_entries(tmp_path)
        assert [e.arxiv_id for e in entries] == ["1706.03762"]  # the good one survives
        assert len(errors) == 1
        assert errors[0].status == cv.STATUS_ERROR
        assert "source-provenance/bad.json" in errors[0].arxiv_id
        assert "corrupt provenance ledger" in errors[0].reason

    def test_missing_sidecar_dir_is_empty_not_an_error(self, tmp_path):
        assert cv.collect_ledger_entries(tmp_path) == ([], [])


class TestFreshness:
    def _entries(self):
        return [cv.LedgerEntry("1706.03762", 5, None)]

    def test_recently_checked_is_skipped(self):
        now = datetime(2026, 7, 9, tzinfo=timezone.utc)
        log = CheckLog(entries={"1706.03762": CheckRecord(
            last_checked_at=(now - timedelta(days=3)).isoformat(), version=5)})
        to_check, skipped = cv.partition_by_freshness(self._entries(), log, 30, now)
        assert to_check == []
        assert [s.arxiv_id for s in skipped] == ["1706.03762"]
        assert skipped[0].status == cv.STATUS_SKIPPED

    def test_stale_check_is_re_checked(self):
        now = datetime(2026, 7, 9, tzinfo=timezone.utc)
        log = CheckLog(entries={"1706.03762": CheckRecord(
            last_checked_at=(now - timedelta(days=40)).isoformat(), version=5)})
        to_check, skipped = cv.partition_by_freshness(self._entries(), log, 30, now)
        assert [e.arxiv_id for e in to_check] == ["1706.03762"]
        assert skipped == []

    def test_never_checked_is_checked(self):
        now = datetime(2026, 7, 9, tzinfo=timezone.utc)
        to_check, skipped = cv.partition_by_freshness(self._entries(), CheckLog(), 30, now)
        assert len(to_check) == 1 and skipped == []

    def test_older_than_zero_forces_recheck(self):
        now = datetime(2026, 7, 9, tzinfo=timezone.utc)
        log = CheckLog(entries={"1706.03762": CheckRecord(
            last_checked_at=now.isoformat(), version=5)})
        to_check, skipped = cv.partition_by_freshness(self._entries(), log, 0, now)
        assert len(to_check) == 1 and skipped == []


class TestCheckEntries:
    def test_matches_by_id_across_batches(self):
        entries = [cv.LedgerEntry(f"2000.0000{i}", 1, None) for i in range(3)]
        works = [_work(f"2000.0000{i}", version=1) for i in range(3)]
        client = FakeClient(works)
        results = cv.check_entries(entries, client, batch_size=2)
        assert len(client.calls) == 2  # 3 ids in batches of 2
        assert all(r.status == cv.STATUS_UNCHANGED for r in results)

    def test_version_change_and_missing(self):
        entries = [cv.LedgerEntry("1706.03762", 5, None),
                   cv.LedgerEntry("9999.99999", 1, None)]
        client = FakeClient([_work("1706.03762", version=7)])
        results = {r.arxiv_id: r for r in cv.check_entries(entries, client)}
        assert results["1706.03762"].status == cv.STATUS_CHANGED
        assert results["1706.03762"].current_version == 7
        assert results["9999.99999"].status == cv.STATUS_ERROR

    def test_newly_withdrawn_without_version_change(self):
        entries = [cv.LedgerEntry("1904.09773", 1, None)]
        client = FakeClient([_work("1904.09773", version=1, withdrawn_by="admin")])
        (result,) = cv.check_entries(entries, client)
        assert result.status == cv.STATUS_UNCHANGED  # version did not move
        assert result.newly_withdrawn is True
        assert result.withdrawn_by == "admin"

    def test_already_recorded_withdrawal_is_not_newly_withdrawn(self):
        entries = [cv.LedgerEntry("1904.09773", 1, "admin")]
        client = FakeClient([_work("1904.09773", version=1, withdrawn_by="admin")])
        (result,) = cv.check_entries(entries, client)
        assert result.newly_withdrawn is False


# --------------------------------------------------------------------------- #
# CLI tests
# --------------------------------------------------------------------------- #
class TestCli:
    def test_reports_divergence_and_leaves_files_immutable(self, tmp_path, fake, capsys):
        _seed(tmp_path, "1706.03762", 5)
        before = _snapshot(tmp_path)
        fake(FakeClient([_work("1706.03762", version=7)]))
        code = run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert code == 0
        out = capsys.readouterr().out
        assert "ledger records v5, arXiv now serves v7" in out
        # Every source and every ledger is byte- AND mtime_ns-identical.
        assert _snapshot(tmp_path) == before

    def test_newly_withdrawn_is_surfaced_even_when_version_unchanged(
        self, tmp_path, fake, capsys
    ):
        _seed(tmp_path, "1904.09773", 1)
        fake(FakeClient([_work("1904.09773", version=1, withdrawn_by="admin")]))
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert "WITHDRAWN by arXiv administrators" in out
        assert "retracted" not in out.lower()

    def test_a_stale_entry_surfaces_a_new_withdrawal(self, tmp_path, fake, capsys):
        # A paper past the freshness window is checked, and a withdrawal recorded
        # nowhere in its ledger surfaces. The name used to claim this held
        # "regardless of --older-than skip"; it does not, and
        # `TestAFreshPapersWithdrawalIsNotDetected` pins what actually happens.
        _seed(tmp_path, "1904.09773", 1)
        now = datetime.now(timezone.utc)
        write_check_log(check_log_path(tmp_path), CheckLog(entries={
            "1904.09773": CheckRecord((now - timedelta(days=90)).isoformat(), 1)}))
        fake(FakeClient([_work("1904.09773", version=1, withdrawn_by="author")]))
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert "WITHDRAWN by the author" in capsys.readouterr().out

    def test_nonexistent_id_is_an_error_not_unchanged(self, tmp_path, fake, capsys):
        _seed(tmp_path, "9999.99999", 1)
        fake(FakeClient([]))  # arXiv returns nothing -> missing
        code = run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert code == 1  # an error sets the exit code
        out = capsys.readouterr().out
        assert "no entry returned by arXiv" in out
        assert "Up to date:      0" in out

    def test_older_than_skips_recent_and_touches_nothing(self, tmp_path, fake, capsys):
        md = _seed(tmp_path, "1706.03762", 5)
        now = datetime.now(timezone.utc)
        write_check_log(check_log_path(tmp_path), CheckLog(entries={
            "1706.03762": CheckRecord((now - timedelta(days=2)).isoformat(), 5)}))
        before = _snapshot(tmp_path)
        log_before = check_log_path(tmp_path).read_bytes()
        # If the client were called it would raise; a skip must not call it.
        client = fake(FakeClient([], raise_exc=AssertionError("must not hit the API")))
        code = run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert code == 0
        assert client.calls == []  # nothing queried
        assert _snapshot(tmp_path) == before  # sources/ + ledgers untouched
        assert check_log_path(tmp_path).read_bytes() == log_before  # log untouched too
        assert "Skipped:         1" in capsys.readouterr().out
        assert md.exists()

    def test_corrupt_ledger_is_per_id_error_not_traceback(self, tmp_path, fake, capsys):
        _seed(tmp_path, "1706.03762", 5, name="good")
        (tmp_path / "source-provenance" / "bad.json").write_text("{ broken")
        fake(FakeClient([_work("1706.03762", version=5)]))
        code = run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert code == 1
        out = capsys.readouterr().out
        assert "corrupt provenance ledger" in out
        assert "Up to date:      1" in out  # the good paper was still checked

    def test_corrupt_check_log_is_a_clear_failure(self, tmp_path, fake, capsys):
        _seed(tmp_path, "1706.03762", 5)
        check_log_path(tmp_path).parent.mkdir(parents=True, exist_ok=True)
        check_log_path(tmp_path).write_text("{ not a check log")
        client = fake(FakeClient([_work("1706.03762", version=7)]))
        code = run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert code == 1
        assert client.calls == []  # never reached the API
        err = capsys.readouterr().err
        assert "arxiv-check-versions" in err
        assert "Traceback" not in err

    def test_check_log_records_int_version_and_timestamp(self, tmp_path, fake):
        _seed(tmp_path, "1706.03762", 5)
        fake(FakeClient([_work("1706.03762", version=7)]))
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        log = read_check_log(check_log_path(tmp_path))
        record = log.entries["1706.03762"]
        assert record.version == 7
        assert isinstance(record.version, int)
        stamped = datetime.fromisoformat(record.last_checked_at)
        assert stamped.tzinfo is not None

    def test_missing_id_does_not_get_a_check_log_entry(self, tmp_path, fake):
        _seed(tmp_path, "9999.99999", 1)
        fake(FakeClient([]))
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        # No version was observed, so nothing was recorded and no log was written.
        assert not check_log_path(tmp_path).exists()

    def test_porcelain_is_machine_parseable_and_progress_on_stderr(
        self, tmp_path, fake, capsys
    ):
        _seed(tmp_path, "1706.03762", 5)
        _seed(tmp_path, "1810.04805", 1, name="bert")
        fake(FakeClient([_work("1706.03762", version=7), _work("1810.04805", version=1)]))
        code = run(["arxiv-check-versions", "--target", str(tmp_path), "--porcelain"])
        assert code == 0
        captured = capsys.readouterr()
        rows = {}
        checks = {}
        for line in captured.out.strip().splitlines():
            fields = line.split("\t")
            if fields[0] == "check":
                checks[fields[1]] = fields
            else:
                rows[fields[0]] = fields[1]
        assert rows["checked"] == "2"
        assert rows["changed"] == "1"
        assert rows["target"].endswith(str(tmp_path))
        assert checks["1706.03762"][2] == "changed"
        assert checks["1706.03762"][3] == "5" and checks["1706.03762"][4] == "7"
        # Progress/ETA is on stderr, never stdout.
        assert "checked 2/2" in captured.err
        assert "checked 2/2" not in captured.out

    def test_no_records_is_a_clean_zero(self, tmp_path, capsys):
        (tmp_path / "sources").mkdir()
        code = run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert code == 0
        assert "no arXiv records" in capsys.readouterr().out


class TestAKbWithNoLedgerIsStillChecked:
    """Every arXiv record got a provenance ledger only as of #82. A KB imported
    before that has front matter and no ledger, and reading only the ledgers made
    the command answer "no arXiv records" and exit 0 — silently checking nothing,
    for most of an existing library."""

    def _old_style_source(self, kb, arxiv_id="1706.03762", version=5):
        (kb / "sources").mkdir(parents=True, exist_ok=True)
        path = kb / "sources" / "old.md"
        path.write_text(
            f'---\narxiv_id: "{arxiv_id}"\narxiv_version: {version}\n'
            f"imported_from: arxiv\n---\n# T\n",
            encoding="utf-8",
        )
        return path

    def test_front_matter_alone_is_enough_to_be_checked(self, tmp_path):
        self._old_style_source(tmp_path)
        entries, errors = cv.collect_ledger_entries(tmp_path)
        assert errors == []
        assert [(e.arxiv_id, e.recorded_version) for e in entries] == [("1706.03762", 5)]
        assert entries[0].sources == ("sources/old.md",)

    def test_a_ledger_wins_over_front_matter_when_both_exist(self, tmp_path):
        # The ledger is what a refresh updates, so it is authoritative, and its
        # `sources` name the ledgers a reader should open.
        _seed(tmp_path, "1706.03762", 7, name="a")
        self._old_style_source(tmp_path, "1706.03762", version=5)
        entries, _ = cv.collect_ledger_entries(tmp_path)
        assert len(entries) == 1
        assert entries[0].recorded_version == 7
        assert entries[0].sources == ("source-provenance/a.json",)

    def test_a_source_without_an_arxiv_id_is_ignored(self, tmp_path):
        (tmp_path / "sources").mkdir(parents=True)
        (tmp_path / "sources" / "zotero.md").write_text(
            '---\nzotero_key: "ABC"\nimported_from: zotero\n---\n', encoding="utf-8")
        assert cv.collect_ledger_entries(tmp_path) == ([], [])

    def test_a_malformed_arxiv_version_does_not_crash_the_enumeration(self, tmp_path):
        (tmp_path / "sources").mkdir(parents=True)
        (tmp_path / "sources" / "bad.md").write_text(
            '---\narxiv_id: "1706.03762"\narxiv_version: "seven"\n---\n', encoding="utf-8")
        entries, errors = cv.collect_ledger_entries(tmp_path)
        assert errors == []
        assert entries[0].recorded_version is None  # unknown, not a crash


class TestAFreshPapersWithdrawalIsNotDetected:
    """`--older-than` skips a recently-checked paper without querying arXiv, and
    the check-log stores only `last_checked_at` and `version` — not withdrawal
    state. So a withdrawal that appears *inside* the freshness window is invisible
    until the window expires.

    This is inherent: arXiv only says a paper is withdrawn when asked. What matters
    is that the command does not pretend otherwise. Nothing pinned this, and the
    test that claimed to had quietly made its paper stale.
    """

    def _fresh_kb(self, tmp_path, days_ago=1):
        _seed(tmp_path, "1904.09773", 1)
        now = datetime.now(timezone.utc)
        write_check_log(check_log_path(tmp_path), CheckLog(entries={
            "1904.09773": CheckRecord((now - timedelta(days=days_ago)).isoformat(), 1)}))
        return tmp_path

    def test_a_withdrawal_inside_the_window_is_not_surfaced(self, tmp_path, fake, capsys):
        kb = self._fresh_kb(tmp_path)
        client = fake(FakeClient([_work("1904.09773", version=1, withdrawn_by="admin")]))
        assert run(["arxiv-check-versions", "--target", str(kb)]) == 0
        out = capsys.readouterr().out
        assert client.calls == [], "a skipped paper must not be queried"
        assert "WITHDRAWN" not in out

    def test_the_skip_note_says_a_withdrawal_would_be_missed(self, tmp_path, fake, capsys):
        # The command must not let a reader infer "0 newly withdrawn" means "none".
        kb = self._fresh_kb(tmp_path)
        fake(FakeClient([_work("1904.09773", version=1, withdrawn_by="admin")]))
        run(["arxiv-check-versions", "--target", str(kb)])
        out = capsys.readouterr().out
        assert "NOT detected" in out
        assert "--older-than 0" in out

    def test_forcing_the_recheck_surfaces_it(self, tmp_path, fake, capsys):
        kb = self._fresh_kb(tmp_path)
        fake(FakeClient([_work("1904.09773", version=1, withdrawn_by="admin")]))
        assert run(["arxiv-check-versions", "--target", str(kb), "--older-than", "0"]) == 0
        assert "WITHDRAWN by arXiv administrators" in capsys.readouterr().out


class TestAFrontMatterOnlyPaperIsNotCalledALedger:
    """A paper imported before #82 has no ledger. Saying "ledger records v5" for it
    names a file that does not exist."""

    def test_the_report_says_front_matter_when_there_is_no_ledger(self, tmp_path, fake, capsys):
        (tmp_path / "sources").mkdir(parents=True)
        (tmp_path / "sources" / "old.md").write_text(
            '---\narxiv_id: "1706.03762"\narxiv_version: 5\n---\n', encoding="utf-8")
        fake(FakeClient([_work("1706.03762", version=7)]))
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        out = capsys.readouterr().out
        assert "front matter records v5" in out
        assert "ledger records" not in out

    def test_a_ledger_backed_paper_still_says_ledger(self, tmp_path, fake, capsys):
        _seed(tmp_path, "1706.03762", 5)
        fake(FakeClient([_work("1706.03762", version=7)]))
        run(["arxiv-check-versions", "--target", str(tmp_path)])
        assert "ledger records v5" in capsys.readouterr().out
