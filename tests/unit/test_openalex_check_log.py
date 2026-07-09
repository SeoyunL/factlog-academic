# SPDX-License-Identifier: Apache-2.0
"""The OpenAlex KB-level check-log adapter (issue #83).

The record is `{openalex_id, last_checked_at}` — no version. Covers the path rule,
round-trip, byte determinism, corrupt-is-error, and — via the REAL CLI — that
`check-log/openalex.json` is invisible to `factlog sources`/`status`/`export`, measured
(not asserted), with a guard test that fails if `check-log` is added to `SOURCE_ROOTS`.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from factlog import cli, common
from factlog.integrations.openalex.check_log import (
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

T1 = "2026-07-09T00:00:00Z"
T2 = "2026-07-08T00:00:00Z"


def _two_work_log() -> CheckLog:
    log = CheckLog()
    record_check(log, "W2311", T1)
    record_check(log, "W2210", T2)
    return log


class TestPathAndNames:
    def test_maps_kb_root_to_sibling_of_sources(self, tmp_path):
        assert check_log_path(tmp_path) == tmp_path / CHECK_LOG_DIR / CHECK_LOG_NAME

    def test_names_are_the_documented_constants(self):
        assert CHECK_LOG_DIR == "check-log"
        assert CHECK_LOG_NAME == "openalex.json"


class TestRoundTrip:
    def test_write_then_read_is_equal(self, tmp_path):
        path = check_log_path(tmp_path)
        write_check_log(path, _two_work_log())
        loaded = read_check_log(path)
        assert loaded.schema_version == SCHEMA_VERSION
        assert loaded.entries == {"W2311": CheckRecord(T1), "W2210": CheckRecord(T2)}

    def test_record_has_no_version(self):
        # The whole point: OpenAlex has no version to remember.
        rec = CheckRecord(T1)
        assert not hasattr(rec, "version")

    def test_exact_two_work_bytes(self, tmp_path):
        path = check_log_path(tmp_path)
        write_check_log(path, _two_work_log())
        expected = (
            "{\n"
            '  "entries": [\n'
            "    {\n"
            '      "last_checked_at": "2026-07-08T00:00:00Z",\n'
            '      "openalex_id": "W2210"\n'
            "    },\n"
            "    {\n"
            '      "last_checked_at": "2026-07-09T00:00:00Z",\n'
            '      "openalex_id": "W2311"\n'
            "    }\n"
            "  ],\n"
            '  "schema_version": 1\n'
            "}\n"
        )
        assert path.read_text(encoding="utf-8") == expected

    def test_re_check_replaces_timestamp(self):
        log = CheckLog()
        record_check(log, "W1", T2)
        record_check(log, "W1", T1)
        assert log.entries == {"W1": CheckRecord(T1)}


class TestDeterminism:
    def test_two_writes_are_byte_identical(self, tmp_path):
        a = tmp_path / CHECK_LOG_DIR / "a.json"
        b = tmp_path / CHECK_LOG_DIR / "b.json"
        write_check_log(a, _two_work_log())
        write_check_log(b, _two_work_log())
        assert a.read_bytes() == b.read_bytes()


class TestCorruptIsError:
    def test_missing_file_is_empty(self, tmp_path):
        assert read_check_log(check_log_path(tmp_path)).entries == {}

    def test_invalid_json_raises(self, tmp_path):
        path = tmp_path / "x.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(CheckLogError, match="not valid JSON"):
            read_check_log(path)

    def test_a_version_key_is_unexpected_for_openalex(self, tmp_path):
        # arXiv's `version` has no place in an OpenAlex record.
        path = tmp_path / "x.json"
        path.write_text(json.dumps({"schema_version": 1, "entries": [
            {"openalex_id": "W1", "last_checked_at": T1, "version": 1}]}), encoding="utf-8")
        with pytest.raises(CheckLogError, match="unexpected key"):
            read_check_log(path)

    def test_non_string_openalex_id_raises(self, tmp_path):
        path = tmp_path / "x.json"
        path.write_text(json.dumps({"schema_version": 1, "entries": [
            {"openalex_id": 5, "last_checked_at": T1}]}), encoding="utf-8")
        with pytest.raises(CheckLogError, match="'openalex_id' must be a string"):
            read_check_log(path)


# --------------------------------------------------------------------------- #
# invisibility — MEASURED against the real CLI (#58 asserted, #63 caught it)
# --------------------------------------------------------------------------- #
_BIB = (
    '---\nzotero_key: "K1"\nitem_type: "journalArticle"\ntitle: "A Study"\n'
    'authors: ["Doe J"]\nyear: "2020"\ndoi: "10.1/x"\n---\n\n# body\n'
)


def _make_kb(tmp_path):
    (tmp_path / "sources").mkdir()
    (tmp_path / "sources" / "doe-2020-a-study.md").write_text(_BIB, encoding="utf-8")
    return tmp_path


def _add_check_log(kb):
    write_check_log(check_log_path(kb), _two_work_log())


class TestCliInvisibility:
    @pytest.mark.parametrize("argv_tail", [
        ["sources"],
        ["status"],
        ["export", "--bibtex"],
        ["export", "--csl"],
    ])
    def test_output_identical_with_and_without_check_log(self, tmp_path, capsys, argv_tail):
        kb = _make_kb(tmp_path)
        argv = [*argv_tail, "--target", str(kb)]
        rc_without = cli.main(argv)
        without = capsys.readouterr()

        _add_check_log(kb)
        assert check_log_path(kb).is_file()
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
        assert CHECK_LOG_DIR not in out


class TestGuardOnSourceRoots:
    def test_source_files_ignores_the_check_log(self, tmp_path):
        kb = _make_kb(tmp_path)
        _add_check_log(kb)
        assert check_log_path(kb) not in common.source_files(kb)

    def test_adding_check_log_to_source_roots_surfaces_it(self, tmp_path, monkeypatch):
        kb = _make_kb(tmp_path)
        _add_check_log(kb)
        monkeypatch.setattr(common, "SOURCE_ROOTS", (*common.SOURCE_ROOTS, CHECK_LOG_DIR))
        assert check_log_path(kb) in common.source_files(kb)


def test_import_factlog_does_not_load_openalex_check_log():
    code = (
        "import sys, factlog; "
        "assert 'factlog.integrations.openalex.check_log' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
