# SPDX-License-Identifier: Apache-2.0
"""accept/reject must key run rows by merge's fact identity, common.fact_key (#477).

merge treats (subject, relation, object, source-file) as one fact -- with the amount
object canonicalised and only the source Unicode-folded -- so the same triple asserted
by two sources, or the same-looking triple stored in two Unicode forms, is two rows a
human decides separately. When accept/reject keyed run rows their own way, deciding one
row also flipped the other, and the next merge (which rebuilds candidates.csv FROM
runs/*.json) retired a confirmed fact nothing had asked to retire.
"""
from __future__ import annotations

import argparse
import csv
import json
import unicodedata

from factlog import cli
from factlog.common import FACT_HEADER, fact_key


def _row(subject, relation, obj, source, status, confidence="0.90", note=""):
    return {
        "subject": subject,
        "relation": relation,
        "object": obj,
        "source": source,
        "status": status,
        "confidence": confidence,
        "note": note,
    }


def _kb(tmp_path, csv_rows, run_files, sources=("note1.md", "note2.md")):
    """A KB with hand-written candidates.csv and runs/*.json.

    *run_files* is either a list (written to runs/r1.json) or a {name: rows} map, so a
    test can spread rows for one fact over several run files.
    """
    for d in ("facts", "runs", "sources", "pages", "decisions", "policy"):
        (tmp_path / d).mkdir()
    with (tmp_path / "facts" / "candidates.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(FACT_HEADER))
        w.writeheader()
        for r in csv_rows:
            w.writerow(r)
    if isinstance(run_files, list):
        run_files = {"r1.json": run_files}
    for name, rows in run_files.items():
        (tmp_path / "runs" / name).write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8"
        )
    for name in sources:
        (tmp_path / "sources" / name).write_text("메모\n", encoding="utf-8")
    return tmp_path


def _invoke(tmp_path, terms, new_status, verb, dry_run=False):
    args = argparse.Namespace(terms=terms, target=str(tmp_path), dry_run=dry_run)
    return cli._apply_review_status(args, new_status, verb)


def _run_rows(tmp_path):
    """Every run row, in a stable order (file name, then position in file)."""
    out = []
    for jp in sorted((tmp_path / "runs").glob("*.json")):
        for row in json.loads(jp.read_text(encoding="utf-8")):
            out.append((jp.name, row))
    return out


def _csv_rows(tmp_path):
    with (tmp_path / "facts" / "candidates.csv").open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _keys_of_changed(before, after):
    """fact_key of every row whose status moved between the two snapshots."""
    assert len(before) == len(after)
    return {
        fact_key(a["subject"], a["relation"], a["object"], a["source"])
        for b, a in zip(before, after)
        if (b.get("status") or "") != (a.get("status") or "")
    }


def _by_source(tmp_path):
    return {row["source"]: row["status"] for _, row in _run_rows(tmp_path)}


def _reported(out):
    """(csv rows changed, run rows changed) as the command reported them."""
    import re

    m = re.search(r"(\d+) candidate row\(s\) → \S+, (\d+) runs/\*\.json row\(s\)", out)
    assert m, out
    return int(m.group(1)), int(m.group(2))


class TestMultiSourceEvidenceIsScoped:
    """One source's decision must not move another source's evidence row."""

    def test_exact_triple_reject_leaves_other_sources_run_row_alone(self, tmp_path):
        # note1 was confirmed by a human before #233, so its run row still drifted at
        # `candidate`; note2 is the only pending row and the only one reject may move.
        kb = _kb(
            tmp_path,
            [
                _row("A", "R", "X", "sources/note1.md", "confirmed"),
                _row("A", "R", "X", "sources/note2.md", "candidate"),
            ],
            [
                _row("A", "R", "X", "sources/note1.md", "candidate"),
                _row("A", "R", "X", "sources/note2.md", "candidate"),
            ],
        )
        _invoke(kb, ["A", "R", "X"], "superseded", "reject")

        assert _by_source(kb) == {
            "sources/note1.md": "candidate",
            "sources/note2.md": "superseded",
        }

    def test_exact_triple_accept_leaves_other_sources_run_row_alone(self, tmp_path):
        kb = _kb(
            tmp_path,
            [
                _row("A", "R", "X", "sources/note1.md", "confirmed"),
                _row("A", "R", "X", "sources/note2.md", "needs_review"),
            ],
            [
                _row("A", "R", "X", "sources/note1.md", "candidate"),
                _row("A", "R", "X", "sources/note2.md", "needs_review"),
            ],
        )
        _invoke(kb, ["A", "R", "X"], "accepted", "accept")

        assert _by_source(kb) == {
            "sources/note1.md": "candidate",
            "sources/note2.md": "accepted",
        }

    def test_wildcard_reject_leaves_other_sources_run_row_alone(self, tmp_path):
        kb = _kb(
            tmp_path,
            [
                _row("A", "R", "X", "sources/note1.md", "confirmed"),
                _row("A", "R", "X", "sources/note2.md", "candidate"),
            ],
            [
                _row("A", "R", "X", "sources/note1.md", "candidate"),
                _row("A", "R", "X", "sources/note2.md", "candidate"),
            ],
        )
        _invoke(kb, ["-", "R", "-"], "superseded", "reject")

        assert _by_source(kb) == {
            "sources/note1.md": "candidate",
            "sources/note2.md": "superseded",
        }

    def test_anchor_in_run_source_still_matches(self, tmp_path):
        """merge keys on the pre-'#anchor' path, so the decision must reach an
        anchored run row -- otherwise durability (#233) breaks for anchored sources."""
        kb = _kb(
            tmp_path,
            [
                _row("A", "R", "X", "sources/note1.md#s1", "candidate"),
                _row("A", "R", "X", "sources/note2.md", "confirmed"),
            ],
            [
                _row("A", "R", "X", "sources/note1.md#s1", "candidate"),
                _row("A", "R", "X", "sources/note2.md", "candidate"),
            ],
        )
        _invoke(kb, ["A", "R", "X"], "accepted", "accept")

        assert _by_source(kb) == {
            "sources/note1.md#s1": "accepted",
            "sources/note2.md": "candidate",
        }


class TestAmountObjectIsComparedCanonically:
    """merge canonicalises an amount BEFORE keying, so the CLI must too (#477 C1).

    candidates.csv holds the canonical `amount(N,"unit")` merge wrote; the run row it was
    built from may still carry the bare or comma-grouped form. Comparing them verbatim
    left the decision out of runs/*.json entirely -- #233 durability, broken again.
    """

    def test_bare_unit_run_row_receives_the_decision(self, tmp_path):
        kb = _kb(
            tmp_path,
            [_row("A", "costs", 'amount(7,"억")', "sources/note1.md", "candidate")],
            [_row("A", "costs", "amount(7,억)", "sources/note1.md", "candidate")],
        )
        _invoke(kb, ["A", "costs", 'amount(7,"억")'], "accepted", "accept")

        assert _by_source(kb) == {"sources/note1.md": "accepted"}

    def test_thousands_separator_run_row_receives_the_decision(self, tmp_path):
        kb = _kb(
            tmp_path,
            [_row("A", "costs", 'amount(1000,"원")', "sources/note1.md", "candidate")],
            [_row("A", "costs", "amount(1,000,원)", "sources/note1.md", "candidate")],
        )
        _invoke(kb, ["A", "costs", 'amount(1000,"원")'], "accepted", "accept")

        assert _by_source(kb) == {"sources/note1.md": "accepted"}

    def test_reported_run_count_counts_the_amount_row(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            [_row("A", "costs", 'amount(7,"억")', "sources/note1.md", "candidate")],
            [_row("A", "costs", "amount(7,억)", "sources/note1.md", "candidate")],
        )
        _invoke(kb, ["A", "costs", 'amount(7,"억")'], "accepted", "accept")
        assert _reported(capsys.readouterr().out) == (1, 1)


class TestUnicodeFormsStayDistinctFacts:
    """merge folds only the SOURCE to NFC; content values it stores verbatim (#477 C2).

    So an NFC subject and an NFD subject are two facts on disk. If the CLI folds them
    together, a decision on the pending one also flips the confirmed one's run row, and
    the confirmed fact drops out of accepted.dl on the next merge -- the original #477
    failure mode, reached without any wildcard trickery in the data.
    """

    NFC = unicodedata.normalize("NFC", "가나")
    NFD = unicodedata.normalize("NFD", "가나")

    def test_wildcard_reject_leaves_the_other_unicode_form_alone(self, tmp_path):
        kb = _kb(
            tmp_path,
            [
                _row(self.NFD, "R", "X", "sources/note1.md", "confirmed"),
                _row(self.NFC, "R", "X", "sources/note1.md", "candidate"),
            ],
            [
                _row(self.NFD, "R", "X", "sources/note1.md", "candidate"),
                _row(self.NFC, "R", "X", "sources/note1.md", "candidate"),
            ],
        )
        _invoke(kb, ["-", "R", "-"], "superseded", "reject")

        statuses = {
            "NFD" if not unicodedata.is_normalized("NFC", row["subject"]) else "NFC": row["status"]
            for _, row in _run_rows(kb)
        }
        assert statuses == {"NFD": "candidate", "NFC": "superseded"}

    def test_lookup_still_finds_a_row_typed_in_the_other_form(self, tmp_path):
        """Matching stays lenient on purpose: we do not control the IME's output."""
        kb = _kb(
            tmp_path,
            [_row(self.NFD, "R", "X", "sources/note1.md", "candidate")],
            [_row(self.NFD, "R", "X", "sources/note1.md", "candidate")],
        )
        rc = _invoke(kb, [self.NFC, "R", "X"], "accepted", "accept")

        assert rc == 0
        assert [row["status"] for _, row in _run_rows(kb)] == ["accepted"]


class TestSourcelessRowIsNotAnIdentity:
    """A row with no source names no fact merge can find, so it decides no run row.

    merge drops such a row (its source is not under sources/), so it only reaches
    candidates.csv by hand-editing. Keying on it would make the blank source match every
    other sourceless run row at once -- a decision spilling onto rows nobody decided.
    """

    def test_blank_source_csv_row_writes_no_run_row(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            [_row("A", "R", "X", "", "candidate")],
            [
                _row("A", "R", "X", "", "candidate"),
                _row("A", "R", "X", "sources/note1.md", "candidate"),
            ],
        )
        _invoke(kb, ["A", "R", "X"], "accepted", "accept")

        assert [r["status"] for _, r in _run_rows(kb)] == ["candidate", "candidate"]
        assert _reported(capsys.readouterr().out) == (1, 0)


class TestRunScopeInvariant:
    """Every run row this command moved must be a row the CSV gate decided.

    Stated as CONTAINMENT, not as a count: `runs_changed <= csv_changed` is false.
    merge dedups anchor-insensitively, so one candidates.csv row can be backed by
    several run rows (bare path + anchored variant), and a single decision legitimately
    updates all of them.
    """

    def test_multi_source_exact_triple(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            [
                _row("A", "R", "X", "sources/note1.md", "confirmed"),
                _row("A", "R", "X", "sources/note2.md", "candidate"),
            ],
            [
                _row("A", "R", "X", "sources/note1.md", "candidate"),
                _row("A", "R", "X", "sources/note2.md", "candidate"),
            ],
        )
        csv_before, runs_before = _csv_rows(kb), [r for _, r in _run_rows(kb)]
        _invoke(kb, ["A", "R", "X"], "superseded", "reject")
        moved_runs = _keys_of_changed(runs_before, [r for _, r in _run_rows(kb)])
        moved_csv = _keys_of_changed(csv_before, _csv_rows(kb))

        assert moved_runs <= moved_csv
        assert moved_runs == {("A", "R", "X", "sources/note2.md")}
        assert _reported(capsys.readouterr().out) == (1, 1)

    def test_multi_source_wildcard(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            [
                _row("A", "R", "X", "sources/note1.md", "confirmed"),
                _row("A", "R", "X", "sources/note2.md", "candidate"),
                _row("B", "R", "Y", "sources/note1.md", "candidate"),
            ],
            [
                _row("A", "R", "X", "sources/note1.md", "candidate"),
                _row("A", "R", "X", "sources/note2.md", "candidate"),
                _row("B", "R", "Y", "sources/note1.md", "candidate"),
            ],
        )
        csv_before, runs_before = _csv_rows(kb), [r for _, r in _run_rows(kb)]
        _invoke(kb, ["-", "R", "-"], "accepted", "accept")
        moved_runs = _keys_of_changed(runs_before, [r for _, r in _run_rows(kb)])
        moved_csv = _keys_of_changed(csv_before, _csv_rows(kb))

        assert moved_runs <= moved_csv
        assert {(r["subject"], r["source"]): r["status"] for _, r in _run_rows(kb)} == {
            ("A", "sources/note1.md"): "candidate",  # confirmed in csv, untouched
            ("A", "sources/note2.md"): "accepted",
            ("B", "sources/note1.md"): "accepted",
        }
        assert _reported(capsys.readouterr().out) == (2, 2)

    def test_anchor_fanout_updates_more_run_rows_than_csv_rows(self, tmp_path, capsys):
        """1 csv row / 2 run rows is CORRECT here: merge collapsed the anchored variant
        into the bare one, so both run rows back that single decided row."""
        kb = _kb(
            tmp_path,
            [_row("A", "R", "X", "sources/note1.md", "candidate")],
            {
                "r1.json": [_row("A", "R", "X", "sources/note1.md", "candidate")],
                "r2.json": [_row("A", "R", "X", "sources/note1.md#s2", "candidate")],
            },
        )
        csv_before, runs_before = _csv_rows(kb), [r for _, r in _run_rows(kb)]
        _invoke(kb, ["A", "R", "X"], "accepted", "accept")
        moved_runs = _keys_of_changed(runs_before, [r for _, r in _run_rows(kb)])
        moved_csv = _keys_of_changed(csv_before, _csv_rows(kb))

        assert moved_runs <= moved_csv
        assert _reported(capsys.readouterr().out) == (1, 2)
        assert [r["status"] for _, r in _run_rows(kb)] == ["accepted", "accepted"]
