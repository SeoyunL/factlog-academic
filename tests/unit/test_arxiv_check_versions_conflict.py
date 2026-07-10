# SPDX-License-Identifier: Apache-2.0
"""A paper whose own sources disagree about ``arxiv_version`` is a reportable conflict,
never folded to one number (#137).

`collect_ledger_entries` used to fold several records for a paper by taking the **highest**
recorded version (the sidecar loop) or the first in sorted-path order (the front-matter
loop). When two records claimed a *different* version, the fold silently dropped one:
``a.json`` recording v3 beside ``b.json`` recording v7, arXiv serving v7, reported
``unchanged`` — the paper three versions behind vanished from the one signal the command
exists to produce, the exact inverse of it.

Two records claiming a different ``arxiv_version`` for one paper is a **conflict**, not an
input to a maximum, and this repo reports or refuses a conflict rather than resolve it
(`add_source` raises `ProvenanceConflict`; a backfill refuses an unreadable identifying
field, #113/#121). So the paper is its own reportable state, :data:`STATUS_VERSION_CONFLICT`,
naming each source and the value it holds; no command resolves it; and it moves across
**both** folds at once (sidecar and front-matter, #117) so its meaning does not change when
`arxiv-backfill-provenance` writes a sidecar.

The invariant these tests pin: a paper's sources disagreeing about the recorded version is
surfaced, never folded — and a KB with no such disagreement reports byte-identically to
before #137.
"""
from __future__ import annotations

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
    sidecar_path,
    write_provenance,
)

ID = "1706.03762"


def _work(arxiv_id=ID, version=7) -> ParsedArxivWork:
    return ParsedArxivWork(
        arxiv_id=arxiv_id, version=version, title="A paper", authors=("Ann Author",),
        abstract="An abstract.", primary_category="cs.CL", categories=("cs.CL",),
        submitted=date(2017, 6, 12), last_updated=date(2020, 1, 1), withdrawn_by=None,
        abs_url=f"https://arxiv.org/abs/{arxiv_id}v{version}",
        pdf_url=f"https://arxiv.org/pdf/{arxiv_id}v{version}",
    )


class FakeClient:
    """Maps base id -> work; returns results reversed, as the real batch does not answer
    in request order (#57)."""

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


def _seed_sidecar(kb, version, *, name, arxiv_id=ID, extra_records=()):
    """A ``.md`` with an arXiv provenance sidecar recording *version*."""
    (kb / "sources").mkdir(exist_ok=True)
    md = kb / "sources" / f"{name}.md"
    md.write_text(f"---\narxiv_id: {arxiv_id}\narxiv_version: {version}\n---\n# {name}\n",
                  encoding="utf-8")
    write_provenance(sidecar_path(md, kb), Provenance(records=[
        SourceRecord(type="arxiv", id=arxiv_id, imported_at="2026-01-01T00:00:00+00:00",
                     fields={"version": version}),
        *extra_records,
    ]))
    return md


def _seed_front_matter(kb, version, *, name, arxiv_id=ID):
    """A front-matter-only ``.md`` (imported before #82) recording *version*, no sidecar."""
    (kb / "sources").mkdir(exist_ok=True)
    md = kb / "sources" / f"{name}.md"
    md.write_text(f"---\narxiv_id: {arxiv_id}\narxiv_version: {version}\n---\n# {name}\n",
                  encoding="utf-8")
    return md


def _plain(kb):
    return ["arxiv-check-versions", "--target", str(kb), "--older-than", "0"]


# --------------------------------------------------------------------------- #
# The unit: `_fold_recorded`
# --------------------------------------------------------------------------- #
class TestTheFold:
    def test_agreement_folds_to_one_value(self):
        assert cv._fold_recorded([("a.json", 5), ("b.json", 5)]) == (5, ())

    def test_a_single_source_is_that_value(self):
        assert cv._fold_recorded([("a.json", 5)]) == (5, ())

    def test_disagreement_is_a_conflict_naming_every_source(self):
        version, pairs = cv._fold_recorded([("b.json", 7), ("a.json", 3)])
        assert version is None  # never `max`, never first
        assert pairs == (("a.json", 3), ("b.json", 7))  # sorted, both named

    def test_nothing_recorded_is_no_version_not_a_conflict(self):
        assert cv._fold_recorded([]) == (None, ())


# --------------------------------------------------------------------------- #
# The sidecar fold (the loop measured in the issue)
# --------------------------------------------------------------------------- #
class TestTwoSidecarsDisagree:
    def test_the_silent_direction_max_reported_unchanged(self, tmp_path, fake, capsys):
        # a.json v3, b.json v7, arXiv serving v7. The pre-#137 `max` folded to v7 and
        # reported `unchanged`; the drift on a.json vanished. It must not.
        _seed_sidecar(tmp_path, 3, name="a")
        _seed_sidecar(tmp_path, 7, name="b")
        fake(FakeClient([_work(version=7)]))

        code = run(_plain(tmp_path))
        out = capsys.readouterr().out

        assert "Up to date:          0" in out  # NOT reported unchanged
        assert "Version conflict:    1" in out
        assert "Version changed:     0" in out
        assert "No version recorded: 0" in out
        # The operator is told which source holds which value.
        assert "source-provenance/a.json records v3" in out
        assert "source-provenance/b.json records v7" in out
        # No command silently resolves it; the note says a human must reconcile.
        assert "No command resolves this" in out
        assert "Reconcile the sources by hand" in out
        assert code == 1  # a self-contradicting KB is not a healthy exit 0
        assert "None" not in out

    def test_whatever_arxiv_serves_the_conflict_holds(self, tmp_path, fake, capsys):
        # Even when arXiv serves a value matching neither recorded version, the KB
        # disagreeing with itself is the signal — not a comparison against arXiv.
        _seed_sidecar(tmp_path, 3, name="a")
        _seed_sidecar(tmp_path, 7, name="b")
        fake(FakeClient([_work(version=9)]))

        run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "Version conflict:    1" in out
        assert "arXiv now serves v9" in out


# --------------------------------------------------------------------------- #
# The front-matter fold (#117's second consumer)
# --------------------------------------------------------------------------- #
class TestTwoFrontMattersDisagree:
    def test_it_conflicts_just_like_two_sidecars(self, tmp_path, fake, capsys):
        # Before any sidecar exists. The front-matter fold used first-wins; the paper's
        # meaning must be the same here as after a backfill turns these into sidecars,
        # so the report may not change because a ledger came into existence (#117).
        _seed_front_matter(tmp_path, 3, name="a")
        _seed_front_matter(tmp_path, 7, name="b")
        fake(FakeClient([_work(version=7)]))

        code = run(_plain(tmp_path))
        out = capsys.readouterr().out

        assert "Version conflict:    1" in out
        assert "Up to date:          0" in out
        assert "Version changed:     0" in out
        assert "sources/a.md records v3" in out
        assert "sources/b.md records v7" in out
        assert code == 1
        assert "None" not in out


class TestOneOfEach:
    """The Done-when case #137 spells out: `a.json: v3` (an arXiv **version ledger**
    sidecar) beside a front-matter-only `.md` recording a different version. An arXiv
    ledger fills `slots`, and the front-matter loop used to skip the whole id — dropping
    the sidecar-less `.md`'s version silently, so `sidecar-v3 + fm-v7` read `changed(v3)`
    and, worse, `sidecar-v7 + fm-v3` read `unchanged`: the exact `unchanged` misreport this
    issue exists to kill, in a place the two-sidecar and two-front-matter tests never reach.

    These tests hit that path directly (`_seed_sidecar`, an arXiv record with a version,
    puts the id in `slots`). Before the fix they fail; the fix joins the sidecar-less `.md`'s
    front-matter version into the ledger slot's fold.
    """

    def test_a_version_ledger_sidecar_beside_a_front_matter_only_md_conflicts(
        self, tmp_path, fake, capsys
    ):
        # a.json records v3 (a real arXiv version ledger), b.md is front-matter-only v7.
        _seed_sidecar(tmp_path, 3, name="a")
        _seed_front_matter(tmp_path, 7, name="b")
        fake(FakeClient([_work(version=7)]))

        code = run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "Version conflict:    1" in out
        assert "Version changed:     0" in out  # NOT folded to a plain v3->v7 change
        assert "source-provenance/a.json records v3" in out
        assert "sources/b.md records v7" in out
        assert code == 1
        assert "None" not in out

    def test_the_silent_direction_sidecar_high_front_matter_low_is_not_unchanged(
        self, tmp_path, fake, capsys
    ):
        # a.json records v7, b.md is front-matter-only v3, arXiv serves v7. The old skip
        # reported `unchanged` — the paper's own b.md three versions behind vanishing. It
        # must be a conflict, naming both sources.
        _seed_sidecar(tmp_path, 7, name="a")
        _seed_front_matter(tmp_path, 3, name="b")
        fake(FakeClient([_work(version=7)]))

        code = run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "Up to date:          0" in out  # NOT reported unchanged
        assert "Version conflict:    1" in out
        assert "source-provenance/a.json records v7" in out
        assert "sources/b.md records v3" in out
        assert code == 1
        assert "None" not in out

    def test_a_version_ledger_agreeing_with_front_matter_is_not_a_conflict(
        self, tmp_path, fake, capsys
    ):
        # The guard against a false positive: a.json v7 and a front-matter-only b.md v7
        # agree, so the paper stays `unchanged` — the sidecar-less `.md` joining the fold
        # must not manufacture a conflict when the values match.
        _seed_sidecar(tmp_path, 7, name="a")
        _seed_front_matter(tmp_path, 7, name="b")
        fake(FakeClient([_work(version=7)]))

        run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "Up to date:          1" in out
        assert "Version conflict" not in out

    def test_a_non_arxiv_sidecar_beside_a_front_matter_only_md_conflicts(
        self, tmp_path, fake, capsys
    ):
        # A second flavour, both through the front-matter fold: a.md carries a sidecar
        # holding only a non-arXiv (OpenAlex) record and states its version in front matter
        # (v3); b.md is front-matter-only (v7). Neither contributes an arXiv *ledger* record,
        # so the id never enters `slots` and both flow through the front-matter fold.
        (tmp_path / "sources").mkdir()
        a = tmp_path / "sources" / "a.md"
        a.write_text(f"---\narxiv_id: {ID}\narxiv_version: 3\n---\n# a\n", encoding="utf-8")
        write_provenance(sidecar_path(a, tmp_path), Provenance(records=[
            SourceRecord(type="openalex", id="W1",
                         imported_at="2026-01-01T00:00:00+00:00")]))
        _seed_front_matter(tmp_path, 7, name="b")
        fake(FakeClient([_work(version=7)]))

        code = run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "Version conflict:    1" in out
        assert "sources/a.md records v3" in out
        assert "sources/b.md records v7" in out
        assert code == 1


def _seed_versionless_sidecar(kb, *, name, arxiv_id=ID):
    """A ``.md`` whose arXiv provenance sidecar carries **no** ``version`` (a hand-edited or
    externally produced ledger — since #113 no importer writes a version-less one). The
    front matter carries no version either."""
    (kb / "sources").mkdir(exist_ok=True)
    md = kb / "sources" / f"{name}.md"
    md.write_text(f"---\narxiv_id: {arxiv_id}\n---\n# {name}\n", encoding="utf-8")
    write_provenance(sidecar_path(md, kb), Provenance(records=[
        SourceRecord(type="arxiv", id=arxiv_id, imported_at="2026-01-01T00:00:00+00:00",
                     fields={"submitted": "2017-06-12"})]))
    return md


class TestAVersionlessLedgerBesideAVersionedFrontMatterIsNotAConflict:
    """A version-*less* source does not disagree with a versioned one — it is claiming no
    version at all — so this is not #137's conflict. But which single state results differs
    by path, and that difference is *intended*, so it is pinned here rather than left to be
    mistaken for a bug (#134's lesson) or silently reverted.

    A version-less arXiv **ledger** sidecar beside a versioned front-matter-only ``.md``
    folds to that one present version: `_fold_recorded` derives the sole recorded value
    (`_DERIVE_SINGLE`) because a sidecar slot carries no first-source `single`. The pure
    front-matter path keeps its first-source-wins answer instead, so a version-less ``.md``
    sorting first holds the fold at `no-version`. The two are deliberately not the same:
    tracking the ledger case across a backfill, `unchanged` is the answer both before the
    ledger existed (the id was covered by the sidecar all along) and after, so #117's
    "reads the same before and after" holds only for the ledger-derived value — matching the
    version-less ledger to the version-less-`.md`'s `no-version` would reintroduce exactly
    the meaning-flip #117 forbids. (On `main` this case reported `no-version`; these tests
    pin the change and its reason.)
    """

    def test_a_versionless_ledger_beside_a_versioned_front_matter_folds_to_that_version(
        self, tmp_path, fake, capsys
    ):
        # a.json holds an arXiv record with NO version; b.md is front-matter-only v7; arXiv
        # serves v7. The one present version stands: `unchanged`, not a conflict, not
        # `no-version`.
        _seed_versionless_sidecar(tmp_path, name="a")
        _seed_front_matter(tmp_path, 7, name="b")

        (entry,), _ = cv.collect_ledger_entries(tmp_path)
        assert entry.recorded_version == 7
        assert entry.version_disagreement == ()

        fake(FakeClient([_work(version=7)]))
        code = run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "Up to date:          1" in out
        assert "No version recorded: 0" in out
        assert "Version conflict" not in out
        assert code == 0

    def test_the_symmetric_pure_front_matter_path_keeps_no_version(
        self, tmp_path, fake, capsys
    ):
        # The same logical shape — a version-less source beside a v7 one — but both are pure
        # front matter and the version-less `.md` sorts first. First-source-wins holds it at
        # `no-version` (recorded_version=None), the pre-#137 behaviour, untouched. This is
        # what makes the ledger case above a deliberate divergence, not an accident.
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "a.md").write_text(  # no version, sorts first
            f"---\narxiv_id: {ID}\n---\n# a\n", encoding="utf-8")
        _seed_front_matter(tmp_path, 7, name="b")

        (entry,), _ = cv.collect_ledger_entries(tmp_path)
        assert entry.recorded_version is None
        assert entry.version_disagreement == ()
        # It is a front-matter paper (no ledger), so the first source wins its version.
        assert cv.provenance_of(entry.sources) == "front-matter"

        fake(FakeClient([_work(version=7)]))
        run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "No version recorded: 1" in out
        assert "Up to date:          0" in out
        assert "Version conflict" not in out


# --------------------------------------------------------------------------- #
# No command resolves it
# --------------------------------------------------------------------------- #
class TestAutoUpdateDoesNotResolveIt:
    def test_auto_update_writes_nothing_for_a_conflict(self, tmp_path, fake, capsys):
        # --auto-update fills a *version-less* record (a value a refresh legitimately
        # learned), but a conflict has two recorded values and choosing between them is a
        # guess a refresh has no authority to make. Both sidecars stay byte-identical.
        a = _seed_sidecar(tmp_path, 3, name="a")
        b = _seed_sidecar(tmp_path, 7, name="b")
        before = {p: sidecar_path(p, tmp_path).read_bytes() for p in (a, b)}
        fake(FakeClient([_work(version=7)]))

        code = run([*_plain(tmp_path), "--auto-update"])
        out = capsys.readouterr().out

        for p in (a, b):
            assert sidecar_path(p, tmp_path).read_bytes() == before[p]  # nothing written
        assert "Ledgers updated:     1" not in out
        assert "Version conflict:    1" in out  # still surfaced under --auto-update
        assert code == 1


# --------------------------------------------------------------------------- #
# The porcelain machine contract
# --------------------------------------------------------------------------- #
class TestThePorcelainContract:
    def test_the_check_row_and_conditional_tally(self, tmp_path, fake, capsys):
        _seed_sidecar(tmp_path, 3, name="a")
        _seed_sidecar(tmp_path, 7, name="b")
        fake(FakeClient([_work(version=7)]))

        run([*_plain(tmp_path), "--porcelain"])
        lines = capsys.readouterr().out.splitlines()

        # status is a new value in the existing status column; recorded is empty (no one
        # value); reason names each source and the value it holds.
        assert (
            f"check\t{ID}\tversion-conflict\t\t7\t\t0\t"
            "source-provenance/a.json=v3; source-provenance/b.json=v7\t0"
        ) in lines
        assert "version_conflict\t1" in lines
        assert "unchanged\t0" in lines
        assert "None" not in "\n".join(lines)

    def test_a_kb_with_no_conflict_has_no_version_conflict_row(self, tmp_path, fake, capsys):
        # The byte-identical guarantee: the tally row appears only when a conflict exists,
        # exactly like `updated` appears only under --auto-update.
        _seed_sidecar(tmp_path, 5, name="a")
        fake(FakeClient([_work(version=7)]))

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
        assert not any("version_conflict" in line for line in lines)

    def test_a_tab_in_a_source_path_cannot_add_a_column(self, tmp_path, fake, capsys):
        # The conflict `reason` carries source *paths* (`src=vN; src=vM`), a caller-influenced
        # value. A tab in a filename would, unneutralized, split the row and shift every
        # column after `reason` — the exact positional break #141 closed for every other
        # porcelain field. `reason` goes through `porcelain_field`, so a tabbed path becomes a
        # space and the row keeps its 9 fields. This fails if the conflict `reason` is emitted
        # raw. (Tab is a legal filename byte on POSIX; only `/` and NUL are not.)
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "a\tTABBED.md").write_text(
            f"---\narxiv_id: {ID}\narxiv_version: 3\n---\n# a\n", encoding="utf-8"
        )
        _seed_front_matter(tmp_path, 7, name="b")
        fake(FakeClient([_work(version=7)]))

        run([*_plain(tmp_path), "--porcelain"])
        lines = capsys.readouterr().out.splitlines()

        check_rows = [line for line in lines if line.startswith("check\t")]
        assert len(check_rows) == 1
        fields = check_rows[0].split("\t")
        assert len(fields) == 9  # the documented column count, tab in the path notwithstanding
        assert fields[2] == "version-conflict"
        # The tabbed path survives as text in the reason, with its tab flattened to a space.
        assert "sources/a TABBED.md=v3" in fields[7]
        assert "\t" not in fields[7]
    def test_the_human_summary_has_no_version_conflict_line(self, tmp_path, fake, capsys):
        _seed_sidecar(tmp_path, 5, name="a")
        fake(FakeClient([_work(version=7)]))

        run(_plain(tmp_path))
        out = capsys.readouterr().out
        assert "Version conflict" not in out
