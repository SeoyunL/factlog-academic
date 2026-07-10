# SPDX-License-Identifier: Apache-2.0
"""A version-less ledger record is a reportable state of its own (#121).

Before this, `_diff` computed ``changed = recorded is not None and current != recorded``,
so a record carrying no ``version`` was reported ``unchanged`` whatever arXiv served —
while ``apply_auto_update``, which is not gated on that flag, rewrote its ``version``
anyway. The report said "changed nothing" and the command changed something. Two bugs in
one: the operator was never told the paper needed repair, and a genuinely drifted version
on such a record was excluded from the one signal the command exists to produce.

The fix is not to make ``changed`` true for a ``None`` recorded value — that prints
"version changed from None to 7", which is #116's ``vNone`` in a new costume. An absent
value gets its own signal: :data:`check_versions.STATUS_NO_VERSION`, its own count, its
own porcelain token, and its own remedy. ``--auto-update`` then fills it *and reports
having filled it*.

The invariant these tests pin: **the command never writes a field the report did not
name.**

And the remedy each paper is given must be the one that *works* for it. `provenance_of`
calls several different papers "front-matter", and they need different commands: a sidecar
that exists and holds no arXiv record is repaired by `arxiv-import`; a paper with no
sidecar at all, version-less, is repaired by nothing that exists; and a sidecar that will
not parse may not even be *described*, let alone prescribed for. Naming a command for
those last two is #116 recreated at the prose layer, so the report says plainly that none
records a version for them.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from factlog import cli
from factlog.integrations.arxiv import check_versions as cv
from factlog.integrations.arxiv.client import BatchResult
from factlog.integrations.arxiv.id_normalizer import ArxivId
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.provenance import (
    Provenance,
    SourceRecord,
    read_provenance,
    sidecar_path,
    write_provenance,
)
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork

ID = "2311.09277"


def _work(arxiv_id=ID, version=7) -> ParsedArxivWork:
    return ParsedArxivWork(
        arxiv_id=arxiv_id,
        version=version,
        title="A paper",
        authors=("Ann Author",),
        abstract="An abstract.",
        primary_category="cs.CL",
        categories=("cs.CL",),
        submitted=date(2023, 11, 15),
        last_updated=date(2024, 1, 1),
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )


class FakeClient:
    """Maps base id -> work; returns results reversed, as the real batch does not
    answer in request order (#57)."""

    def __init__(self, works):
        self._works = {w.arxiv_id: w for w in works}

    def fetch_works(self, ids):
        found, missing = [], []
        for value in ids:
            work = self._works.get(str(value))
            (found if work is not None else missing).append(
                work if work is not None else ArxivId(str(value))
            )
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


def _seed(kb, arxiv_id, version, *, name=None):
    """A healthy ledger record: the pre-existing `changed`/`unchanged` inputs."""
    (kb / "sources").mkdir(exist_ok=True)
    name = name or arxiv_id.replace("/", "_")
    md = kb / "sources" / f"{name}.md"
    md.write_text(
        f"---\narxiv_id: {arxiv_id}\narxiv_version: {version}\n---\n# {name}\n",
        encoding="utf-8",
    )
    write_provenance(
        sidecar_path(md, kb),
        Provenance(
            records=[
                SourceRecord(
                    type="arxiv",
                    id=arxiv_id,
                    imported_at="2026-01-01T00:00:00+00:00",
                    fields={"version": version},
                )
            ]
        ),
    )
    return md


def _seed_versionless(kb, arxiv_id=ID, *, name=None, withdrawn_by=None):
    """A ledger arXiv record carrying no ``version`` at all.

    Since #113 no importer writes one; this is the hand-edited / externally produced
    ledger the issue describes. Written through `Provenance` so the record is otherwise
    exactly what a real ledger holds.
    """
    (kb / "sources").mkdir(exist_ok=True)
    name = name or arxiv_id.replace("/", "_")
    md = kb / "sources" / f"{name}.md"
    md.write_text(f"---\narxiv_id: {arxiv_id}\n---\n# {name}\n", encoding="utf-8")
    fields = {"submitted": "2023-11-15"}
    if withdrawn_by is not None:
        fields["withdrawn_by"] = withdrawn_by
    write_provenance(
        sidecar_path(md, kb),
        Provenance(
            records=[
                SourceRecord(
                    type="arxiv",
                    id=arxiv_id,
                    imported_at="2026-01-01T00:00:00+00:00",
                    fields=fields,
                )
            ]
        ),
    )
    return md


def _seed_openalex_original(kb, arxiv_id=ID):
    """An OpenAlex-primary source whose front matter echoes ``arxiv_id``.

    Its sidecar exists and holds only an ``openalex`` record — so `provenance_of` calls
    the paper "front-matter", but "there is no ledger" is false for it.
    """
    written = OpenAlexSourceWriter().write(
        ParsedWork(
            openalex_id="W1",
            title="A Paper",
            authors=("Ada Lovelace",),
            year=2023,
            journal="Journal of Foo",
            doi=None,
            pmid=None,
            arxiv_id=arxiv_id,
            work_type="article",
        ),
        kb,
        imported_at="2026-01-01T00:00:00Z",
    )
    assert written.status == "imported"
    return written.path


def _record(md):
    return next(r for r in read_provenance(sidecar_path(md, md.parent.parent)).records if r.type == "arxiv")


def _plain(kb):
    return ["arxiv-check-versions", "--target", str(kb), "--older-than", "0"]


class TestThePlainCheckSurfacesTheVersionLessRecord:
    def test_it_is_its_own_state_not_unchanged_and_not_changed(self, tmp_path, fake, capsys):
        _seed_versionless(tmp_path)
        fake(FakeClient([_work(ID, version=7)]))

        assert run(_plain(tmp_path)) == 0
        out = capsys.readouterr().out

        assert "No version recorded" in out
        assert ID in out
        # Not folded into either neighbouring state.
        assert "Version changed:     0" in out
        assert "Up to date:          0" in out
        assert "No version recorded: 1" in out

    def test_the_report_never_prints_a_bare_python_none(self, tmp_path, fake, capsys):
        # #116 in a new costume: "version changed from None to 7". The one thing the
        # issue forbids. `vNone` and a bare `None` must both be absent.
        _seed_versionless(tmp_path)
        fake(FakeClient([_work(ID, version=7)]))

        run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "vNone" not in out
        assert "None" not in out
        assert "v7" in out  # it does say what arXiv serves

    def test_the_signal_names_the_working_remedy(self, tmp_path, fake, capsys):
        # What #116/`7ad3412` established for `_divergence`: a signal that prescribes
        # nothing is wallpaper. The remedy must be the one that measurably works.
        _seed_versionless(tmp_path)
        fake(FakeClient([_work(ID, version=7)]))

        run(_plain(tmp_path))
        assert "arxiv-check-versions --auto-update" in capsys.readouterr().out

    def test_the_silent_direction_a_drifted_version_less_record_is_now_reported(
        self, tmp_path, fake, capsys
    ):
        # The issue's "silent direction": whatever arXiv serves, the pre-#121 command
        # listed nothing. The paper must appear in the report's body, not only in a
        # tally. Two different upstream versions, both surfaced.
        for version in (1, 12):
            _seed_versionless(tmp_path)
            fake(FakeClient([_work(ID, version=version)]))
            run(_plain(tmp_path))
            out = capsys.readouterr().out
            body = out.split("\nSummary:")[0]
            assert ID in body
            assert f"v{version}" in body

    def test_a_paper_whose_sidecar_holds_no_arxiv_record_is_sent_to_arxiv_import(
        self, tmp_path, fake, capsys
    ):
        # An OpenAlex-primary import echoed `arxiv_id` into the front matter. A sidecar
        # DOES exist; it simply holds no arXiv record. Measured: `arxiv-import` merges
        # one in. So the report may name it — but not the reason "there is no ledger".
        original = _seed_openalex_original(tmp_path)
        assert sidecar_path(original, tmp_path).is_file()
        fake(FakeClient([_work(ID, version=7)]))

        run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "No version recorded: 1" in out
        assert "front matter records no version" in out
        assert "ledger holds no arXiv record for this paper" in out
        assert f"factlog arxiv-import --id {ID}" in out
        # --auto-update has no arXiv record to fill, so it must not be prescribed.
        assert "arxiv-check-versions --auto-update" not in out
        # And it must not claim the paper has no ledger — it has one.
        assert "has no provenance ledger" not in out
        assert "None" not in out

    def test_a_paper_with_no_sidecar_at_all_is_told_no_command_repairs_it(
        self, tmp_path, fake, capsys
    ):
        # The honest case #116 asked for. Measured, this paper has NO working remedy:
        #   ArxivSourceWriter().write(...) -> skipped, "already imported (arxiv_id match)"
        #   backfill(...)                  -> refused (required=("version",))
        # So the report must name none. Prescribing `arxiv-import` here — as the
        # UPDATE_NO_LEDGER reason did — is #116 recreated at the prose layer.
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "p.md").write_text(
            f"---\narxiv_id: {ID}\n---\n# p\n", encoding="utf-8"
        )
        assert not sidecar_path(tmp_path / "sources" / "p.md", tmp_path).is_file()
        fake(FakeClient([_work(ID, version=7)]))

        run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "No version recorded: 1" in out
        assert "no command currently records a version for it" in out
        assert "already imported (arxiv_id match)" in out
        assert "refuses a paper whose front matter carries no arxiv_version" in out
        # Not one of these is a working remedy for this paper, so not one is prescribed
        # as an imperative.
        assert "arxiv-check-versions --auto-update" not in out
        assert f"Run `factlog arxiv-import --id {ID}`" not in out
        assert "None" not in out

    def test_a_paper_whose_sidecar_will_not_parse_asserts_nothing_and_prescribes_nothing(
        self, tmp_path, fake, capsys
    ):
        # The fourth UPDATE_NO_LEDGER case. `sidecar_path(md, tmp_path).is_file()` is True for an
        # unparseable ledger, so a bool made this paper indistinguishable from the
        # OpenAlex-primary one: the report asserted the contents of a file it never read
        # ("holds no arXiv record") and prescribed `arxiv-import`, which measurably fails.
        # #128 made this newly reachable (read_provenance now raises on a non-bool
        # `is_retracted` and an out-of-vocabulary `withdrawn_by`).
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "p.md").write_text(
            f"---\narxiv_id: {ID}\n---\n# p\n", encoding="utf-8"
        )
        (tmp_path / "source-provenance").mkdir()
        (tmp_path / "source-provenance" / "p.json").write_text("{ not json", encoding="utf-8")
        fake(FakeClient([_work(ID, version=7)]))

        code = run(_plain(tmp_path))
        out = capsys.readouterr().out

        assert code == 1  # the corrupt ledger reaches the exit code
        assert "No version recorded: 1" in out
        assert "could not be read" in out
        assert "repaired by hand" in out
        # Never assert what an unparsed file holds.
        assert "holds no arXiv record" not in out
        # Never prescribe a command that fails or silently no-ops on this paper.
        assert "arxiv-import" not in out
        assert "arxiv-backfill-provenance" not in out
        assert "arxiv-check-versions --auto-update" not in out
        # The adjacent error line still names the broken ledger.
        assert "corrupt provenance ledger" in out
        assert "None" not in out

    def test_the_unreadable_sidecar_is_a_distinct_state_not_a_readable_one(self, tmp_path):
        # The bool this replaced answered True here. Pin the discrimination at the seam
        # that produces it, so a future refactor cannot quietly collapse the two again.
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "p.md").write_text(
            f"---\narxiv_id: {ID}\n---\n# p\n", encoding="utf-8"
        )
        (tmp_path / "source-provenance").mkdir()
        (tmp_path / "source-provenance" / "p.json").write_text("{ not json", encoding="utf-8")

        (entry,), errors = cv.collect_ledger_entries(tmp_path)
        assert entry.sidecar_state == cv.SIDECAR_UNREADABLE
        assert sidecar_path(tmp_path / "sources" / "p.md", tmp_path).is_file()  # the bool said True
        assert [e.status for e in errors] == [cv.STATUS_ERROR]

    def test_the_two_front_matter_papers_get_different_notes(self, tmp_path, fake, capsys):
        # The collapse this fixes: `provenance_of` says "front-matter" for both, and one
        # note for both is false for at least one of them. Same absent version, same
        # arXiv v7, different remedies — the absent-vs-normal-value distinction of
        # `개념-커버리지-힌트-사일런트미스-vs-부재`, applied to prose.
        def check(state):
            return cv.VersionCheck(
                arxiv_id=ID, status=cv.STATUS_NO_VERSION, current_version=7,
                recorded_from="front-matter", sidecar_state=state,
            )

        readable = cv.no_version_note(check(cv.SIDECAR_READABLE))
        absent = cv.no_version_note(check(cv.SIDECAR_ABSENT))
        unreadable = cv.no_version_note(check(cv.SIDECAR_UNREADABLE))
        assert len({readable, absent, unreadable}) == 3
        assert "arxiv-import" in readable
        assert "no command currently records a version" in absent
        assert "could not be read" in unreadable


class TestAutoUpdateFillsItAndSaysSo:
    def test_the_version_is_written_and_the_report_names_the_write(
        self, tmp_path, fake, capsys
    ):
        md = _seed_versionless(tmp_path)
        assert _record(md).fields.get("version") is None
        fake(FakeClient([_work(ID, version=7)]))

        assert run([*_plain(tmp_path), "--auto-update"]) == 0
        out = capsys.readouterr().out

        assert _record(md).fields["version"] == 7  # the write
        assert "Ledger updated" in out  # the report of the write
        assert f"{ID}: recorded v7 (no version was recorded)" in out
        assert "Ledgers updated:     1" in out
        # The paper is still surfaced as the state it was in when the run began.
        assert "No version recorded: 1" in out
        assert "None" not in out

    def test_the_no_version_line_stops_prescribing_a_command_that_already_ran(
        self, tmp_path, fake, capsys
    ):
        # The report and the write must agree. Telling the operator to run
        # `--auto-update` directly above a line saying it just ran is the same
        # disagreement, inverted.
        _seed_versionless(tmp_path)
        fake(FakeClient([_work(ID, version=7)]))

        run([*_plain(tmp_path), "--auto-update"])
        out = capsys.readouterr().out
        assert "--auto-update recorded it this run" in out
        assert "Run `factlog arxiv-check-versions --auto-update`" not in out

    def test_a_failed_write_points_at_the_error_not_at_the_command_that_just_ran(
        self, tmp_path, fake, capsys
    ):
        # An UPDATE_ERROR paper was told "Run `factlog arxiv-check-versions
        # --auto-update`" during a run of that very command. Point at the failure.
        _seed_versionless(tmp_path)
        fake(FakeClient([_work(ID, version=7)]))
        sidecar = tmp_path / "source-provenance" / f"{ID}.json"
        sidecar.chmod(0o400)
        (tmp_path / "source-provenance").chmod(0o500)
        try:
            code = run([*_plain(tmp_path), "--auto-update"])
        finally:
            (tmp_path / "source-provenance").chmod(0o700)
            sidecar.chmod(0o600)

        out = capsys.readouterr().out
        assert code == 1
        assert "--auto-update could not record it this run" in out
        assert "Run `factlog arxiv-check-versions --auto-update`" not in out
        assert "None" not in out


class TestTheExistingChangedSemanticsDoNotMove:
    def test_a_recorded_version_that_drifted_still_reports_changed(
        self, tmp_path, fake, capsys
    ):
        _seed(tmp_path, "1706.03762", 5)
        fake(FakeClient([_work("1706.03762", version=7)]))

        run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "Version diverged" in out
        assert "ledger records v5, arXiv now serves v7" in out
        assert "Version changed:     1" in out
        assert "No version recorded: 0" in out

    def test_a_recorded_version_that_did_not_drift_still_reports_unchanged(
        self, tmp_path, fake, capsys
    ):
        _seed(tmp_path, "1706.03762", 7)
        fake(FakeClient([_work("1706.03762", version=7)]))

        run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "Up to date:          1" in out
        assert "Version changed:     0" in out
        assert "No version recorded: 0" in out
        assert "Version diverged" not in out

    def test_diff_classifies_the_three_states(self):
        work = _work(ID, version=7)
        assert cv._diff(cv.LedgerEntry(ID, None, None), work).status == cv.STATUS_NO_VERSION
        assert cv._diff(cv.LedgerEntry(ID, 5, None), work).status == cv.STATUS_CHANGED
        assert cv._diff(cv.LedgerEntry(ID, 7, None), work).status == cv.STATUS_UNCHANGED


class TestThePorcelainContract:
    """Porcelain is a machine contract: a new state must appear deterministically, as a
    new *value* in an existing column, never a bare `None` and never a shifted column.

    The tally footer is keyed by its first field. `no_version` lands after `skipped`, so
    every row that existed before it keeps its index, but `updated` and `target` move
    down one. Both orders are pinned below, because the docstring now *claims* that and a
    claim about a contract must be tested.
    """

    def test_the_check_row_for_a_version_less_record_is_verbatim(
        self, tmp_path, fake, capsys
    ):
        _seed_versionless(tmp_path)
        fake(FakeClient([_work(ID, version=7)]))

        run([*_plain(tmp_path), "--porcelain"])
        lines = capsys.readouterr().out.splitlines()

        # id, status, recorded (empty — there is none), current, withdrawn_by,
        # newly_withdrawn, reason, un_withdrawn
        assert f"check\t{ID}\tno-version\t\t7\t\t0\t\t0" in lines
        assert "no_version\t1" in lines
        assert "changed\t0" in lines
        assert "unchanged\t0" in lines
        assert "None" not in "\n".join(lines)

    def test_the_tally_row_lands_after_skipped_on_a_plain_run(
        self, tmp_path, fake, capsys
    ):
        _seed(tmp_path, "1706.03762", 5)
        fake(FakeClient([_work("1706.03762", version=7)]))

        run([*_plain(tmp_path), "--porcelain"])
        lines = capsys.readouterr().out.splitlines()
        tally = [line for line in lines if not line.startswith(("check\t", "update\t"))]
        assert tally == [
            "checked\t1",
            "unchanged\t0",
            "changed\t1",
            "withdrawn\t0",
            "un_withdrawn\t0",
            "errors\t0",
            "skipped\t0",
            "no_version\t0",
            f"target\t{tmp_path}",
        ]

    def test_the_tally_order_under_auto_update_is_pinned_too(self, tmp_path, fake, capsys):
        # `no_version` lands at index 7, pushing `updated` and `target` down by one. The
        # contract says so rather than claiming nothing moves. Pinned so the claim and
        # the output cannot drift apart.
        _seed_versionless(tmp_path)
        fake(FakeClient([_work(ID, version=7)]))

        run([*_plain(tmp_path), "--porcelain", "--auto-update"])
        lines = capsys.readouterr().out.splitlines()
        tally = [line for line in lines if not line.startswith(("check\t", "update\t"))]
        assert tally == [
            "checked\t1",
            "unchanged\t0",
            "changed\t0",
            "withdrawn\t0",
            "un_withdrawn\t0",
            "errors\t0",
            "skipped\t0",
            "no_version\t1",
            "updated\t1",
            f"target\t{tmp_path}",
        ]
        # Every row before `updated` keeps the index it had before #121.
        assert tally[:7] == [
            "checked\t1", "unchanged\t0", "changed\t0", "withdrawn\t0",
            "un_withdrawn\t0", "errors\t0", "skipped\t0",
        ]

    def test_the_update_row_carries_an_empty_recorded_column(self, tmp_path, fake, capsys):
        _seed_versionless(tmp_path)
        fake(FakeClient([_work(ID, version=7)]))

        run([*_plain(tmp_path), "--porcelain", "--auto-update"])
        lines = capsys.readouterr().out.splitlines()
        assert f"update\t{ID}\tupdated\t\t7\tsource-provenance/{ID}.json" in lines
        assert "updated\t1" in lines


class TestTheReportedSetEqualsTheWrittenSet:
    """The general invariant #121 states: the command must not write a field the report
    did not name — nor a paper the report did not name.

    Scope note, so this class is not mistaken for the regression guard it is not: the two
    set-equality tests below **also pass on `main`**. The `✎` section listed version-less
    papers there too (that is where `was vNone` printed from), so
    ``written == reported`` held while the bug was live. What was silent on `main` was the
    *plain*, no-flag report, which listed nothing — and that is pinned by
    `test_the_plain_run_names_the_version_less_paper_auto_update_would_write` and by the
    `TestThePlainCheckSurfacesTheVersionLessRecord` class. These tests pin the invariant
    so a future change cannot break it; they do not detect the original bug.
    """

    def _ledger_fields(self, kb):
        out = {}
        for path in sorted((kb / "source-provenance").glob("*.json")):
            for record in json.loads(path.read_text(encoding="utf-8"))["records"]:
                if record["type"] == "arxiv":
                    out[record["id"]] = {
                        k: v for k, v in record.items()
                        if k not in ("type", "id", "imported_at")
                    }
        return out

    def _fields_written(self, before, after):
        """Field names whose value moved, in either direction.

        Walking only `after` would never see a field the write *removed* — and
        `_refreshed_fields` writes `None` for a comment arXiv dropped, which `to_dict`
        then omits. A deletion is a write.
        """
        moved = set()
        for arxiv_id, fields in after.items():
            was = before.get(arxiv_id, {})
            for key in set(fields) | set(was):
                if was.get(key) != fields.get(key):
                    moved.add(key)
        return moved

    def test_every_field_written_is_a_field_the_report_named(self, tmp_path, fake, capsys):
        # `version-less` + `drifted` in one run: two papers, one report.
        _seed_versionless(tmp_path, name="none")
        _seed(tmp_path, "1706.03762", 5, name="drift")
        fake(FakeClient([_work(ID, version=7), _work("1706.03762", version=7)]))

        before = self._ledger_fields(tmp_path)
        run([*_plain(tmp_path), "--auto-update"])
        after = self._ledger_fields(tmp_path)
        out = capsys.readouterr().out

        written_fields = self._fields_written(before, after)
        assert written_fields, "the run must actually write something to test this"

        # The "Ledger updated" section header is where the report names them. Match the
        # section, not the phrase (the no-version note points at it by name too).
        header = out.split("\nLedger updated (")[1].split("✎")[0]
        named = {f for f in cv.AUTO_UPDATE_FIELDS if f in header}
        assert named == set(cv.AUTO_UPDATE_FIELDS)
        # Containment, deliberately: the report names all three version-tracking fields
        # whether or not each one moved. What must never happen is a field written that
        # the header does not name — `written_fields - named` must be empty.
        assert not (written_fields - named)

    def test_every_paper_written_is_a_paper_the_report_named(self, tmp_path, fake, capsys):
        # The two sets must be equal, not merely overlapping — a write the report omits
        # and a report entry with no write are the same lie in opposite directions.
        # (This held on `main` too: the `✎` section did list the version-less paper. It
        # was the *plain* report that listed nothing. See the class docstring.)
        _seed_versionless(tmp_path, name="none")
        _seed(tmp_path, "1706.03762", 5, name="drift")
        _seed(tmp_path, "1810.04805", 7, name="fresh")
        fake(FakeClient([_work(ID, version=7), _work("1706.03762", version=7),
                         _work("1810.04805", version=7)]))

        before = self._ledger_fields(tmp_path)
        run([*_plain(tmp_path), "--auto-update"])
        out = capsys.readouterr().out
        after = self._ledger_fields(tmp_path)

        written = {i for i in after if after[i] != before.get(i)}
        reported = {
            line.split(":")[0].split()[-1]
            for line in out.splitlines()
            if line.startswith("  ✎ ")
        }
        assert written == reported
        assert ID in written  # the paper the pre-#121 command wrote in silence

    def test_the_plain_run_names_the_version_less_paper_auto_update_would_write(
        self, tmp_path, fake, capsys
    ):
        # And an operator's *first* run — no flags — must connect the paper to the
        # remedy. That connection is what the merge error (#116) had no counterpart for.
        _seed_versionless(tmp_path)
        fake(FakeClient([_work(ID, version=7)]))

        run(_plain(tmp_path))
        body = capsys.readouterr().out.split("\nSummary:")[0]
        assert ID in body
        assert "arxiv-check-versions --auto-update" in body
