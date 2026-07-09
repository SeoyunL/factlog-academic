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
        assert sidecar_path(src) == tmp_path / SIDECAR_DIR / "foo.json"

    def test_kb_root_is_parent_of_sources(self, tmp_path):
        # The sidecar dir sits beside sources/, not inside it.
        src = tmp_path / "sources" / "foo.md"
        result = sidecar_path(src)
        assert result.parent == tmp_path / SIDECAR_DIR
        assert result.parent.parent == tmp_path

    def test_multidot_stem_replaces_only_final_suffix(self, tmp_path):
        src = tmp_path / "sources" / "foo.provenance.md"
        assert sidecar_path(src) == tmp_path / SIDECAR_DIR / "foo.provenance.json"

    def test_non_md_source(self, tmp_path):
        src = tmp_path / "sources" / "report.pdf"
        assert sidecar_path(src) == tmp_path / SIDECAR_DIR / "report.json"

    def test_accepts_str(self, tmp_path):
        src = str(tmp_path / "sources" / "foo.md")
        assert sidecar_path(src) == tmp_path / SIDECAR_DIR / "foo.json"


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
        assert is_sidecar(sidecar_path(tmp_path / "sources" / "foo.md"))


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
    write_provenance(sidecar_path(kb / "sources" / "doe-2020-a-study.md"),
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
