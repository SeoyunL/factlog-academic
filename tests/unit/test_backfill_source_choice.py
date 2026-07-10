# SPDX-License-Identifier: Apache-2.0
"""A backfill picks its source by what the front matter says, never by its name (#117).

Two `.md` may legitimately carry one `arxiv_id`: an arXiv deposit, and an OpenAlex import
recording the id as a cross-reference (its writer never emits `arxiv_version`). A
provenance sidecar is written **per `.md`**, so the file the backfill acts on decides both
where the ledger lands and what it holds.

`collect_ledger_entries` deduplicated those two by keeping the first in sorted-path order —
right for `arxiv-check-versions`, which checks a paper once, and wrong for a backfill.
Measured on `main`, same paper, same data, only the filenames different:

    sources/a_arxiv.md    + sources/z_openalex.md  -> backfilled (a_arxiv.json, version 7)
    sources/a_openalex.md + sources/z_arxiv.md     -> refused

The file that *had* the version lost the sort and was never read. The refusal is the safe
direction and is correct on its own terms (#113/#121: a backfill never populates an
identifying field it cannot read). Refusing because of a filename is what is wrong.

What is pinned here:

* every filename permutation of one paper backfills **identically** — the arXiv-authored
  `.md` gets the sidecar, carrying its version, whichever way the names sort;
* per-source values: the record written beside a `.md` is built from *that* `.md`'s front
  matter, so a second, higher-versioned deposit cannot leak its version into the first's
  ledger;
* a paper **no** source can supply is still refused, reported per source, and gets no
  sidecar (#113's guard, intact);
* `arxiv-check-versions` is untouched: it still checks such a paper exactly **once**, and
  its answer is still the first source in sorted-path order, *unfolded*. Folding the
  per-source views instead (highest version wins, as the ledger loop folds its sidecars)
  was measured to silence a real drift — `a.md: v3` beside `b.md: v7`, arXiv serving v7,
  reported `unchanged` — so `per_source` is added beside the check's answer, never into it;
* a paper **no** source can supply is still refused, reported per source, and gets no
  sidecar (#113's guard, intact);
* `--dry-run` and the real run agree paper-for-paper, refusals included.

Two `.md` disagreeing about a paper's `arxiv_version` is a *conflict*, and reporting it is
its own issue: `collect_ledger_entries`' ledger loop folds the identical disagreement
between two *sidecars* with `max`, so the fix must move across both consumers at once or a
paper's report would change meaning the moment `arxiv-backfill-provenance` gave it ledgers.
"""
from __future__ import annotations

from datetime import date

import pytest

from factlog.integrations.arxiv import check_versions as cv
from factlog.integrations.arxiv.backfill import backfill_schema
from factlog.integrations.arxiv.client import BatchResult
from factlog.integrations.arxiv.work_parser import ParsedArxivWork
from factlog.integrations.common.backfill import (
    BACKFILL_ERROR,
    BACKFILL_REFUSED,
    BACKFILL_WRITTEN,
    backfill,
)
from factlog.integrations.common.provenance import (
    Provenance,
    SourceRecord,
    read_provenance,
    write_provenance,
)

ARXIV_ID = "1706.03762"
IMPORTED_AT = "2026-01-01T00:00:00+00:00"


def _write(kb, name: str, lines: list[str]) -> None:
    sources = kb / "sources"
    sources.mkdir(parents=True, exist_ok=True)
    (sources / f"{name}.md").write_text(
        "---\n" + "\n".join(lines) + "\n---\n# T\n", encoding="utf-8"
    )


def _arxiv_md(kb, name: str, *, version: int = 7) -> None:
    """What `ArxivSourceWriter` leaves behind: the id *and* the version."""
    _write(
        kb,
        name,
        [
            f'arxiv_id: "{ARXIV_ID}"',
            f"arxiv_version: {version}",
            "imported_from: arxiv",
            f'imported_at: "{IMPORTED_AT}"',
        ],
    )


def _openalex_md(kb, name: str) -> None:
    """What an OpenAlex-primary import leaves behind: `arxiv_id` as a cross-reference,
    and no `arxiv_version` — the field it has no way to know."""
    _write(
        kb,
        name,
        [
            'openalex_id: "W2963403868"',
            f'arxiv_id: "{ARXIV_ID}"',
            "imported_from: openalex",
            f'imported_at: "{IMPORTED_AT}"',
        ],
    )


class _FakeClient:
    """Answers every requested id with one work at *version*. Touches no network."""

    def __init__(self, version: int):
        self._version = version

    def fetch_works(self, ids):
        return BatchResult(
            works=[
                ParsedArxivWork(
                    arxiv_id=ARXIV_ID,
                    version=self._version,
                    title="T",
                    authors=("Ann Author",),
                    abstract="a",
                    primary_category="cs.CL",
                    categories=("cs.CL",),
                    submitted=date(2017, 6, 12),
                    last_updated=date(2020, 1, 1),
                    withdrawn_by=None,
                    abs_url="https://arxiv.org/abs/x",
                    pdf_url="https://arxiv.org/pdf/x",
                )
            ],
            missing=(),
        )


def _outcome(results):
    """The comparable shape of a run: (id, status, ledger) per result, sorted."""
    return sorted((r.entry_id, r.status, r.ledger) for r in results)


def _sidecars(kb):
    root = kb / "source-provenance"
    return sorted(p.name for p in root.glob("*.json")) if root.is_dir() else []


# --------------------------------------------------------------------------- #
# the measured pair, and a third permutation to show it is not pinned to two names
# --------------------------------------------------------------------------- #
#: `(arxiv name, openalex name)`. The first two are the pair measured on `main`, where the
#: sort order between them flipped the outcome. The third puts the arXiv file last again
#: under different names, so the property under test is "the front matter decides", not
#: "these two filenames were special-cased".
PERMUTATIONS = [
    ("a_arxiv", "z_openalex"),
    ("z_arxiv", "a_openalex"),
    ("mmm_arxiv", "nnn_openalex"),
]


@pytest.fixture(params=PERMUTATIONS, ids=lambda p: f"{p[0]}+{p[1]}")
def two_sources(request, tmp_path):
    """One paper, two `.md`, named per the permutation."""
    arxiv_name, openalex_name = request.param
    _arxiv_md(tmp_path, arxiv_name)
    _openalex_md(tmp_path, openalex_name)
    return tmp_path, arxiv_name


class TestFilenamesDoNotDecide:
    def test_the_arxiv_authored_source_gets_the_ledger_in_every_permutation(
        self, two_sources
    ):
        kb, arxiv_name = two_sources
        results = backfill(kb, backfill_schema())

        assert [r.status for r in results] == [BACKFILL_WRITTEN]
        assert results[0].entry_id == ARXIV_ID
        # Next to the source that actually knows the values, not next to whichever
        # filename sorted first.
        assert results[0].ledger == f"source-provenance/{arxiv_name}.json"
        assert _sidecars(kb) == [f"{arxiv_name}.json"]

    def test_the_written_record_carries_the_version_only_that_source_had(
        self, two_sources
    ):
        kb, arxiv_name = two_sources
        backfill(kb, backfill_schema())

        (record,) = read_provenance(kb / "source-provenance" / f"{arxiv_name}.json").records
        assert record.type == "arxiv"
        assert record.id == ARXIV_ID
        assert record.imported_at == IMPORTED_AT
        assert record.fields["version"] == 7

    def test_every_permutation_produces_one_identical_outcome(self, tmp_path):
        # The property the issue names, asserted across the permutations at once rather
        # than one KB at a time: same paper, same data, different names, one outcome.
        outcomes = {}
        for arxiv_name, openalex_name in PERMUTATIONS:
            kb = tmp_path / f"{arxiv_name}-{openalex_name}"
            _arxiv_md(kb, arxiv_name)
            _openalex_md(kb, openalex_name)
            results = backfill(kb, backfill_schema())
            # The ledger path necessarily carries the source's name, so compare the
            # decision — status, id, and *which* source was chosen — not the literal path.
            (result,) = results
            outcomes[(arxiv_name, openalex_name)] = (
                result.entry_id,
                result.status,
                result.ledger == f"source-provenance/{arxiv_name}.json",
            )
        assert set(outcomes.values()) == {(ARXIV_ID, BACKFILL_WRITTEN, True)}

    def test_the_openalex_source_gets_no_arxiv_sidecar(self, two_sources):
        kb, arxiv_name = two_sources
        backfill(kb, backfill_schema())
        # A `.md` that never claimed to carry arXiv's identity is left alone: its remedy
        # is a merging `arxiv-import`, not a ledger built from a version it does not hold.
        assert _sidecars(kb) == [f"{arxiv_name}.json"]

    def test_a_second_deposit_does_not_leak_its_version_into_the_first(self, tmp_path):
        # Two arXiv-authored `.md` for one paper, disagreeing on the version. Each sidecar
        # is built from its own `.md`, so neither records the other's value. Before #117
        # both would have been written from the aggregate entry (the highest version).
        _arxiv_md(tmp_path, "a_old", version=5)
        _arxiv_md(tmp_path, "z_new", version=7)

        results = backfill(tmp_path, backfill_schema())
        assert [r.status for r in results] == [BACKFILL_WRITTEN, BACKFILL_WRITTEN]
        assert _sidecars(tmp_path) == ["a_old.json", "z_new.json"]

        def _version(name):
            (record,) = read_provenance(
                tmp_path / "source-provenance" / f"{name}.json"
            ).records
            return record.fields["version"]

        assert (_version("a_old"), _version("z_new")) == (5, 7)


# --------------------------------------------------------------------------- #
# the refusal #113 built, unchanged
# --------------------------------------------------------------------------- #
class TestNoSourceCanSupply:
    @pytest.fixture
    def kb(self, tmp_path):
        _openalex_md(tmp_path, "a_oa")
        _openalex_md(tmp_path, "z_oa")
        return tmp_path

    def test_refused_reported_and_no_sidecar_is_written(self, kb):
        results = backfill(kb, backfill_schema())

        assert {r.status for r in results} == {BACKFILL_REFUSED}
        assert {r.entry_id for r in results} == {ARXIV_ID}
        # Reported per source: each `.md` is named, so an operator learns which files
        # cannot supply the identity — not merely that "the paper" could not.
        assert sorted(r.reason.split(" ")[0] for r in results) == [
            "sources/a_oa.md",
            "sources/z_oa.md",
        ]
        for result in results:
            assert "cannot supply" in result.reason
            assert "version" in result.reason
        assert not (kb / "source-provenance").exists()


# --------------------------------------------------------------------------- #
# what the check path must keep doing
# --------------------------------------------------------------------------- #
class TestCheckVersionsStillDedups:
    @pytest.mark.parametrize("arxiv_name,openalex_name", PERMUTATIONS)
    def test_one_paper_is_collected_exactly_once(
        self, tmp_path, arxiv_name, openalex_name
    ):
        # First-wins dedup is *right* for a check: a paper is checked once, so arXiv is
        # asked about it once. Only the backfill needed the per-source detail.
        _arxiv_md(tmp_path, arxiv_name)
        _openalex_md(tmp_path, openalex_name)

        entries, errors = cv.collect_ledger_entries(tmp_path)
        assert errors == []
        assert [e.arxiv_id for e in entries] == [ARXIV_ID]

    @pytest.mark.parametrize("arxiv_name,openalex_name", PERMUTATIONS)
    def test_the_checks_answer_is_the_first_source_unfolded(
        self, tmp_path, arxiv_name, openalex_name
    ):
        # The check's entry is the first `.md` in sorted-path order — every field of it,
        # verbatim. #117 is a backfill fix; it adds `per_source` beside this answer and
        # moves nothing in it.
        _arxiv_md(tmp_path, arxiv_name)
        _openalex_md(tmp_path, openalex_name)
        first = min(arxiv_name, openalex_name)

        (entry,), _ = cv.collect_ledger_entries(tmp_path)
        assert entry.sources == (f"sources/{first}.md",)
        # ...including its version, which is `None` when the OpenAlex file sorts first.
        assert entry.recorded_version == (7 if first == arxiv_name else None)

    def test_a_stale_source_is_never_hidden_by_a_fresher_sibling(self, tmp_path):
        # `a.md` records v3, `b.md` records v7, arXiv serves v7. Neither a `max` fold
        # (reports `unchanged`, the paper three versions behind vanishing) nor the old
        # first-wins (reports a plain v3->v7 change, the v7 source vanishing) may hide that
        # the KB's own sources disagree. Since #137 this is neither: two front matters
        # recording two different versions is a *conflict*, its own reportable state naming
        # each source and the value it holds (the sidecar loop treats the identical
        # disagreement the same way, so the paper reads the same after a backfill). It is
        # never `unchanged`, and no longer folded to either source's number.
        _arxiv_md(tmp_path, "a", version=3)
        _arxiv_md(tmp_path, "b", version=7)

        entries, _ = cv.collect_ledger_entries(tmp_path)
        (result,) = cv.check_entries(entries, _FakeClient(version=7))

        assert result.status == cv.STATUS_VERSION_CONFLICT
        assert result.status != cv.STATUS_UNCHANGED
        assert result.recorded_version is None  # not folded to v3 or v7
        assert result.version_disagreement == (("sources/a.md", 3), ("sources/b.md", 7))

    def test_per_source_views_are_one_per_md_and_carry_their_own_values(self, tmp_path):
        _arxiv_md(tmp_path, "a_arxiv")
        _openalex_md(tmp_path, "z_openalex")

        (entry,), _ = cv.collect_ledger_entries(tmp_path)
        assert [(v.sources, v.recorded_version) for v in entry.per_source] == [
            (("sources/a_arxiv.md",), 7),
            (("sources/z_openalex.md",), None),
        ]

    def test_a_ledger_backed_paper_has_no_per_source_views(self, tmp_path):
        # `per_source` speaks for `.md` front matter. A ledger's record belongs to no
        # `.md`, and a backfill never touches such a paper.
        _arxiv_md(tmp_path, "a_arxiv")
        backfill(tmp_path, backfill_schema())

        (entry,), _ = cv.collect_ledger_entries(tmp_path)
        assert entry.sources == ("source-provenance/a_arxiv.json",)
        assert entry.per_source == ()


# --------------------------------------------------------------------------- #
# sidecar_state is read off one real file, so its remedy is true of that file
# --------------------------------------------------------------------------- #
class TestSidecarStateIsNeverFolded:
    """`sidecar_state` selects a *remedy* (`no_ledger_remedy`), so a folded state could
    prescribe a command that does nothing — the failure #116/#121 were sent back for. It is
    not folded: like every other field of the check's entry it is the first source's own
    state, so whatever it says is true of a file that exists.
    """

    def test_an_unreadable_sidecar_on_the_first_source_wins_and_blocks_every_write(
        self, tmp_path
    ):
        # a.md's sidecar will not parse; b.md is a clean deposit with no sidecar.
        _arxiv_md(tmp_path, "a")
        _arxiv_md(tmp_path, "b")
        corrupt = tmp_path / "source-provenance" / "a.json"
        corrupt.parent.mkdir(parents=True)
        corrupt.write_text("{not json", encoding="utf-8")

        (entry,), errors = cv.collect_ledger_entries(tmp_path)
        assert entry.sidecar_state == cv.SIDECAR_UNREADABLE
        # ...and the remedy for that state prescribes no command, because none repairs it.
        assert "repaired by hand" in cv.no_ledger_remedy(
            ARXIV_ID, sidecar_state=entry.sidecar_state, has_recorded_version=True
        )
        # An unreadable ledger poisons the front-matter classification, so *nothing* is
        # backfilled while it stands — including b.md, which could otherwise supply (#111).
        assert len(errors) == 1
        results = backfill(tmp_path, backfill_schema())
        assert {r.status for r in results} == {BACKFILL_ERROR}
        assert _sidecars(tmp_path) == ["a.json"]  # the corrupt one, untouched

    def test_a_readable_sidecar_without_an_arxiv_record_prescribes_a_working_import(
        self, tmp_path
    ):
        # a.md is an OpenAlex import with its own ledger (no arXiv record); b.md is the
        # arXiv deposit, with none. `a` sorts first, so it supplies the state.
        _openalex_md(tmp_path, "a")
        _arxiv_md(tmp_path, "b")
        write_provenance(
            tmp_path / "source-provenance" / "a.json",
            Provenance(records=(
                SourceRecord(type="openalex", id="W2963403868", imported_at=IMPORTED_AT),
            )),
        )

        (entry,), errors = cv.collect_ledger_entries(tmp_path)
        assert errors == []
        assert entry.sidecar_state == cv.SIDECAR_READABLE
        assert "arxiv-import" in cv.no_ledger_remedy(
            ARXIV_ID, sidecar_state=entry.sidecar_state, has_recorded_version=False
        )
        # The state speaks for `a.md`. It does not decide the backfill, which still finds
        # the source that can supply — `b.md` — and writes only there.
        results = backfill(tmp_path, backfill_schema())
        assert [(r.status, r.ledger) for r in results] == [
            (BACKFILL_WRITTEN, "source-provenance/b.json")
        ]
        assert _sidecars(tmp_path) == ["a.json", "b.json"]


# --------------------------------------------------------------------------- #
# the preview may never promise what the run refuses
# --------------------------------------------------------------------------- #
class TestDryRunAgreesWithTheRun:
    @pytest.mark.parametrize("arxiv_name,openalex_name", PERMUTATIONS)
    def test_a_writable_paper(self, tmp_path, arxiv_name, openalex_name):
        _arxiv_md(tmp_path, arxiv_name)
        _openalex_md(tmp_path, openalex_name)

        preview = backfill(tmp_path, backfill_schema(), dry_run=True)
        assert not (tmp_path / "source-provenance").exists()
        assert _outcome(preview) == _outcome(backfill(tmp_path, backfill_schema()))

    def test_a_refused_paper(self, tmp_path):
        _openalex_md(tmp_path, "a_oa")
        _openalex_md(tmp_path, "z_oa")

        preview = backfill(tmp_path, backfill_schema(), dry_run=True)
        real = backfill(tmp_path, backfill_schema())
        assert _outcome(preview) == _outcome(real)
        assert {r.status for r in preview} == {BACKFILL_REFUSED}
        assert not (tmp_path / "source-provenance").exists()
