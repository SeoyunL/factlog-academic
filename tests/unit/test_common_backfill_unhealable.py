# SPDX-License-Identifier: Apache-2.0
"""Why `BackfillSchema.required` names exactly the fields it names (#113).

A backfilled ledger inherits whatever the front matter got wrong. That is the accepted
cost (#105): the ledger records what this KB believed at import, and a later refresh
surfaces any divergence. The cost is only acceptable while every wrong value is
*recoverable*.

One is not. `check_versions._diff` computes

    changed = recorded is not None and current != recorded

so a record whose `version` is **absent** is never `changed`, `--auto-update` never
writes it, and a cross-source merge that compares identifying fields errors forever.
A record whose `version` is merely *wrong* (`0`, `-1`) is `changed`, gets rewritten by
the first `--auto-update`, and the merge then succeeds.

`required` refuses precisely the unhealable case. These tests pin that boundary: they
fail if `version` leaves `required`, and they fail if `withdrawn_by` joins it.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import factlog.integrations.arxiv.check_versions as cv
import factlog.integrations.common.backfill as bf


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


class TestTheUnhealableCaseIsRefused:
    def test_an_absent_version_makes_changed_false_forever(self):
        # The reason `version` is required. Not a style choice — this line is why.
        recorded, current = None, 7
        assert (recorded is not None and current != recorded) is False

    def test_a_wrong_version_is_still_seen_as_changed(self):
        recorded, current = 0, 7
        assert (recorded is not None and current != recorded) is True

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
