# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the KB-level arXiv check-log reader/writer (issue #77, Step 6a).

Covers the path rule, round-trip, upsert semantics, byte determinism,
missing/corrupt files (every corrupt shape that parses as JSON), atomic-write
behaviour, ``import factlog`` weight, and — via the real CLI — that a
``check-log/`` directory is invisible to source enumeration. No check-log is
created by any production code path in this step; these tests exercise the module
directly.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from factlog import cli, common
from factlog.integrations.arxiv import check_log as clog
from factlog.integrations.arxiv.check_log import (
    CHECK_LOG_DIR,
    CHECK_LOG_NAME,
    SCHEMA_VERSION,
    CheckLog,
    CheckLogError,
    CheckRecord,
    check_log_path,
    read_check_log,
    record_check,
    write_check_log,
)

# Fixed, caller-supplied timestamps — the module must never read a clock, so
# reusing these keeps every write byte-reproducible.
T1 = "2026-07-09T00:00:00Z"
T2 = "2026-07-08T00:00:00Z"


def _two_paper_log() -> CheckLog:
    """The canonical two-paper example from the issue."""
    log = CheckLog()
    record_check(log, "2311.09277", T1, 2)
    record_check(log, "2210.03629", T2, 1)
    return log


# --- path rule ---------------------------------------------------------------

class TestCheckLogPath:
    def test_maps_kb_root_to_sibling_of_sources(self, tmp_path):
        assert check_log_path(tmp_path) == tmp_path / CHECK_LOG_DIR / CHECK_LOG_NAME

    def test_check_log_is_not_under_sources(self, tmp_path):
        rel = check_log_path(tmp_path).relative_to(tmp_path)
        assert "sources" not in rel.parts

    def test_accepts_str(self, tmp_path):
        assert check_log_path(str(tmp_path)) == tmp_path / CHECK_LOG_DIR / CHECK_LOG_NAME

    def test_names_are_the_documented_constants(self):
        assert CHECK_LOG_DIR == "check-log"
        assert CHECK_LOG_NAME == "arxiv.json"


# --- round-trip --------------------------------------------------------------

class TestRoundTrip:
    def test_write_then_read_is_equal(self, tmp_path):
        path = check_log_path(tmp_path)
        original = _two_paper_log()
        write_check_log(path, original)
        loaded = read_check_log(path)
        assert loaded.schema_version == SCHEMA_VERSION
        assert loaded.entries == original.entries

    def test_lookup_is_keyed_by_arxiv_id(self, tmp_path):
        path = check_log_path(tmp_path)
        write_check_log(path, _two_paper_log())
        loaded = read_check_log(path)
        assert loaded.entries["2311.09277"] == CheckRecord("2026-07-09T00:00:00Z", 2)

    def test_exact_two_paper_bytes(self, tmp_path):
        # Pins the on-disk format: records sorted by arxiv_id, keys sorted,
        # indent=2, trailing newline.
        path = check_log_path(tmp_path)
        write_check_log(path, _two_paper_log())
        expected = (
            "{\n"
            '  "entries": [\n'
            "    {\n"
            '      "arxiv_id": "2210.03629",\n'
            '      "last_checked_at": "2026-07-08T00:00:00Z",\n'
            '      "version": 1\n'
            "    },\n"
            "    {\n"
            '      "arxiv_id": "2311.09277",\n'
            '      "last_checked_at": "2026-07-09T00:00:00Z",\n'
            '      "version": 2\n'
            "    }\n"
            "  ],\n"
            '  "schema_version": 1\n'
            "}\n"
        )
        assert path.read_text(encoding="utf-8") == expected


# --- determinism -------------------------------------------------------------

class TestDeterminism:
    def test_two_writes_are_byte_identical(self, tmp_path):
        a = tmp_path / CHECK_LOG_DIR / "a.json"
        b = tmp_path / CHECK_LOG_DIR / "b.json"
        write_check_log(a, _two_paper_log())
        write_check_log(b, _two_paper_log())
        assert a.read_bytes() == b.read_bytes()

    def test_insertion_order_does_not_affect_bytes(self, tmp_path):
        forward = CheckLog()
        record_check(forward, "2311.09277", T1, 2)
        record_check(forward, "2210.03629", T2, 1)
        reverse = CheckLog()
        record_check(reverse, "2210.03629", T2, 1)
        record_check(reverse, "2311.09277", T1, 2)
        f = tmp_path / CHECK_LOG_DIR / "f.json"
        r = tmp_path / CHECK_LOG_DIR / "r.json"
        write_check_log(f, forward)
        write_check_log(r, reverse)
        assert f.read_bytes() == r.read_bytes()


# --- record_check (upsert) semantics -----------------------------------------

class TestRecordCheck:
    def test_records_a_new_paper(self):
        log = CheckLog()
        record_check(log, "2311.09277", T1, 1)
        assert log.entries == {"2311.09277": CheckRecord(T1, 1)}

    def test_re_recording_identical_is_a_noop_on_bytes(self, tmp_path):
        path = check_log_path(tmp_path)
        log = CheckLog()
        record_check(log, "2311.09277", T1, 1)
        write_check_log(path, log)
        before = path.read_bytes()
        record_check(log, "2311.09277", T1, 1)
        write_check_log(path, log)
        assert path.read_bytes() == before

    def test_re_check_replaces_timestamp_and_version(self):
        # The whole point of --older-than: the latest observation wins.
        log = CheckLog()
        record_check(log, "2311.09277", T2, 1)
        record_check(log, "2311.09277", T1, 2)
        assert len(log.entries) == 1
        assert log.entries["2311.09277"] == CheckRecord(T1, 2)

    def test_recording_a_second_paper_leaves_the_first(self):
        log = CheckLog()
        record_check(log, "2311.09277", T1, 1)
        record_check(log, "2210.03629", T2, 3)
        assert set(log.entries) == {"2311.09277", "2210.03629"}


# --- read robustness: missing file -------------------------------------------

class TestMissingFile:
    def test_missing_file_yields_empty(self, tmp_path):
        result = read_check_log(check_log_path(tmp_path))
        assert result.schema_version == SCHEMA_VERSION
        assert result.entries == {}

    def test_a_first_run_can_write_after_an_empty_read(self, tmp_path):
        path = check_log_path(tmp_path)
        log = read_check_log(path)  # missing -> empty
        record_check(log, "2311.09277", T1, 1)
        write_check_log(path, log)
        assert read_check_log(path).entries["2311.09277"] == CheckRecord(T1, 1)


# --- read robustness: every corrupt shape must raise -------------------------

class TestReadRejectsCorruptLogs:
    """Each shape parses as valid JSON, so only a read-boundary check catches it.
    Reading any as empty would let the next write erase the real log."""

    def _write(self, tmp_path, payload_text):
        path = tmp_path / "x.json"
        path.write_text(payload_text, encoding="utf-8")
        return path

    def _json(self, tmp_path, payload):
        return self._write(tmp_path, json.dumps(payload))

    def _entry(self, **over):
        return {"arxiv_id": "2311.09277", "last_checked_at": T1, "version": 1, **over}

    def test_invalid_json_raises(self, tmp_path):
        path = self._write(tmp_path, "{not json")
        with pytest.raises(CheckLogError, match="not valid JSON"):
            read_check_log(path)

    def test_top_level_array_raises(self, tmp_path):
        path = self._json(tmp_path, [1, 2, 3])
        with pytest.raises(CheckLogError, match="not a JSON object"):
            read_check_log(path)

    @pytest.mark.parametrize("payload", [{}, {"schema_version": 1}, {"entries": []}])
    def test_missing_required_key_raises(self, tmp_path, payload):
        path = self._json(tmp_path, payload)
        with pytest.raises(CheckLogError, match="has no"):
            read_check_log(path)

    def test_future_schema_version_raises(self, tmp_path):
        path = self._json(tmp_path, {"schema_version": SCHEMA_VERSION + 1, "entries": []})
        with pytest.raises(CheckLogError, match="newer than this factlog"):
            read_check_log(path)

    def test_older_schema_version_is_accepted(self, tmp_path):
        path = self._json(tmp_path, {"schema_version": 0, "entries": []})
        assert read_check_log(path).schema_version == 0

    @pytest.mark.parametrize("version", ["1", 1.5, True, None])
    def test_non_integer_schema_version_raises(self, tmp_path, version):
        path = self._json(tmp_path, {"schema_version": version, "entries": []})
        with pytest.raises(CheckLogError, match="not an integer"):
            read_check_log(path)

    def test_entries_not_a_list_raises(self, tmp_path):
        path = self._json(tmp_path, {"schema_version": 1, "entries": {"a": 1}})
        with pytest.raises(CheckLogError, match="'entries' is not a list"):
            read_check_log(path)

    def test_entry_not_an_object_raises(self, tmp_path):
        path = self._json(tmp_path, {"schema_version": 1, "entries": ["nope"]})
        with pytest.raises(CheckLogError, match="entry is not a JSON object"):
            read_check_log(path)

    @pytest.mark.parametrize("drop", ["arxiv_id", "last_checked_at", "version"])
    def test_entry_missing_a_field_raises(self, tmp_path, drop):
        entry = self._entry()
        del entry[drop]
        path = self._json(tmp_path, {"schema_version": 1, "entries": [entry]})
        with pytest.raises(CheckLogError, match="is missing"):
            read_check_log(path)

    def test_entry_extra_key_raises(self, tmp_path):
        path = self._json(tmp_path, {"schema_version": 1,
                                     "entries": [self._entry(updated_at="whenever")]})
        with pytest.raises(CheckLogError, match="unexpected key"):
            read_check_log(path)

    @pytest.mark.parametrize("bad", [123, None, ["x"], {"a": 1}, 1.5])
    def test_non_string_arxiv_id_raises(self, tmp_path, bad):
        path = self._json(tmp_path, {"schema_version": 1,
                                     "entries": [self._entry(arxiv_id=bad)]})
        with pytest.raises(CheckLogError, match="'arxiv_id' must be a string"):
            read_check_log(path)

    @pytest.mark.parametrize("field", ["last_checked_at"])  # `version` has its own int guard
    def test_non_string_value_raises(self, tmp_path, field):
        path = self._json(tmp_path, {"schema_version": 1,
                                     "entries": [self._entry(**{field: 7})]})
        with pytest.raises(CheckLogError, match=f"{field!r} must be a string"):
            read_check_log(path)

    def test_two_records_for_one_id_raises(self, tmp_path):
        path = self._json(tmp_path, {"schema_version": 1, "entries": [
            self._entry(version=1), self._entry(version=9)]})
        with pytest.raises(CheckLogError, match="two records for arxiv_id"):
            read_check_log(path)

    def test_duplicate_key_inside_a_record_raises(self, tmp_path):
        # json.loads keeps the last duplicate silently; the object_pairs_hook
        # refuses it. Hand-written because json.dumps cannot emit a duplicate key.
        text = ('{"schema_version": 1, "entries": [{"arxiv_id": "a", '
                '"arxiv_id": "b", "last_checked_at": "t", "version": 1}]}')
        path = self._write(tmp_path, text)
        with pytest.raises(CheckLogError, match="duplicate key"):
            read_check_log(path)

    def test_a_missing_file_still_reads_as_empty(self, tmp_path):
        # The one case that must NOT raise: a KB with no check-log yet.
        assert read_check_log(tmp_path / "absent.json").entries == {}


# --- atomic write ------------------------------------------------------------

class TestAtomicWrite:
    def test_interrupted_write_leaves_no_partial_file_and_no_tmp(self, tmp_path, monkeypatch):
        import factlog.integrations.common._textio as textio

        def boom(_src, _dst):
            raise OSError("simulated crash before rename completes")

        monkeypatch.setattr(textio.os, "replace", boom)
        path = check_log_path(tmp_path)
        with pytest.raises(OSError):
            write_check_log(path, _two_paper_log())
        assert not path.exists()
        # No .arxiv.json.tmp (or any temp) left behind in the directory.
        assert list(path.parent.iterdir()) == []

    def test_overwrite_is_atomic_and_complete(self, tmp_path):
        path = check_log_path(tmp_path)
        first = CheckLog()
        record_check(first, "2311.09277", T1, 1)
        write_check_log(path, first)
        write_check_log(path, _two_paper_log())
        loaded = read_check_log(path)
        assert set(loaded.entries) == {"2311.09277", "2210.03629"}


# --- CLI invisibility (real KB, real CLI) ------------------------------------

_BIB = (
    '---\nzotero_key: "K1"\nitem_type: "journalArticle"\ntitle: "A Study"\n'
    'authors: ["Doe J"]\nyear: "2020"\ndoi: "10.1/x"\n---\n\n# body\n'
)


def _make_kb(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "doe-2020-a-study.md").write_text(_BIB, encoding="utf-8")
    return tmp_path


def _add_check_log(kb):
    write_check_log(check_log_path(kb), _two_paper_log())


class TestCliInvisibility:
    """A check-log/ directory must not change any command's output. Each command
    is run against a real temp KB with and without a real check-log (built by this
    module), through cli.main — never a reimplementation of the command."""

    @pytest.mark.parametrize("argv_tail", [
        ["sources"],
        ["status"],          # the source_files() surface
        ["export", "--bibtex"],
        ["export", "--csl"],
    ])
    def test_output_identical_with_and_without_check_log(self, tmp_path, capsys, argv_tail):
        kb = _make_kb(tmp_path)
        argv = [*argv_tail, "--target", str(kb)]
        rc_without = cli.main(argv)
        without = capsys.readouterr()

        _add_check_log(kb)
        assert check_log_path(kb).is_file()  # it really exists
        rc_with = cli.main(argv)
        with_ = capsys.readouterr()

        assert rc_without == rc_with
        assert without.out == with_.out
        assert without.err == with_.err

    def test_sources_still_reports_one_source(self, tmp_path, capsys):
        kb = _make_kb(tmp_path)
        _add_check_log(kb)
        rc = cli.main(["sources", "--target", str(kb)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "1 source(s)" in out
        assert CHECK_LOG_DIR not in out  # the log is never listed as a source


class TestGuardOnSourceRoots:
    """The invisibility must rest on ``check-log`` being outside ``SOURCE_ROOTS``,
    not on luck. If someone adds the directory to ``SOURCE_ROOTS``, the enumerator
    must pick the log up — proving that placement is the only thing hiding it."""

    def test_source_files_ignores_the_check_log(self, tmp_path):
        kb = _make_kb(tmp_path)
        _add_check_log(kb)
        assert check_log_path(kb) not in common.source_files(kb)

    def test_adding_check_log_to_source_roots_surfaces_it(self, tmp_path, monkeypatch):
        kb = _make_kb(tmp_path)
        _add_check_log(kb)
        monkeypatch.setattr(common, "SOURCE_ROOTS", (*common.SOURCE_ROOTS, CHECK_LOG_DIR))
        assert check_log_path(kb) in common.source_files(kb)


# --- import weight -----------------------------------------------------------

def test_import_factlog_does_not_load_check_log():
    """`import factlog` must not pull in the check-log module (nor json via it):
    nothing in this step wires it into a production import path."""
    code = (
        "import sys; import factlog; "
        "assert 'factlog.integrations.arxiv.check_log' not in sys.modules, "
        "'check_log was imported by import factlog'"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_module_exports():
    assert clog.CHECK_LOG_DIR == "check-log"
    assert callable(clog.check_log_path)


class TestVersionIsAnIntEverywhere:
    """`version` must be the same type here, on `ParsedArxivWork`, and in an arXiv
    provenance record. A string in the log is a silent trap for the one consumer
    this file exists for: `arxiv-check-versions` (#78) compares the logged version
    with the one the API returns, and `7 != "7"` reports every paper as changed."""

    def test_the_log_agrees_with_the_parsed_work_and_the_provenance_record(self):
        # Runtime types, not annotations: `from __future__ import annotations`
        # makes the latter strings, and a string that says "int" proves nothing.
        from datetime import date

        from factlog.integrations.arxiv.source_writer import ArxivSourceWriter
        from factlog.integrations.arxiv.work_parser import ParsedArxivWork

        work = ParsedArxivWork(
            arxiv_id="1706.03762", version=7, title="T", authors=("A",),
            abstract="x", primary_category="cs.CL", categories=("cs.CL",),
            submitted=date(2017, 6, 12), last_updated=date(2017, 6, 12))
        provenance_version = ArxivSourceWriter()._provenance_record(work, "t").fields["version"]

        log = CheckLog()
        record_check(log, work.arxiv_id, T1, work.version)
        logged = log.entries[work.arxiv_id].version

        assert type(work.version) is type(provenance_version) is type(logged) is int

    def test_the_comparison_check_versions_will_make(self, tmp_path):
        # The whole point of the log. Round-trip through disk, then compare with an
        # int as `ParsedArxivWork.version` hands it over.
        path = check_log_path(tmp_path)
        log = CheckLog()
        record_check(log, "1706.03762", T1, 7)
        write_check_log(path, log)

        logged = read_check_log(path).entries["1706.03762"].version
        api_version = 7  # what ParsedArxivWork.version carries
        assert logged == api_version
        assert logged is not None and isinstance(logged, int)

    @pytest.mark.parametrize("bad", ["1", 1.5, True, None, [1]])
    def test_a_non_integer_version_is_refused_at_read(self, tmp_path, bad):
        path = check_log_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "entries": [
            {"arxiv_id": "1706.03762", "last_checked_at": T1, "version": bad}]}),
            encoding="utf-8")
        with pytest.raises(CheckLogError, match="must be an integer"):
            read_check_log(path)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_version_below_one_is_refused(self, tmp_path, bad):
        # arXiv numbers versions from v1; v0 does not exist.
        path = check_log_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema_version": 1, "entries": [
            {"arxiv_id": "1706.03762", "last_checked_at": T1, "version": bad}]}),
            encoding="utf-8")
        with pytest.raises(CheckLogError, match=">= 1"):
            read_check_log(path)

    def test_the_version_is_written_as_a_json_number(self, tmp_path):
        path = check_log_path(tmp_path)
        log = CheckLog()
        record_check(log, "1706.03762", T1, 7)
        write_check_log(path, log)
        raw = json.loads(path.read_text(encoding="utf-8"))
        assert raw["entries"][0]["version"] == 7
        assert isinstance(raw["entries"][0]["version"], int)


class TestTheWriteBoundaryIsGuardedToo:
    """`provenance.py` guarded its reader and left its writer open. A value only a
    *read* rejects is a value a *write* can still put on disk, and then every later
    read of that file fails — one bad call bricks the KB's check-log.

    No test here passed a non-int to `record_check`, so a `version: str` annotation
    survived 62 green tests.
    """

    @pytest.mark.parametrize("bad", ["7", 1.5, True, None, [7]])
    def test_a_non_integer_version_is_refused_at_write(self, bad):
        with pytest.raises(CheckLogError, match="must be an integer"):
            record_check(CheckLog(), "1706.03762", T1, bad)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_a_version_below_one_is_refused_at_write(self, bad):
        with pytest.raises(CheckLogError, match=">= 1"):
            record_check(CheckLog(), "1706.03762", T1, bad)

    def test_a_refused_write_leaves_the_log_untouched(self):
        log = CheckLog()
        record_check(log, "1706.03762", T1, 7)
        before = dict(log.entries)
        with pytest.raises(CheckLogError):
            record_check(log, "1706.03762", T1, "8")
        assert log.entries == before

    def test_the_log_cannot_be_bricked_by_one_bad_call(self, tmp_path):
        # The end-to-end shape of the bug: a bad write, then every read fails.
        path = check_log_path(tmp_path)
        with pytest.raises(CheckLogError):
            log = CheckLog()
            record_check(log, "1706.03762", T1, "7")
            write_check_log(path, log)
        assert not path.exists()

    def test_record_check_declares_the_type_it_enforces(self):
        import inspect

        annotation = inspect.signature(record_check).parameters["version"].annotation
        assert annotation == "int", (
            "the signature invites the very value the reader rejects"
        )
