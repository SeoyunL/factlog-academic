# SPDX-License-Identifier: Apache-2.0
"""The PubMed KB-level check-log adapter (issue #168).

The record is ``{pmid, last_checked_at}`` — no version, like OpenAlex's. Covers the path
rule, round-trip, byte determinism, missing-is-empty, corrupt-is-error, and that
``check-log/pubmed.json`` is invisible to ``factlog sources`` (the generic boundary owns
the invisibility; this asserts the PubMed adapter inherits it).
"""
from __future__ import annotations

import json

import pytest

from factlog.integrations.pubmed.check_log import (
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

T1 = "2026-07-09T00:00:00+00:00"
T2 = "2026-07-08T00:00:00+00:00"


def _two_record_log() -> CheckLog:
    log = CheckLog()
    record_check(log, "32738937", T1)
    record_check(log, "10123456", T2)
    return log


class TestPathAndNames:
    def test_maps_kb_root_to_sibling_of_sources(self, tmp_path):
        assert check_log_path(tmp_path) == tmp_path / CHECK_LOG_DIR / CHECK_LOG_NAME

    def test_names_are_the_documented_constants(self):
        assert CHECK_LOG_DIR == "check-log"
        assert CHECK_LOG_NAME == "pubmed.json"


class TestRoundTrip:
    def test_write_then_read_is_equal(self, tmp_path):
        path = check_log_path(tmp_path)
        write_check_log(path, _two_record_log())
        loaded = read_check_log(path)
        assert loaded.schema_version == SCHEMA_VERSION
        assert loaded.entries == {
            "32738937": CheckRecord(T1),
            "10123456": CheckRecord(T2),
        }

    def test_record_is_only_a_timestamp(self):
        rec = CheckRecord(last_checked_at=T1)
        assert rec.last_checked_at == T1
        assert not hasattr(rec, "version")

    def test_upsert_replaces_previous_timestamp(self):
        log = CheckLog()
        record_check(log, "32738937", T2)
        record_check(log, "32738937", T1)
        assert log.entries == {"32738937": CheckRecord(T1)}


class TestBytesAndBoundary:
    def test_missing_file_reads_as_empty(self, tmp_path):
        assert read_check_log(check_log_path(tmp_path)).entries == {}

    def test_write_is_deterministic_and_id_keyed(self, tmp_path):
        path = check_log_path(tmp_path)
        write_check_log(path, _two_record_log())
        first = path.read_bytes()
        # Insertion order reversed; bytes must be identical (records sorted by pmid).
        log = CheckLog()
        record_check(log, "10123456", T2)
        record_check(log, "32738937", T1)
        write_check_log(path, log)
        assert path.read_bytes() == first
        payload = json.loads(first)
        assert [e["pmid"] for e in payload["entries"]] == ["10123456", "32738937"]
        assert set(payload["entries"][0]) == {"pmid", "last_checked_at"}

    def test_corrupt_is_error_not_empty(self, tmp_path):
        path = check_log_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text('{"schema_version": 1}', encoding="utf-8")  # no entries key
        with pytest.raises(CheckLogError):
            read_check_log(path)
