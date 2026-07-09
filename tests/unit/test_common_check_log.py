# SPDX-License-Identifier: Apache-2.0
"""The generic KB-level check-log boundary (issue #83).

`common/check_log.py` is the read/write boundary that `arxiv.check_log` and
`openalex.check_log` both delegate to, so a hardening on one side (#77's both-boundary
`version` guard) cannot rot on the other (#64's two-guarded-twins failure). These tests
exercise the generic module directly, then pin — with a GOLDEN byte-identity test —
that the arXiv migration onto it is a pure no-op on disk.
"""
from __future__ import annotations

import json
import subprocess
import sys

import pytest

from factlog.integrations.common import check_log as base
from factlog.integrations.common.check_log import (
    CheckLog,
    CheckLogError,
    CheckLogSchema,
    FieldSpec,
    check_log_path,
    read_check_log,
    record_check,
    write_check_log,
)

T1 = "2026-07-09T00:00:00Z"
T2 = "2026-07-08T00:00:00Z"


def _int_ge_1(value, where=""):
    suffix = f": {where}" if where else ""
    if not isinstance(value, int) or isinstance(value, bool):
        raise CheckLogError(f"must be an integer, got {type(value).__name__}{suffix}")
    if value < 1:
        raise CheckLogError(f"must be >= 1, got {value}{suffix}")
    return value


#: A schema with one extra validated field, like arXiv's.
VERSIONED = CheckLogSchema(name="v.json", id_key="thing_id",
                           fields=(FieldSpec("version", _int_ge_1),))
#: A schema with no extra fields, like OpenAlex's.
BARE = CheckLogSchema(name="b.json", id_key="thing_id")


class TestPath:
    def test_path_uses_the_schema_name(self, tmp_path):
        assert check_log_path(tmp_path, BARE) == tmp_path / "check-log" / "b.json"

    def test_path_is_outside_sources(self, tmp_path):
        rel = check_log_path(tmp_path, VERSIONED).relative_to(tmp_path)
        assert "sources" not in rel.parts


class TestRoundTrip:
    def test_write_then_read_bare(self, tmp_path):
        log = CheckLog()
        record_check(log, "A", T1, {}, BARE)
        record_check(log, "B", T2, {}, BARE)
        path = check_log_path(tmp_path, BARE)
        write_check_log(path, log, BARE)
        loaded = read_check_log(path, BARE)
        assert loaded.entries == {"A": {"last_checked_at": T1},
                                  "B": {"last_checked_at": T2}}

    def test_write_then_read_versioned(self, tmp_path):
        log = CheckLog()
        record_check(log, "A", T1, {"version": 3}, VERSIONED)
        path = check_log_path(tmp_path, VERSIONED)
        write_check_log(path, log, VERSIONED)
        loaded = read_check_log(path, VERSIONED)
        assert loaded.entries == {"A": {"last_checked_at": T1, "version": 3}}


class TestDeterminism:
    def test_two_writes_are_byte_identical(self, tmp_path):
        log = CheckLog()
        record_check(log, "B", T2, {"version": 1}, VERSIONED)
        record_check(log, "A", T1, {"version": 2}, VERSIONED)
        a = tmp_path / "a.json"
        b = tmp_path / "b.json"
        write_check_log(a, log, VERSIONED)
        write_check_log(b, log, VERSIONED)
        assert a.read_bytes() == b.read_bytes()

    def test_insertion_order_does_not_affect_bytes(self, tmp_path):
        forward, reverse = CheckLog(), CheckLog()
        record_check(forward, "A", T1, {}, BARE)
        record_check(forward, "B", T2, {}, BARE)
        record_check(reverse, "B", T2, {}, BARE)
        record_check(reverse, "A", T1, {}, BARE)
        f, r = tmp_path / "f.json", tmp_path / "r.json"
        write_check_log(f, forward, BARE)
        write_check_log(r, reverse, BARE)
        assert f.read_bytes() == r.read_bytes()


class TestRecordCheckValidatesAtWrite:
    """The write boundary runs the schema's validators — a value only a read would
    reject can never reach the disk (#77)."""

    @pytest.mark.parametrize("bad", ["1", 1.5, True, None])
    def test_bad_extra_field_is_refused_at_write(self, bad):
        with pytest.raises(CheckLogError):
            record_check(CheckLog(), "A", T1, {"version": bad}, VERSIONED)

    def test_wrong_extra_keys_are_refused(self):
        with pytest.raises(CheckLogError, match="must carry exactly"):
            record_check(CheckLog(), "A", T1, {}, VERSIONED)
        with pytest.raises(CheckLogError, match="must carry exactly"):
            record_check(CheckLog(), "A", T1, {"nope": 1}, BARE)

    def test_a_refused_write_leaves_the_log_untouched(self):
        log = CheckLog()
        record_check(log, "A", T1, {"version": 7}, VERSIONED)
        before = dict(log.entries)
        with pytest.raises(CheckLogError):
            record_check(log, "A", T1, {"version": "8"}, VERSIONED)
        assert log.entries == before


class TestReadBoundary:
    def _write(self, tmp_path, payload):
        p = tmp_path / "x.json"
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_missing_file_is_empty(self, tmp_path):
        assert read_check_log(tmp_path / "absent.json", BARE).entries == {}

    def test_invalid_json_raises(self, tmp_path):
        p = tmp_path / "x.json"
        p.write_text("{not json", encoding="utf-8")
        with pytest.raises(CheckLogError, match="not valid JSON"):
            read_check_log(p, BARE)

    def test_bad_extra_field_is_refused_at_read(self, tmp_path):
        p = self._write(tmp_path, {"schema_version": 1, "entries": [
            {"thing_id": "A", "last_checked_at": T1, "version": "7"}]})
        with pytest.raises(CheckLogError, match="must be an integer"):
            read_check_log(p, VERSIONED)

    def test_message_names_the_schema_id_key(self, tmp_path):
        p = self._write(tmp_path, {"schema_version": 1, "entries": [
            {"thing_id": 5, "last_checked_at": T1}]})
        with pytest.raises(CheckLogError, match="'thing_id' must be a string"):
            read_check_log(p, BARE)

    def test_two_records_for_one_id_names_the_schema_id_key(self, tmp_path):
        p = self._write(tmp_path, {"schema_version": 1, "entries": [
            {"thing_id": "A", "last_checked_at": T1},
            {"thing_id": "A", "last_checked_at": T2}]})
        with pytest.raises(CheckLogError, match="two records for thing_id"):
            read_check_log(p, BARE)

    def test_a_bare_schemas_version_key_is_an_unexpected_key(self, tmp_path):
        # A record carrying `version` under the BARE schema (no such field) is corrupt.
        p = self._write(tmp_path, {"schema_version": 1, "entries": [
            {"thing_id": "A", "last_checked_at": T1, "version": 1}]})
        with pytest.raises(CheckLogError, match="unexpected key"):
            read_check_log(p, BARE)


# --------------------------------------------------------------------------- #
# GOLDEN: the arXiv migration onto the generic boundary is a byte-level no-op
# --------------------------------------------------------------------------- #
class TestArxivGoldenByteIdentity:
    """The safety condition of the generalization (#83): `check-log/arxiv.json` written
    through the arXiv module must be byte-for-byte what it was before the module was
    lifted onto `common/check_log.py`. If this drifts, the migration weakened
    something — STOP and ship a standalone module instead."""

    def test_exact_bytes_via_the_arxiv_module(self, tmp_path):
        from factlog.integrations.arxiv.check_log import (
            CheckLog as ArxivCheckLog,
        )
        from factlog.integrations.arxiv.check_log import (
            check_log_path as arxiv_path,
        )
        from factlog.integrations.arxiv.check_log import (
            record_check as arxiv_record,
        )
        from factlog.integrations.arxiv.check_log import (
            write_check_log as arxiv_write,
        )

        log = ArxivCheckLog()
        arxiv_record(log, "2311.09277", "2026-07-09T00:00:00Z", 2)
        arxiv_record(log, "2210.03629", "2026-07-08T00:00:00Z", 1)
        path = arxiv_path(tmp_path)
        arxiv_write(path, log)
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

    def test_the_arxiv_error_type_is_the_generic_one(self):
        # arXiv re-exports the generic CheckLogError, so `pytest.raises` in the arXiv
        # suite catches what the generic boundary raises.
        from factlog.integrations.arxiv.check_log import CheckLogError as ArxivErr
        assert ArxivErr is base.CheckLogError


def test_import_factlog_does_not_load_the_generic_check_log():
    code = (
        "import sys, factlog; "
        "assert 'factlog.integrations.common.check_log' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
