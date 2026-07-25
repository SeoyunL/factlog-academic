# SPDX-License-Identifier: Apache-2.0
"""Every candidates.csv producer writes LF line endings, byte-for-byte (#503).

`factlog init` scaffolds `facts/candidates.csv` as `",".join(FACT_HEADER) + "\n"`
(LF), and the committed `examples/sample-kb` fixture is LF-only. But the four
producers that *rewrite* candidates.csv used `csv.DictWriter`'s default excel
dialect, which terminates every row with CRLF. The result was a flip-flop: a
sync (`write_facts`) wrote LF while accept/amend/priority/review rewrote the same
file as CRLF, so the header alone was never byte-stable across the normal
lifecycle.

These tests pin each of the four producers separately to the init scaffold's
exact bytes — header line AND data rows end with a lone LF, never CRLF. Four
distinct assertions on purpose (issue #503): the four producers now route
through one helper (`common.candidates_csv_writer`), so a mutant that bypasses it
at a *single* call site — reverting that one writer to the default CRLF dialect —
must be caught by that writer's own test. Folding them into one assertion would
let three producers regress to CRLF undetected.
"""
from __future__ import annotations

import argparse

import pytest

from factlog import cli
from factlog import common as factlog_common
from factlog.common import FACT_HEADER

# The init scaffold IS the reference. cli._TEMPLATES["facts/candidates.csv"] is
# what `factlog init` writes verbatim; every producer's header must match it byte
# for byte. Derived from the module so a change to the scaffold moves the target
# rather than silently diverging from this literal.
INIT_SCAFFOLD_BYTES = cli._TEMPLATES["facts/candidates.csv"].encode("utf-8")
EXPECTED_HEADER_LINE = (",".join(FACT_HEADER) + "\n").encode("utf-8")

# Two independent spellings of the same header agree — the init scaffold and the
# FACT_HEADER-join formula the producers pin against are one and the same bytes.
def test_init_scaffold_is_lf_header():
    assert INIT_SCAFFOLD_BYTES == EXPECTED_HEADER_LINE
    assert b"\r\n" not in INIT_SCAFFOLD_BYTES


def _sample_rows() -> list[dict[str, str]]:
    return [
        {
            "subject": "Alice",
            "relation": "wrote",
            "object": "Paper A",
            "source": "sources/a.md",
            "status": "needs_review",
            "confidence": "0.8",
            "note": "",
        },
        {
            "subject": "Bob",
            "relation": "wrote",
            "object": "Paper B",
            "source": "sources/b.md",
            "status": "candidate",
            "confidence": "",
            "note": "line-end pin",
        },
    ]


def _assert_lf_only(data: bytes, n_rows: int) -> None:
    """The bytes are a header + data rows, each terminated by a lone LF."""
    # Header line is byte-identical to the init scaffold.
    assert data.split(b"\n", 1)[0] + b"\n" == EXPECTED_HEADER_LINE
    # No CRLF anywhere — the excel default is gone.
    assert b"\r\n" not in data
    assert b"\r" not in data
    # Header + one LF per data row (final row LF-terminated, no trailing blank).
    assert data.count(b"\n") == n_rows + 1


def test_write_facts_lf(tmp_path):
    """Producer 1: tools/merge_candidates.write_facts (sync rebuild)."""
    import merge_candidates

    rows = _sample_rows()
    merge_candidates.write_facts(tmp_path, rows)
    data = (tmp_path / "facts" / "candidates.csv").read_bytes()
    _assert_lf_only(data, len(rows))


def test_atomic_write_csv_lf(tmp_path):
    """Producer 2: cli._atomic_write_csv (accept/reject/amend/supersede)."""
    rows = _sample_rows()
    out = tmp_path / "candidates.csv"
    cli._atomic_write_csv(out, rows, FACT_HEADER)
    _assert_lf_only(out.read_bytes(), len(rows))


def test_review_write_candidate_rows_lf(tmp_path, monkeypatch):
    """Producer 3: tools/review_candidates.write_candidate_rows (review CLI)."""
    import review_candidates

    out = tmp_path / "facts" / "candidates.csv"
    monkeypatch.setattr(review_candidates, "CANDIDATES_CSV", out)
    rows = _sample_rows()
    review_candidates.write_candidate_rows(rows)
    _assert_lf_only(out.read_bytes(), len(rows))


def test_migrate_unicode_priority_lf(tmp_path):
    """Producer 4: cli.cmd_migrate_unicode --resolve-status=priority.

    Exercises the real command (not just the helper) so a mutant that reverts
    only the priority write site to the default CRLF dialect is caught. Two rows
    that fold to one NFC fact_key form a collision, which triggers the rewrite.
    """
    kb = tmp_path / "kb"
    (kb / "sources").mkdir(parents=True)
    facts_dir = kb / "facts"
    facts_dir.mkdir()
    csv_path = facts_dir / "candidates.csv"
    # "café": NFC (precomposed é) vs NFD (e + combining acute). fact_key folds
    # both to NFC, so they collapse to one group → a collision priority folds.
    header = ",".join(FACT_HEADER)
    nfc = "café"
    nfd = "café"
    csv_path.write_text(
        header
        + "\n"
        + f"{nfc},wrote,Paper,sources/x.md,confirmed,0.9,\n"
        + f"{nfd},wrote,Paper,sources/x.md,needs_review,0.5,\n",
        encoding="utf-8",
    )

    args = argparse.Namespace(target=str(kb), resolve_status="priority")
    rc = cli.cmd_migrate_unicode(args)
    assert rc == 0

    data = csv_path.read_bytes()
    # One survivor row after folding the collision.
    _assert_lf_only(data, 1)


@pytest.mark.parametrize("extrasaction", ["raise", "ignore"])
def test_helper_dialect_is_lf(extrasaction):
    """The shared helper hardcodes the LF terminator regardless of extrasaction."""
    import io

    buf = io.StringIO()
    writer = factlog_common.candidates_csv_writer(
        buf, FACT_HEADER, extrasaction=extrasaction
    )
    writer.writeheader()
    assert buf.getvalue().encode("utf-8") == EXPECTED_HEADER_LINE
