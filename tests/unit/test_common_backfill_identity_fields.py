# SPDX-License-Identifier: Apache-2.0
"""Why `BackfillSchema.required` names exactly the fields it names (#113).

A backfilled ledger inherits whatever the front matter got wrong. That is the accepted
cost (#105): the ledger records what this KB believed at import, and a later refresh
surfaces any divergence.

The cost is only acceptable for a value the ledger can *hold*. `version` is an
**identifying** field (`ArxivSourceWriter._IDENTIFYING_FIELDS`), and an absent
identifying field cannot be held: `_record_fields` omits a `None`, and `_identity_fields`
reads the omission back as `{"version": None}`. A later import carrying the real value
sees `None != 7`, calls it a divergence, and errors — a *false* conflict, on a paper that
had none before the backfill ran. That is the discriminating fact, and the contrast test
below measures both halves.

Two earlier justifications for `required` were wrong and are recorded here so they are
not rediscovered:

* *"such a record is unhealable"* — false. `apply_auto_update` is not gated on `changed`,
  so `--auto-update` wrote the missing version all along. It did so **silently**, which
  is the bug #121 fixed, not an unhealable state.
* *"an absent version excludes the paper from version checking"* — true, but it does not
  discriminate. A refused paper is read from front matter and lands in
  `check_versions.STATUS_NO_VERSION` exactly the same way, and unlike the written record
  it has no ledger for `--auto-update` to fill. Exclusion argues *against* refusing.

A record whose `version` is merely *wrong* (`0`, `-1`) is a value the ledger can hold: it
reports as `changed` and the first `--auto-update` rewrites it. So it is recorded, not
refused. These tests pin that boundary: they fail if `version` leaves `required`, they
fail if `withdrawn_by` joins it, and they fail if a version-less record stops making a
later merge error.
"""
from __future__ import annotations

import json
import tempfile
from datetime import date
from pathlib import Path

import factlog.integrations.arxiv.check_versions as cv
import factlog.integrations.common.backfill as bf
from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.provenance import (
    SourceRecord,
    read_provenance,
    sidecar_path,
    write_provenance,
)
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork


def _schema() -> bf.BackfillSchema:
    return bf.BackfillSchema(
        type="arxiv",
        collect_entries=cv.collect_ledger_entries,
        provenance_of=cv.provenance_of,
        id_of=lambda entry: entry.arxiv_id,
        fields={
            "version": lambda entry: entry.recorded_version,
            "withdrawn_by": lambda entry: entry.recorded_withdrawn_by,
        },
        required=("version",),
    )


def _backfill(front_matter: str) -> tuple[list, dict | None]:
    root = Path(tempfile.mkdtemp())
    (root / "sources").mkdir()
    (root / "sources" / "p.md").write_text(
        "---\n" + front_matter + 'imported_at: "2025-01-01T00:00:00Z"\n---\n# P\n',
        encoding="utf-8",
    )
    results = bf.backfill(root, _schema())
    sidecar = root / "source-provenance" / "p.json"
    record = json.loads(sidecar.read_text())["records"][0] if sidecar.is_file() else None
    return results, record


def _arxiv_work(arxiv_id="2311.09277", version=7) -> ParsedArxivWork:
    return ParsedArxivWork(
        arxiv_id=arxiv_id,
        version=version,
        title="A Paper",
        authors=("Ada Lovelace",),
        abstract="An abstract.",
        primary_category="cs.CL",
        categories=("cs.CL",),
        submitted=date(2023, 11, 15),
        last_updated=date(2023, 11, 20),
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )


def _kb_with_openalex_original(arxiv_id="2311.09277") -> tuple[Path, Path]:
    """A KB whose sole source is an OpenAlex-primary record echoing ``arxiv_id``.

    Its front matter carries no ``arxiv_version``, so this is exactly the paper
    ``required=("version",)`` refuses to backfill.
    """
    root = Path(tempfile.mkdtemp())
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
        root,
        imported_at="2026-01-01T00:00:00Z",
    )
    assert written.status == "imported"
    return root, written.path


class TestARefusedRecordIsWhatKeepsALaterMergeClean:
    """The measured contrast the refusal exists for. Same paper, same arXiv v7; the only
    difference is whether a version-less arXiv record was written into the ledger."""

    def test_refusing_leaves_a_later_merge_able_to_succeed(self):
        root, original = _kb_with_openalex_original()
        (result,) = bf.backfill(root, _schema())
        assert result.status == bf.BACKFILL_REFUSED
        # Pin *which* refusal. `backfill` has several, and #113's unreadable-identity
        # refusal is the only one this contrast is about: a test that merely saw
        # "refused" would keep passing if this paper started being refused for the
        # signal-field value space (#109) or a missing `imported_at` instead, and the
        # measurement below would then prove nothing about `required`.
        assert "identifying field(s) version" in result.reason
        assert "version" not in read_provenance(sidecar_path(original)).records[0].fields

        merged = ArxivSourceWriter().write(_arxiv_work(version=7), root, imported_at="t")
        assert merged.status == "merged"

    def test_writing_a_version_less_record_makes_that_same_merge_error(self):
        # What the backfill would have produced had `version` not been `required`:
        # `_record_fields` omits the None, `_identity_fields` reads it back as
        # {"version": None}, and the import sees None != 7 -> a divergence it did not
        # cause. The false conflict is the reason to refuse.
        root, original = _kb_with_openalex_original()
        sidecar = sidecar_path(original)
        provenance = read_provenance(sidecar)
        provenance.records.append(
            SourceRecord(
                type="arxiv",
                id="2311.09277",
                imported_at="2026-01-01T00:00:00Z",
                fields={"primary_category": "cs.CL"},  # no `version`
            )
        )
        write_provenance(sidecar, provenance)

        errored = ArxivSourceWriter().write(_arxiv_work(version=7), root, imported_at="t")
        assert errored.status == "error"
        assert "no version" in errored.reason
        assert "None" not in errored.reason  # #116: never a bare Python None


class TestTheThreeVersionStatesAreDistinct:
    def test_an_absent_version_is_never_the_changed_signal(self):
        # Call `_diff`, not a re-implementation of it: this must fail if someone ever
        # makes a None recorded value report as `changed` (the #116 `vNone` trap).
        result = cv._diff(cv.LedgerEntry("2311.09277", None, None), _arxiv_work(version=7))
        assert result.status == cv.STATUS_NO_VERSION
        assert result.status != cv.STATUS_CHANGED

    def test_a_wrong_version_is_still_seen_as_changed(self):
        result = cv._diff(cv.LedgerEntry("2311.09277", 0, None), _arxiv_work(version=7))
        assert result.status == cv.STATUS_CHANGED

    def test_a_matching_version_is_unchanged(self):
        result = cv._diff(cv.LedgerEntry("2311.09277", 7, None), _arxiv_work(version=7))
        assert result.status == cv.STATUS_UNCHANGED


class TestTheUnreadableIdentityFieldIsRefused:
    def test_front_matter_without_arxiv_version_is_refused(self):
        # An OpenAlex-authored `.md` carries `arxiv_id` but never `arxiv_version`.
        results, record = _backfill('arxiv_id: "2301.00001"\n')
        assert [r.status for r in results] == ["refused"]
        assert record is None

    def test_an_unparseable_version_is_refused(self):
        results, record = _backfill('arxiv_id: "2301.00001"\narxiv_version: "abc"\n')
        assert [r.status for r in results] == ["refused"]
        assert record is None


class TestARecoverableWrongValueIsRecorded:
    """A wrong-but-present version is the KB's belief. Record it; the refresh fixes it."""

    def test_a_bogus_version_is_recorded_rather_than_refused(self):
        # arXiv versions start at v1, so 0 cannot be real — but `--auto-update` rewrites it.
        results, record = _backfill('arxiv_id: "2301.00001"\narxiv_version: 0\n')
        assert [r.status for r in results] == ["backfilled"]
        assert record["version"] == 0


class TestALegitimateNoneIsNotRequired:
    """`withdrawn_by` is `None` for every paper that is not withdrawn — the overwhelming
    majority. Requiring it would refuse almost the entire library.
    """

    def test_a_paper_that_is_not_withdrawn_still_backfills(self):
        results, record = _backfill('arxiv_id: "2301.00001"\narxiv_version: 7\n')
        assert [r.status for r in results] == ["backfilled"]
        assert "withdrawn_by" not in record

    def test_a_withdrawn_paper_records_the_agent(self):
        results, record = _backfill(
            'arxiv_id: "2301.00001"\narxiv_version: 7\narxiv_withdrawn_by: "author"\n'
        )
        assert [r.status for r in results] == ["backfilled"]
        assert record["withdrawn_by"] == "author"

    def test_withdrawn_by_is_not_declared_required(self):
        assert "withdrawn_by" not in _schema().required
        assert "version" in _schema().required
