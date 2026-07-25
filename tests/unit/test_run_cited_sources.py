# SPDX-License-Identifier: Apache-2.0
"""`common.run_cited_sources` — what runs/*.json ASKS for, keyed as merge keys (#558).

The coverage report reads candidates.csv, which is the state AFTER merge dropped
every row citing a source that is not on disk, so a KB carrying 145 rows about a
deleted source reports `0 orphan citation(s)` forever. This helper reads the run
files directly, so the drop is observable from a status query.

The load-bearing property is not "it counts rows" but "it keys them EXACTLY the
way merge does". A key built by a different rule turns a source that is alive on
disk into a reported orphan (or hides a dead one), so the rule — strip, NFC,
pre-anchor — is pinned here field by field.
"""
from __future__ import annotations

import json
import unicodedata

import common


def write_run(root, name, rows):
    """One runs/<name>.json holding *rows* (raw, unnormalised)."""
    (root / "runs").mkdir(exist_ok=True)
    (root / "runs" / name).write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8"
    )


def row(**overrides):
    base = {
        "subject": "갑봇",
        "relation": "통합",
        "object": "을서비스",
        "source": "sources/a.md",
        "status": "candidate",
        "confidence": "0.9",
        "note": "",
    }
    base.update(overrides)
    return base


class TestKeyRule:
    def test_counts_rows_per_source(self, tmp_path):
        write_run(tmp_path, "r.json", [row(), row(object="병서비스"), row(source="sources/b.md")])
        assert common.run_cited_sources(tmp_path) == {"sources/a.md": 2, "sources/b.md": 1}

    def test_surrounding_whitespace_is_stripped(self, tmp_path):
        """The measured blocking case: merge strips on load, so a trailing-space
        source merges into candidates.csv as the bare path and stays ALIVE. A key
        that kept the space would report a live source as an orphan."""
        write_run(tmp_path, "r.json", [row(source="sources/a.md  "), row(source="  sources/a.md")])
        assert common.run_cited_sources(tmp_path) == {"sources/a.md": 2}

    def test_nfd_source_folds_onto_nfc(self, tmp_path):
        """macOS stores filenames NFD; source_file_refs compares NFC. Both spellings
        of one Korean path must land on one key, or half its rows read as orphaned."""
        nfc = unicodedata.normalize("NFC", "sources/방법론.md")
        nfd = unicodedata.normalize("NFD", nfc)
        assert nfd != nfc  # the fixture is only meaningful if the forms differ
        write_run(tmp_path, "r.json", [row(source=nfc), row(source=nfd, object="병서비스")])
        assert common.run_cited_sources(tmp_path) == {nfc: 2}

    def test_anchor_is_stripped(self, tmp_path):
        """Merge checks existence on the pre-anchor portion, so anchored and bare
        citations of one file are one key."""
        write_run(
            tmp_path,
            "r.json",
            [row(source="sources/a.md#sec1"), row(source="sources/a.md#sec2"), row(source="sources/a.md")],
        )
        assert common.run_cited_sources(tmp_path) == {"sources/a.md": 3}

    def test_anchor_only_source_contributes_nothing(self, tmp_path):
        """'#sec' strips to the empty ref, which names no source."""
        write_run(tmp_path, "r.json", [row(source="#sec")])
        assert common.run_cited_sources(tmp_path) == {}


class TestRowCompleteness:
    def test_row_missing_a_required_field_is_skipped(self, tmp_path):
        """Merge keeps a row only when subject/relation/object/source are all
        non-empty; a row it would never keep must not be counted as pending."""
        for field in ("subject", "relation", "object", "source"):
            write_run(tmp_path, "r.json", [row(**{field: ""})])
            assert common.run_cited_sources(tmp_path) == {}, field

    def test_whitespace_only_field_counts_as_missing(self, tmp_path):
        write_run(tmp_path, "r.json", [row(subject="   ")])
        assert common.run_cited_sources(tmp_path) == {}

    def test_optional_fields_may_be_absent(self, tmp_path):
        """status/confidence/note are not part of the completeness rule."""
        write_run(
            tmp_path,
            "r.json",
            [{"subject": "갑봇", "relation": "통합", "object": "을서비스", "source": "sources/a.md"}],
        )
        assert common.run_cited_sources(tmp_path) == {"sources/a.md": 1}

    def test_non_dict_item_is_skipped(self, tmp_path):
        write_run(tmp_path, "r.json", ["nope", 3, None, row()])
        assert common.run_cited_sources(tmp_path) == {"sources/a.md": 1}


class TestUnreadableInputIsSilent:
    """A diagnostic report must survive every state a runs/ dir can be in.
    ``load_candidate_files`` raises SystemExit on a parse failure; copying that
    policy here would let one malformed file hide the whole KB's status."""

    def test_non_array_json_is_skipped(self, tmp_path):
        """Other tools write objects under runs/ (generate_logic_policy.py)."""
        write_run(tmp_path, "natural-language-to-policy-response.json", {"queries": []})
        write_run(tmp_path, "r.json", [row()])
        assert common.run_cited_sources(tmp_path) == {"sources/a.md": 1}

    def test_corrupt_json_raises_nothing(self, tmp_path):
        (tmp_path / "runs").mkdir()
        (tmp_path / "runs" / "bad.json").write_text("{not json at all", encoding="utf-8")
        write_run(tmp_path, "r.json", [row()])
        assert common.run_cited_sources(tmp_path) == {"sources/a.md": 1}

    def test_undecodable_bytes_raise_nothing(self, tmp_path):
        (tmp_path / "runs").mkdir()
        (tmp_path / "runs" / "bad.json").write_bytes(b"\xff\xfe\x00binary")
        write_run(tmp_path, "r.json", [row()])
        assert common.run_cited_sources(tmp_path) == {"sources/a.md": 1}

    def test_unreadable_file_raises_nothing(self, tmp_path):
        """A directory named *.json reads as an OSError, not a parse failure."""
        (tmp_path / "runs").mkdir()
        (tmp_path / "runs" / "adir.json").mkdir()
        write_run(tmp_path, "r.json", [row()])
        assert common.run_cited_sources(tmp_path) == {"sources/a.md": 1}

    def test_missing_runs_dir_is_empty(self, tmp_path):
        assert common.run_cited_sources(tmp_path) == {}


class TestPattern:
    def test_pattern_is_overridable(self, tmp_path):
        write_run(tmp_path, "r.json", [row()])
        (tmp_path / "runs" / "sub").mkdir()
        (tmp_path / "runs" / "sub" / "s.json").write_text(
            json.dumps([row(source="sources/b.md")]), encoding="utf-8"
        )
        assert common.run_cited_sources(tmp_path) == {"sources/a.md": 1}
        assert common.run_cited_sources(tmp_path, "runs/sub/*.json") == {"sources/b.md": 1}


class TestAgreesWithMerge:
    def test_key_matches_the_ref_merge_compares(self, tmp_path):
        """The two sides of the compare, derived from the same fixture: what the
        run rows ask for (this helper) minus what is on disk (source_file_refs)
        is exactly the set merge drops."""
        (tmp_path / "sources").mkdir()
        (tmp_path / "sources" / "live.md").write_text("live\n", encoding="utf-8")
        write_run(
            tmp_path,
            "r.json",
            [row(source=" sources/live.md#sec "), row(source="sources/ghost.md"), row(source="sources/ghost.md", object="병서비스")],
        )
        cited = common.run_cited_sources(tmp_path)
        on_disk = common.source_file_refs(tmp_path)
        assert cited == {"sources/live.md": 1, "sources/ghost.md": 2}
        assert set(cited) - on_disk == {"sources/ghost.md"}
