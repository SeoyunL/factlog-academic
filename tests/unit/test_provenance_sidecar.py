# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the provenance sidecar reader/writer (issue #63, Step 4a).

Covers the naming rule, round-trip, idempotent/conflicting adds, byte
determinism, missing/corrupt files, atomic-write behaviour, and — via the real
CLI — that a ``source-provenance/`` directory is invisible to source
enumeration. No sidecar is created by any production code path in this step;
these tests exercise the module directly.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from factlog import cli
from factlog.integrations.common import provenance as prov
from factlog.integrations.common.provenance import (
    SCHEMA_VERSION,
    SIDECAR_DIR,
    Provenance,
    ProvenanceConflict,
    ProvenanceError,
    SourceRecord,
    add_source,
    is_sidecar,
    read_provenance,
    sidecar_path,
    write_provenance,
)

# Fixed, caller-supplied timestamps — the module must never read a clock, so
# reusing these keeps every write byte-reproducible.
T_ARXIV = "2026-07-09T00:00:00Z"
T_OPENALEX = "2026-07-08T00:00:00Z"


def _two_source_prov() -> Provenance:
    """The canonical two-source example from the issue: one arXiv, one OpenAlex."""
    p = Provenance()
    add_source(p, SourceRecord("arxiv", "2311.09277", T_ARXIV, {
        "version": 2, "submitted": "2023-11-15", "primary_category": "cs.CL",
        "comment": None,  # a None extra must be dropped, not written as null
    }))
    add_source(p, SourceRecord("openalex", "W4392847", T_OPENALEX))
    return p


# --- naming rule -------------------------------------------------------------

class TestSidecarPath:
    def test_md_source_maps_to_sibling_json(self, tmp_path):
        src = tmp_path / "sources" / "foo.md"
        assert sidecar_path(src, tmp_path) == tmp_path / SIDECAR_DIR / "foo.json"

    def test_kb_root_is_parent_of_sources(self, tmp_path):
        # The sidecar dir sits beside sources/, not inside it.
        src = tmp_path / "sources" / "foo.md"
        result = sidecar_path(src, tmp_path)
        assert result.parent == tmp_path / SIDECAR_DIR
        assert result.parent.parent == tmp_path

    def test_multidot_stem_replaces_only_final_suffix(self, tmp_path):
        src = tmp_path / "sources" / "foo.provenance.md"
        assert sidecar_path(src, tmp_path) == tmp_path / SIDECAR_DIR / "foo.provenance.json"

    def test_non_md_source(self, tmp_path):
        src = tmp_path / "sources" / "report.pdf"
        assert sidecar_path(src, tmp_path) == tmp_path / SIDECAR_DIR / "report.json"

    def test_accepts_str(self, tmp_path):
        src = str(tmp_path / "sources" / "foo.md")
        assert sidecar_path(src, str(tmp_path)) == tmp_path / SIDECAR_DIR / "foo.json"


class TestIsSidecar:
    def test_true_for_json_in_sidecar_dir(self, tmp_path):
        assert is_sidecar(tmp_path / SIDECAR_DIR / "foo.json")

    def test_false_for_json_elsewhere(self, tmp_path):
        assert not is_sidecar(tmp_path / "sources" / "foo.json")

    def test_false_for_non_json_in_sidecar_dir(self, tmp_path):
        assert not is_sidecar(tmp_path / SIDECAR_DIR / "foo.md")

    def test_false_for_source_md(self, tmp_path):
        assert not is_sidecar(tmp_path / "sources" / "foo.md")

    def test_sidecar_path_output_is_a_sidecar(self, tmp_path):
        # The two exports agree: whatever sidecar_path builds, is_sidecar accepts.
        assert is_sidecar(sidecar_path(tmp_path / "sources" / "foo.md", tmp_path))


# --- round-trip --------------------------------------------------------------

class TestRoundTrip:
    def test_write_then_read_is_equal(self, tmp_path):
        path = tmp_path / SIDECAR_DIR / "foo.json"
        original = _two_source_prov()
        write_provenance(path, original)
        loaded = read_provenance(path)
        assert loaded.schema_version == SCHEMA_VERSION
        # Order-independent: compare as a set of flat dicts.
        assert sorted(r.to_dict().items() for r in loaded.records) == \
            sorted(r.to_dict().items() for r in original.records)

    def test_none_extra_is_dropped_not_null(self, tmp_path):
        path = tmp_path / SIDECAR_DIR / "foo.json"
        write_provenance(path, _two_source_prov())
        data = json.loads(path.read_text(encoding="utf-8"))
        arxiv = next(r for r in data["records"] if r["type"] == "arxiv")
        assert "comment" not in arxiv  # None -> absent, never null
        assert arxiv["version"] == 2

    def test_exact_two_source_bytes(self, tmp_path):
        # Pins the on-disk format: sorted records, sorted keys, indent=2, newline.
        path = tmp_path / SIDECAR_DIR / "foo.json"
        write_provenance(path, _two_source_prov())
        expected = (
            "{\n"
            '  "records": [\n'
            "    {\n"
            '      "id": "2311.09277",\n'
            '      "imported_at": "2026-07-09T00:00:00Z",\n'
            '      "primary_category": "cs.CL",\n'
            '      "submitted": "2023-11-15",\n'
            '      "type": "arxiv",\n'
            '      "version": 2\n'
            "    },\n"
            "    {\n"
            '      "id": "W4392847",\n'
            '      "imported_at": "2026-07-08T00:00:00Z",\n'
            '      "type": "openalex"\n'
            "    }\n"
            "  ],\n"
            '  "schema_version": 1\n'
            "}\n"
        )
        assert path.read_text(encoding="utf-8") == expected


# --- determinism -------------------------------------------------------------

class TestDeterminism:
    def test_two_writes_are_byte_identical(self, tmp_path):
        a = tmp_path / SIDECAR_DIR / "a.json"
        b = tmp_path / SIDECAR_DIR / "b.json"
        write_provenance(a, _two_source_prov())
        write_provenance(b, _two_source_prov())
        assert a.read_bytes() == b.read_bytes()

    def test_insertion_order_does_not_affect_bytes(self, tmp_path):
        forward = Provenance()
        add_source(forward, SourceRecord("arxiv", "2311.09277", T_ARXIV, {"version": 2}))
        add_source(forward, SourceRecord("openalex", "W4392847", T_OPENALEX))
        reverse = Provenance()
        add_source(reverse, SourceRecord("openalex", "W4392847", T_OPENALEX))
        add_source(reverse, SourceRecord("arxiv", "2311.09277", T_ARXIV, {"version": 2}))
        fpath = tmp_path / SIDECAR_DIR / "f.json"
        rpath = tmp_path / SIDECAR_DIR / "r.json"
        write_provenance(fpath, forward)
        write_provenance(rpath, reverse)
        assert fpath.read_bytes() == rpath.read_bytes()


# --- add_source semantics ----------------------------------------------------

class TestAddSource:
    def test_appends_new_pair(self):
        p = Provenance()
        add_source(p, SourceRecord("arxiv", "1", T_ARXIV))
        add_source(p, SourceRecord("openalex", "1", T_OPENALEX))
        assert len(p.records) == 2  # same id string, different type -> distinct

    def test_identical_pair_is_noop(self):
        p = Provenance()
        add_source(p, SourceRecord("arxiv", "1", T_ARXIV, {"version": 2}))
        add_source(p, SourceRecord("arxiv", "1", T_ARXIV, {"version": 2}))
        assert len(p.records) == 1

    def test_different_fields_same_key_raises(self):
        p = Provenance()
        add_source(p, SourceRecord("arxiv", "1", T_ARXIV, {"version": 2}))
        with pytest.raises(ProvenanceConflict):
            add_source(p, SourceRecord("arxiv", "1", T_ARXIV, {"version": 3}))
        assert len(p.records) == 1  # rejected write leaves the ledger untouched

    def test_conflict_on_differing_imported_at(self):
        p = Provenance()
        add_source(p, SourceRecord("arxiv", "1", T_ARXIV))
        with pytest.raises(ProvenanceConflict):
            add_source(p, SourceRecord("arxiv", "1", T_OPENALEX))


# --- read robustness ---------------------------------------------------------

class TestReadRobustness:
    def test_missing_file_yields_empty(self, tmp_path):
        result = read_provenance(tmp_path / SIDECAR_DIR / "nope.json")
        assert result.schema_version == SCHEMA_VERSION
        assert result.records == []

    def test_corrupt_json_raises(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(ProvenanceError):
            read_provenance(path)

    def test_non_object_raises(self, tmp_path):
        path = tmp_path / "list.json"
        path.write_text("[1, 2, 3]", encoding="utf-8")
        with pytest.raises(ProvenanceError):
            read_provenance(path)

    def test_record_missing_required_field_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text(json.dumps({"schema_version": 1, "records": [{"type": "arxiv"}]}),
                        encoding="utf-8")
        with pytest.raises(ProvenanceError):
            read_provenance(path)


# --- atomic write ------------------------------------------------------------

class TestAtomicWrite:
    def test_interrupted_write_leaves_no_partial_file(self, tmp_path, monkeypatch):
        # atomic_write_text writes a temp then os.replace()s it. Simulate the
        # replace failing: the target must not exist and no temp may linger.
        import factlog.integrations.common._textio as textio

        def boom(_src, _dst):
            raise OSError("simulated crash before rename completes")

        monkeypatch.setattr(textio.os, "replace", boom)
        path = tmp_path / SIDECAR_DIR / "foo.json"
        with pytest.raises(OSError):
            write_provenance(path, _two_source_prov())
        assert not path.exists()
        # No .foo.json.tmp (or any temp) left behind in the directory.
        assert list(path.parent.iterdir()) == []

    def test_overwrite_is_atomic_and_complete(self, tmp_path):
        path = tmp_path / SIDECAR_DIR / "foo.json"
        write_provenance(path, Provenance(records=[SourceRecord("arxiv", "1", T_ARXIV)]))
        write_provenance(path, _two_source_prov())
        loaded = read_provenance(path)
        assert len(loaded.records) == 2


# --- CLI invisibility (real KB, real CLI) ------------------------------------

_BIB = (
    '---\nzotero_key: "K1"\nitem_type: "journalArticle"\ntitle: "A Study"\n'
    'authors: ["Doe J"]\nyear: "2020"\ndoi: "10.1/x"\n---\n\n# body\n'
)


def _make_kb(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "doe-2020-a-study.md").write_text(_BIB, encoding="utf-8")
    return tmp_path


def _add_sidecar(kb):
    write_provenance(sidecar_path(kb / "sources" / "doe-2020-a-study.md", kb),
                     _two_source_prov())


class TestCliInvisibility:
    """A source-provenance/ directory must not change any command's output.

    Each command is run against a real temp KB with and without a real sidecar
    (built by this module, not a hand-written file), through cli.main — never a
    reimplementation of the command.
    """

    @pytest.mark.parametrize("argv_tail", [
        ["sources"],
        ["status"],          # this is the "coverage" surface (source_files())
        ["export", "--bibtex"],
        ["export", "--csl"],
    ])
    def test_output_identical_with_and_without_sidecar(self, tmp_path, capsys, argv_tail):
        kb = _make_kb(tmp_path)
        argv = [*argv_tail, "--target", str(kb)]
        rc_without = cli.main(argv)
        without = capsys.readouterr()

        _add_sidecar(kb)
        assert (kb / SIDECAR_DIR / "doe-2020-a-study.json").is_file()  # it really exists
        rc_with = cli.main(argv)
        with_ = capsys.readouterr()

        assert rc_without == rc_with
        assert without.out == with_.out
        assert without.err == with_.err

    def test_sources_still_reports_one_source(self, tmp_path, capsys):
        kb = _make_kb(tmp_path)
        _add_sidecar(kb)
        rc = cli.main(["sources", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "1 source(s)" in out
        assert SIDECAR_DIR not in out  # the ledger is never listed as a source


# --- import weight -----------------------------------------------------------

def test_import_factlog_does_not_load_provenance():
    """`import factlog` must not pull in the provenance module (or json via it):
    nothing in this step wires it into a production import path."""
    code = (
        "import sys; import factlog; "
        "assert 'factlog.integrations.common.provenance' not in sys.modules, "
        "'provenance was imported by import factlog'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_module_exports():
    # The two names later steps depend on are public on the module.
    assert prov.SIDECAR_DIR == "source-provenance"
    assert callable(prov.is_sidecar)


class TestSidecarPathAnchoring:
    """`sources/a/x.md` and `sources/b/x.md` must not share one ledger.

    The enumerators use `rglob`, and `ingest` mirrors an original's subtree, so a
    source can legitimately sit in a subdirectory. A stem-only mapping put both
    of these at `source-provenance/x.json` — and, worse, put it *inside*
    `sources/`, the one place the sidecar must never be.
    """

    def test_nested_sources_do_not_collide(self, tmp_path):
        a = prov.sidecar_path(tmp_path / "sources" / "a" / "x.md", tmp_path)
        b = prov.sidecar_path(tmp_path / "sources" / "b" / "x.md", tmp_path)
        assert a != b
        assert a == tmp_path / prov.SIDECAR_DIR / "a" / "x.json"

    def test_a_nested_source_sidecar_stays_out_of_sources(self, tmp_path):
        path = prov.sidecar_path(tmp_path / "sources" / "deep" / "er" / "x.md", tmp_path)
        assert "sources" not in path.relative_to(tmp_path).parts

    def test_flat_source_is_unchanged(self, tmp_path):
        assert prov.sidecar_path(tmp_path / "sources" / "x.md", tmp_path) == (
            tmp_path / prov.SIDECAR_DIR / "x.json"
        )

    def test_runs_sources_has_no_sidecar_at_all(self, tmp_path):
        # `runs/sources/` is the other SOURCE_ROOT, and #112 gives it NO sidecar. It
        # used to anchor on its own root (`runs/source-provenance/x.json`), a directory
        # no walker reads — a ledger nothing reads is worse than no ledger.
        with pytest.raises(ValueError, match="not under 'sources/'"):
            prov.sidecar_path(tmp_path / "runs" / "sources" / "x.md", tmp_path)

    def test_the_two_source_roots_cannot_collide_on_one_ledger(self, tmp_path):
        """The adversarial pair. `sources/z.md` and `runs/sources/z.md` are two different
        papers; mapping both into one `source-provenance/` would put both ledgers in
        `source-provenance/z.json` and silently merge them (#258's slug collision, in the
        provenance layer). The collision is impossible by *construction*, not improbable:
        only one root maps at all, so there is no second path to collide with."""
        flat = prov.sidecar_path(tmp_path / "sources" / "z.md", tmp_path)
        assert flat == tmp_path / prov.SIDECAR_DIR / "z.json"
        with pytest.raises(ValueError):
            prov.sidecar_path(tmp_path / "runs" / "sources" / "z.md", tmp_path)

    def test_a_source_dir_named_sources_cannot_put_a_sidecar_inside_sources(self, tmp_path):
        """`sources/a/sources/x.md` is a path a user can simply mkdir. Anchoring on the
        innermost `sources/` component sent its sidecar to `sources/a/source-provenance/`
        — *inside* `sources/`, where the next enumeration counts the ledger as a source.
        With the KB root given, the sidecar cannot land under `sources/` at all."""
        path = prov.sidecar_path(tmp_path / "sources" / "a" / "sources" / "x.md", tmp_path)
        assert path == tmp_path / prov.SIDECAR_DIR / "a" / "sources" / "x.json"
        assert path.relative_to(tmp_path).parts[0] == prov.SIDECAR_DIR

    def test_a_kb_root_that_itself_contains_sources_stays_inside_the_kb(self, tmp_path):
        """The mirror trap: anchoring on the *outermost* `sources/` component would send
        `/x/sources/kb/sources/p.md`'s sidecar to `/x/source-provenance/kb/sources/p.json`
        — outside the KB. `relative_to(kb_root)` cannot reach either failure."""
        kb = tmp_path / "sources" / "kb"
        path = prov.sidecar_path(kb / "sources" / "p.md", kb)
        assert path == kb / prov.SIDECAR_DIR / "p.json"

    def test_a_path_outside_sources_is_refused(self, tmp_path):
        # Silently producing `<kb>/source-provenance/x.json` for an arbitrary
        # path would write a ledger for a file that is not a source.
        with pytest.raises(ValueError, match="not under 'sources/'"):
            prov.sidecar_path(tmp_path / "elsewhere" / "x.md", tmp_path)

    def test_a_path_outside_the_kb_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="not under the KB root"):
            prov.sidecar_path(tmp_path / "sources" / "x.md", tmp_path / "other")

    def test_is_sidecar_recognises_a_nested_sidecar(self):
        from pathlib import Path
        assert prov.is_sidecar(Path("kb") / prov.SIDECAR_DIR / "a" / "x.json")
        assert not prov.is_sidecar(Path("kb") / "sources" / "a" / "x.json")


class TestReadRejectsCorruptLedgers:
    """A corrupt sidecar must raise, never read as empty: the next write would
    erase real provenance. These shapes all parse as JSON, so only a read-boundary
    check catches them."""

    def _write(self, tmp_path, payload):
        path = tmp_path / "x.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def _record(self, **over):
        return {"type": "arxiv", "id": "2311.09277", "imported_at": "t", **over}

    def test_duplicate_type_id_records_are_refused(self, tmp_path):
        # add_source guarantees one record per (type, id). A file breaking it was
        # not written by us, and read-modify-write would pick one arbitrarily.
        path = self._write(tmp_path, {"schema_version": 1, "records": [
            self._record(version=1), self._record(version=9)]})
        with pytest.raises(ProvenanceError, match="two arxiv records"):
            prov.read_provenance(path)

    def test_a_future_schema_version_is_refused(self, tmp_path):
        # Reading a v2 layout as v1 would misparse it, and the next write would
        # persist the misparse.
        path = self._write(tmp_path, {"schema_version": SCHEMA_VERSION + 1,
                                      "records": [self._record()]})
        with pytest.raises(ProvenanceError, match="newer than this factlog"):
            prov.read_provenance(path)

    def test_an_older_schema_version_is_accepted(self, tmp_path):
        path = self._write(tmp_path, {"schema_version": 0, "records": []})
        assert prov.read_provenance(path).schema_version == 0

    @pytest.mark.parametrize("bad", [123, None, ["x"], {"a": 1}])
    def test_a_non_string_id_is_refused_at_read_not_at_write(self, tmp_path, bad):
        # Left unchecked, this survives read and makes _serialize's sort compare
        # an int against a str, dying with a bare TypeError at write time — far
        # from the corrupt file that caused it.
        path = self._write(tmp_path, {"schema_version": 1, "records": [self._record(id=bad)]})
        with pytest.raises(ProvenanceError, match="'id' must be a string"):
            prov.read_provenance(path)

    def test_a_non_string_type_is_refused(self, tmp_path):
        path = self._write(tmp_path, {"schema_version": 1, "records": [self._record(type=7)]})
        with pytest.raises(ProvenanceError, match="'type' must be a string"):
            prov.read_provenance(path)

    @pytest.mark.parametrize("payload", [{}, {"schema_version": 1}, {"records": []}])
    def test_a_file_missing_a_required_key_is_refused(self, tmp_path, payload):
        path = self._write(tmp_path, payload)
        with pytest.raises(ProvenanceError, match="has no"):
            prov.read_provenance(path)

    @pytest.mark.parametrize("version", ["1", 1.5, True, None])
    def test_a_non_integer_schema_version_is_refused(self, tmp_path, version):
        path = self._write(tmp_path, {"schema_version": version, "records": []})
        with pytest.raises(ProvenanceError, match="not an integer"):
            prov.read_provenance(path)

    def test_a_missing_file_still_reads_as_empty(self, tmp_path):
        # The one case that must NOT raise: a source with no ledger yet.
        assert prov.read_provenance(tmp_path / "absent.json").records == []


class TestUpdateSource:
    """`arxiv-check-versions --auto-update` (#58) exists to change an arXiv
    record's version in place. `add_source` refuses that by design, so a
    legitimate refresh needs its own verb."""

    def _record(self, **over):
        return SourceRecord(type="arxiv", id="2311.09277", imported_at="t",
                            fields={"version": 2, **over.pop("fields", {})}, **over)

    def test_update_replaces_a_diverged_record(self, tmp_path):
        p = Provenance(records=[self._record()])
        prov.update_source(p, SourceRecord(type="arxiv", id="2311.09277",
                                           imported_at="t", fields={"version": 3}))
        assert len(p.records) == 1
        assert p.records[0].fields["version"] == 3

    def test_add_source_still_refuses_the_same_change(self, tmp_path):
        # The split is the point: an import has no authority to revise an entry.
        p = Provenance(records=[self._record()])
        with pytest.raises(ProvenanceConflict, match="update_source"):
            prov.add_source(p, SourceRecord(type="arxiv", id="2311.09277",
                                            imported_at="t", fields={"version": 3}))

    def test_update_appends_when_absent(self):
        p = Provenance()
        prov.update_source(p, self._record())
        assert [r.key for r in p.records] == [("arxiv", "2311.09277")]

    def test_update_leaves_other_sources_alone(self):
        openalex = SourceRecord(type="openalex", id="W1", imported_at="t")
        p = Provenance(records=[openalex, self._record()])
        prov.update_source(p, SourceRecord(type="arxiv", id="2311.09277",
                                           imported_at="t", fields={"version": 9}))
        assert openalex in p.records
        assert len(p.records) == 2

    def test_an_updated_ledger_still_round_trips(self, tmp_path):
        path = tmp_path / "sources" / "x.md"
        p = Provenance(records=[self._record()])
        prov.update_source(p, SourceRecord(type="arxiv", id="2311.09277",
                                           imported_at="t", fields={"version": 3}))
        sidecar = prov.sidecar_path(path, tmp_path)
        prov.write_provenance(sidecar, p)
        assert prov.read_provenance(sidecar).records[0].fields["version"] == 3
