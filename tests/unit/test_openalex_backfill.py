# SPDX-License-Identifier: Apache-2.0
"""The OpenAlex backfill schema — what it records, and what it refuses (#115, #105).

What is pinned here:

* **Full recoverability.** A backfilled record is *field for field* what
  ``OpenAlexSourceWriter._provenance_record`` would have written, ``imported_at`` included.
  This is checked against the real writer's own output, not a transcription of it, so the
  claim cannot rot when the writer changes. arXiv cannot make this claim (``submitted`` is
  not in its front matter); OpenAlex loses nothing.
* **``is_retracted`` keeps the import's shape:** ``True`` or absent, never a literal
  ``False`` — absence from the JSON *means* not retracted, and a ``False`` would change the
  bytes.
* **No coercion of the retraction flag.** Front matter has no booleans: ``read_scalars``
  strips quotes, so ``openalex_is_retracted: true`` and ``: "true"`` are the same document.
  Anything that is not a YAML boolean word — ``1``, ``yes``, ``on`` — is promoted into the
  record *verbatim*, and ``common/backfill.py``'s ``signal_field_error`` guard (#109)
  refuses that paper by id. It is neither dropped (which would assert OpenAlex does not
  flag the paper) nor coerced (which would assert a retraction no source made). Every other
  paper is backfilled normally.
* **``required`` is empty**, because ``OpenAlexSourceWriter._IDENTIFYING_FIELDS`` is empty:
  the false conflict arXiv's ``required=("version",)`` prevents cannot arise here.
* **The collection is not re-derived**: the schema is bound to ``refresh.provenance_of``
  itself, and its ``collect_entries`` calls ``refresh.collect_ledger_entries``.
* **No network**, and **no ``.md`` opened for write** (P4).
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

import factlog.integrations.common.backfill as bf
import factlog.integrations.openalex.backfill as ob
from factlog.integrations.common.provenance import read_provenance, sidecar_path
from factlog.integrations.openalex import refresh
from factlog.integrations.openalex.source_writer import OpenAlexSourceWriter
from factlog.integrations.openalex.work_parser import ParsedWork

_STAMP = "2025-01-01T00:00:00Z"


@pytest.fixture
def kb(tmp_path: Path) -> Path:
    (tmp_path / "sources").mkdir()
    return tmp_path


def _source(kb: Path, name: str, front_matter: str) -> Path:
    path = kb / "sources" / name
    path.write_text(f"---\n{front_matter}---\n\n# A paper\n", encoding="utf-8")
    return path


def _run(kb: Path, dry_run: bool = False) -> dict[str, bf.BackfillResult]:
    return {r.entry_id: r for r in bf.backfill(kb, ob.backfill_schema(), dry_run=dry_run)}


def _records(kb: Path, md_name: str) -> list[dict]:
    sidecar = sidecar_path(kb / "sources" / md_name)
    return [r.to_dict() for r in read_provenance(sidecar).records]


class TestABackfilledRecordIsWhatTheImportWouldHaveWritten:
    """Field for field, against the writer's own ``_provenance_record`` — the asymmetry
    with arXiv, whose ``submitted`` no front matter carries."""

    @pytest.mark.parametrize("retracted", [False, True])
    def test_it_equals_the_writers_own_record(self, kb: Path, retracted: bool):
        parsed = ParsedWork(
            openalex_id="W3046275966",
            title="Dementia prevention, intervention, and care",
            authors=("Gill Livingston",),
            year=2020,
            journal="The Lancet",
            doi="10.1016/S0140-6736(20)30367-6",
            work_type="article",
            openalex_is_retracted=retracted,
        )
        writer = OpenAlexSourceWriter()
        # The real front matter this importer emits, written by hand into the KB exactly as
        # a pre-#84 import left it: a `.md` and no sidecar.
        md = _source(kb, "livingston.md", writer._front_matter(parsed, _STAMP)[4:-4])

        assert _run(kb)["W3046275966"].status == bf.BACKFILL_WRITTEN

        expected = writer._provenance_record(parsed, _STAMP).to_dict()
        assert _records(kb, "livingston.md") == [expected]
        assert expected["imported_at"] == _STAMP
        assert md.read_text(encoding="utf-8").startswith("---\n")

    def test_is_retracted_is_true_or_absent_never_false(self, kb: Path):
        _source(kb, "plain.md", f'openalex_id: "W1"\ntype: "article"\nimported_at: "{_STAMP}"\n')
        _source(
            kb, "flagged.md",
            f'openalex_id: "W2"\ntype: "article"\nimported_at: "{_STAMP}"\n'
            "openalex_is_retracted: true\n",
        )
        _run(kb)

        (plain,) = _records(kb, "plain.md")
        assert "is_retracted" not in plain  # absence *means* not retracted

        (flagged,) = _records(kb, "flagged.md")
        assert flagged["is_retracted"] is True  # a real bool, not the string "true"

    def test_a_front_matter_field_the_md_lacks_is_omitted_not_nulled(self, kb: Path):
        _source(kb, "bare.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\n')
        _run(kb)
        (record,) = _records(kb, "bare.md")
        assert record == {"type": "openalex", "id": "W1", "imported_at": _STAMP}


class TestTheRetractionFlagIsNeverCoerced:
    """``openalex_is_retracted`` is OpenAlex's opinion. The reader is a line reader, not a
    YAML parser, so what arrives is a string; only YAML's boolean words are interpreted."""

    def test_a_quoted_true_is_the_same_document_as_a_bare_true(self, kb: Path):
        _source(kb, "bare.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: true\n')
        _source(kb, "quoted.md", f'openalex_id: "W2"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: "true"\n')
        results = _run(kb)
        assert results["W1"].status == bf.BACKFILL_WRITTEN
        assert results["W2"].status == bf.BACKFILL_WRITTEN
        assert _records(kb, "bare.md")[0]["is_retracted"] is True
        assert _records(kb, "quoted.md")[0]["is_retracted"] is True

    @pytest.mark.parametrize("literal", ["TRUE", "True", "false", "False"])
    def test_yaml_boolean_words_are_interpreted_in_any_case(self, kb: Path, literal: str):
        _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: {literal}\n')
        assert _run(kb)["W1"].status == bf.BACKFILL_WRITTEN
        (record,) = _records(kb, "a.md")
        assert record.get("is_retracted", False) is (literal.lower() == "true")

    @pytest.mark.parametrize("literal", ["1", "0", "yes", "no", "on", "off", "y", "n", "maybe"])
    def test_a_non_boolean_flag_is_refused_by_id_and_nothing_is_written(
        self, kb: Path, literal: str
    ):
        md = _source(
            kb, "hand-edited.md",
            f'openalex_id: "W1"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: {literal}\n',
        )
        before = os.stat(md).st_mtime_ns

        results = _run(kb)

        # The shared writer's signal-field guard names the field in OpenAlex's own words.
        assert results["W1"].status == bf.BACKFILL_REFUSED
        assert "is_retracted" in results["W1"].reason
        assert repr(literal) in results["W1"].reason
        # Neither true nor false was guessed at: no ledger exists at all.
        assert not (kb / "source-provenance").exists()
        assert os.stat(md).st_mtime_ns == before

    def test_the_raw_value_reaches_the_record_verbatim(self, kb: Path):
        """Not dropped, not coerced. The schema promotes it; the shared writer refuses it."""
        _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: yes\n')
        (entry,) = [e for e in ob._collect_entries(kb)[0]]
        assert entry.is_retracted == "yes"

    def test_one_bad_paper_does_not_block_its_neighbours(self, kb: Path):
        """The refusal is per-id: a hand-edited flag on one paper costs only that paper."""
        _source(kb, "clean.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\n')
        _source(kb, "dirty.md", f'openalex_id: "W2"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: yes\n')

        results = _run(kb)

        assert results["W1"].status == bf.BACKFILL_WRITTEN
        assert results["W2"].status == bf.BACKFILL_REFUSED
        assert sidecar_path(kb / "sources" / "clean.md").exists()
        assert not sidecar_path(kb / "sources" / "dirty.md").exists()

    def test_a_ledger_backed_papers_flag_is_not_inspected(self, kb: Path):
        """Only a front-matter-only paper's flag can reach a ledger through a backfill. A
        paper that already has a ledger is skipped before any field is read, so a stray
        value in its (now-advisory) front matter is never judged."""
        _source(
            kb, "has-ledger.md",
            f'openalex_id: "W1"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: yes\n',
        )
        sidecar = sidecar_path(kb / "sources" / "has-ledger.md")
        sidecar.parent.mkdir()
        sidecar.write_text(
            '{"schema_version": 1, "records": [{"type": "openalex", "id": "W1", '
            f'"imported_at": "{_STAMP}"}}]}}\n',
            encoding="utf-8",
        )
        _source(kb, "clean.md", f'openalex_id: "W2"\nimported_at: "{_STAMP}"\n')

        results = _run(kb)

        assert "W1" not in results  # classified "ledger", skipped entirely
        assert results["W2"].status == bf.BACKFILL_WRITTEN


class TestRefusalsAndNoOps:
    def test_a_paper_without_imported_at_is_refused_and_nothing_is_written(self, kb: Path):
        _source(kb, "a.md", 'openalex_id: "W1"\ntype: "article"\n')
        result = _run(kb)["W1"]
        assert result.status == bf.BACKFILL_REFUSED
        assert "imported_at" in result.reason
        assert not (kb / "source-provenance").exists()

    def test_a_second_backfill_is_a_byte_and_mtime_identical_no_op(self, kb: Path):
        _source(kb, "a.md", f'openalex_id: "W1"\ntype: "article"\nimported_at: "{_STAMP}"\n')
        _run(kb)
        sidecar = sidecar_path(kb / "sources" / "a.md")
        before_bytes, before_mtime = sidecar.read_bytes(), os.stat(sidecar).st_mtime_ns

        second = _run(kb)

        assert "W1" not in second  # now ledger-backed: skipped, never re-written
        assert sidecar.read_bytes() == before_bytes
        assert os.stat(sidecar).st_mtime_ns == before_mtime

    def test_a_neighbours_record_is_left_alone(self, kb: Path):
        """A pre-ledger cross-source merge may already carry the arXiv record."""
        _source(
            kb, "a.md",
            f'openalex_id: "W1"\narxiv_id: "2301.00001"\ntype: "article"\nimported_at: "{_STAMP}"\n',
        )
        sidecar = sidecar_path(kb / "sources" / "a.md")
        sidecar.parent.mkdir()
        sidecar.write_text(
            '{"schema_version": 1, "records": [{"type": "arxiv", "id": "2301.00001", '
            '"imported_at": "2024-01-01T00:00:00Z", "version": 3}]}\n',
            encoding="utf-8",
        )

        assert _run(kb)["W1"].status == bf.BACKFILL_WRITTEN

        records = {r["type"]: r for r in _records(kb, "a.md")}
        assert records["arxiv"] == {
            "type": "arxiv", "id": "2301.00001",
            "imported_at": "2024-01-01T00:00:00Z", "version": 3,
        }
        assert records["openalex"]["imported_at"] == _STAMP

    def test_a_corrupt_sidecar_is_a_per_id_error_not_a_crash(self, kb: Path):
        _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\n')
        (kb / "source-provenance").mkdir()
        (kb / "source-provenance" / "a.json").write_text("{ not json", encoding="utf-8")

        results = _run(kb)

        assert [r.status for r in results.values()] == [bf.BACKFILL_ERROR]


class TestTheSchemaItself:
    def test_required_is_empty_because_openalex_declares_no_identifying_field(self):
        assert OpenAlexSourceWriter._IDENTIFYING_FIELDS == ()
        assert ob.backfill_schema().required == ()

    def test_the_fields_are_exactly_the_ledger_record_s(self):
        assert set(ob.backfill_schema().fields) == {"doi", "work_type", "journal", "is_retracted"}

    def test_membership_is_decided_by_openalex_s_own_predicate(self):
        assert ob.backfill_schema().provenance_of is refresh.provenance_of

    def test_the_collector_wraps_openalex_s_own_collect_ledger_entries(self, kb, monkeypatch):
        seen: list[Path] = []
        real = refresh.collect_ledger_entries
        monkeypatch.setattr(
            refresh, "collect_ledger_entries",
            lambda root: (seen.append(Path(root)), real(root))[1],
        )
        _run(kb)
        assert seen == [kb]

    def test_the_module_imports_no_api_client(self):
        """A backfill that queried upstream would be a refresh, whose write is
        ``update_source``. This module has no import that could reach the network."""
        source = Path(ob.__file__).read_text(encoding="utf-8")
        imports = [
            line for line in source.splitlines()
            if line.startswith(("import ", "from "))
        ]
        assert imports and not any("client" in line for line in imports)

    def test_the_front_matter_key_is_source_scoped_and_never_a_bare_retracted(self):
        """``is_retracted`` is OpenAlex's claim about the world, not the KB's (#51). The
        key names its source; a bare ``retracted:`` would launder the attribution away."""
        assert ob.RETRACTION_KEY == "openalex_is_retracted"

    def test_a_refusal_never_translates_openalex_s_vocabulary(self, kb: Path):
        """arXiv says "withdrawn"; OpenAlex says "retracted". Neither may be reworded into
        the other's word, for the same reason a triple is not translated."""
        _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: yes\n')
        _source(kb, "b.md", 'openalex_id: "W2"\n')  # no imported_at
        reasons = " ".join(r.reason for r in _run(kb).values()).lower()
        assert "withdrawn" not in reasons


class TestBackfillAndRefreshShareOneParser:
    """The boolean words live in exactly one function. Were they written down twice,
    widening one copy to YAML 1.1's ``yes``/``on`` would make ``openalex-refresh`` report a
    paper retracted that this command still refuses a ledger — and a retraction that can
    never be acknowledged repeats forever, the failure #105 exists to end (#64/#98/#111)."""

    def test_the_schema_calls_refresh_s_parser(self, kb: Path, monkeypatch):
        seen: list[str] = []
        real = refresh.parse_retraction_flag
        monkeypatch.setattr(
            refresh, "parse_retraction_flag",
            lambda raw: (seen.append(raw), real(raw))[1],
        )
        _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: true\n')
        _run(kb)
        assert "true" in seen

    def test_widening_the_parser_moves_both_callers_at_once(self, kb: Path, monkeypatch):
        """The drift this design forecloses, exercised rather than asserted. With ``yes``
        unrecognised, refresh compares it as not-retracted and backfill refuses the paper.
        Widen the one parser and *both* read it as a retraction: refresh's entry says so, and
        the backfill records it. Neither can move without the other."""
        _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: yes\n')

        before, _ = refresh.collect_ledger_entries(kb)
        assert before[0].recorded_is_retracted is False        # refresh: not retracted
        assert _run(kb)["W1"].status == bf.BACKFILL_REFUSED    # backfill: refuses

        real = refresh.parse_retraction_flag
        monkeypatch.setattr(
            refresh, "parse_retraction_flag",
            lambda raw: True if raw.strip().lower() == "yes" else real(raw),
        )

        after, _ = refresh.collect_ledger_entries(kb)
        assert after[0].recorded_is_retracted is True          # refresh: retracted
        assert _run(kb)["W1"].status == bf.BACKFILL_WRITTEN    # backfill: records it
        assert _records(kb, "a.md")[0]["is_retracted"] is True

    def test_a_front_matter_only_entry_has_exactly_one_source(self, kb: Path):
        """``_retraction_value`` reads one ``.md`` and refuses to pick a winner among several.
        ``collect_ledger_entries``' front-matter branch fills a slot only for an id no ledger
        and no earlier source covered, so there is exactly one. When #112 or #117 gives an
        entry several sources this goes red — and the per-source flag must then be plumbed
        through the shared writer, not guessed at."""
        _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\n')
        _source(kb, "b-same-id.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\n')
        entries, _ = refresh.collect_ledger_entries(kb)
        front_matter_only = [
            e for e in entries if refresh.provenance_of(e.sources) == "front-matter"
        ]
        assert front_matter_only
        assert all(len(e.sources) == 1 for e in front_matter_only)


class TestRefusingIsTheOnlyTimeSuchAValueIsHeardFrom:
    """The refusal is not a deferral. ``openalex-refresh`` narrows the same parse to a bool,
    so a hand-typed ``yes`` is compared as *not retracted* and surfaces nothing, ever."""

    def test_refresh_reads_an_unparseable_flag_as_not_retracted(self, kb: Path):
        _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: yes\n')
        (entry,) = refresh.collect_ledger_entries(kb)[0]
        # Not True. With OpenAlex also not flagging the work, `_diff` computes
        # newly_retracted = current and not recorded = False, and un_retracted = False:
        # neither signal fires, so nothing is ever reported for this paper.
        assert entry.recorded_is_retracted is False


class TestAnEmptyFlagIsAbsence:
    """N1: ``: ""`` cannot be told from an absent key by ``_key_pattern`` (its capture group
    needs a non-quote character), while ``: ''`` arrives as the two-character token. Both are
    empty once unquoted, and both mean absence — one rule, not two outcomes."""

    @pytest.mark.parametrize("literal", ['""', "''"])
    def test_both_spellings_of_empty_backfill_as_not_retracted(self, kb: Path, literal: str):
        _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: {literal}\n')
        assert _run(kb)["W1"].status == bf.BACKFILL_WRITTEN
        assert "is_retracted" not in _records(kb, "a.md")[0]

    @pytest.mark.parametrize("literal", ['""', "''"])
    def test_refresh_agrees(self, kb: Path, literal: str):
        _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: {literal}\n')
        (entry,) = refresh.collect_ledger_entries(kb)[0]
        assert entry.recorded_is_retracted is False


class TestPreviewAgreesWithTheRun:
    """``--dry-run`` shares one classifier with the real run, so it can never predict
    ``WRITTEN`` for a paper the run refuses, nor a refusal the run would write."""

    def test_every_id_is_classified_identically(self, kb: Path):
        _source(kb, "a_ok.md", f'openalex_id: "W1"\ntype: "article"\nimported_at: "{_STAMP}"\n')
        _source(kb, "b_no_stamp.md", 'openalex_id: "W2"\ntype: "article"\n')
        _source(
            kb, "c_flagged.md",
            f'openalex_id: "W3"\njournal: "The Lancet"\nimported_at: "{_STAMP}"\n'
            "openalex_is_retracted: true\n",
        )
        _source(kb, "d_has_ledger.md", f'openalex_id: "W4"\nimported_at: "{_STAMP}"\n')
        sidecar = sidecar_path(kb / "sources" / "d_has_ledger.md")
        sidecar.parent.mkdir()
        sidecar.write_text(
            '{"schema_version": 1, "records": [{"type": "openalex", "id": "W4", '
            f'"imported_at": "{_STAMP}"}}]}}\n',
            encoding="utf-8",
        )

        preview = {i: r.status for i, r in _run(kb, dry_run=True).items()}
        real = {i: r.status for i, r in _run(kb).items()}

        assert preview == real
        assert preview["W1"] == bf.BACKFILL_WRITTEN
        assert preview["W2"] == bf.BACKFILL_REFUSED  # no imported_at
        assert preview["W3"] == bf.BACKFILL_WRITTEN
        assert "W4" not in preview  # already ledger-backed

    def test_an_uninterpretable_flag_is_refused_in_both(self, kb: Path):
        """The signal-field guard sits before ``_backfill_source``'s ``if dry_run:`` early
        return, so the preview cannot promise a ledger the run would refuse."""
        _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\n')
        _source(kb, "b.md", f'openalex_id: "W2"\nimported_at: "{_STAMP}"\nopenalex_is_retracted: 1\n')

        preview = {i: r.status for i, r in _run(kb, dry_run=True).items()}
        assert not (kb / "source-provenance").exists()
        real = {i: r.status for i, r in _run(kb).items()}

        assert preview == real == {"W1": bf.BACKFILL_WRITTEN, "W2": bf.BACKFILL_REFUSED}

    def test_a_preview_writes_nothing(self, kb: Path):
        md = _source(kb, "a.md", f'openalex_id: "W1"\nimported_at: "{_STAMP}"\n')
        before = os.stat(md).st_mtime_ns
        _run(kb, dry_run=True)
        assert not (kb / "source-provenance").exists()
        assert os.stat(md).st_mtime_ns == before
