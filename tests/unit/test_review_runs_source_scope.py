# SPDX-License-Identifier: Apache-2.0
"""accept/reject must key run rows by fact identity, not by triple alone (#477).

merge treats (subject, relation, object, source-file) as one fact, so the same triple
asserted by two sources is two rows a human decides separately. When accept/reject
matched run rows on the triple only, deciding source B's row also flipped source A's
run row -- and the next merge, which rebuilds candidates.csv FROM runs/*.json, retired
A's confirmed fact nothing had asked to retire. This holds for an EXACT triple, no
wildcard involved.
"""
from __future__ import annotations

import argparse
import json

from factlog import cli

HEADER = "subject,relation,object,source,status,confidence,note\n"


def _kb(tmp_path, csv_rows, run_rows, sources=("note1.md", "note2.md")):
    for d in ("facts", "runs", "sources", "pages", "decisions", "policy"):
        (tmp_path / d).mkdir()
    (tmp_path / "facts" / "candidates.csv").write_text(
        HEADER + "".join(r + "\n" for r in csv_rows), encoding="utf-8"
    )
    (tmp_path / "runs" / "r1.json").write_text(
        json.dumps(run_rows, ensure_ascii=False), encoding="utf-8"
    )
    for name in sources:
        (tmp_path / "sources" / name).write_text("메모\n", encoding="utf-8")
    return tmp_path


def _run_row(subject, obj, source, status, relation="R"):
    return {
        "subject": subject,
        "relation": relation,
        "object": obj,
        "source": source,
        "status": status,
        "confidence": "0.9",
        "note": "",
    }


def _invoke(tmp_path, terms, new_status, verb, dry_run=False):
    args = argparse.Namespace(terms=terms, target=str(tmp_path), dry_run=dry_run)
    return cli._apply_review_status(args, new_status, verb)


def _by_source(tmp_path):
    rows = json.loads((tmp_path / "runs" / "r1.json").read_text(encoding="utf-8"))
    return {r["source"]: r["status"] for r in rows}


class TestMultiSourceEvidenceIsScoped:
    """One source's decision must not move another source's evidence row."""

    def test_exact_triple_reject_leaves_other_sources_run_row_alone(self, tmp_path):
        # note1 was confirmed by a human before #233, so its run row still drifted at
        # `candidate`; note2 is the only pending row and the only one reject may move.
        kb = _kb(
            tmp_path,
            [
                "A,R,X,sources/note1.md,confirmed,0.90,",
                "A,R,X,sources/note2.md,candidate,0.90,",
            ],
            [
                _run_row("A", "X", "sources/note1.md", "candidate"),
                _run_row("A", "X", "sources/note2.md", "candidate"),
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
                "A,R,X,sources/note1.md,confirmed,0.90,",
                "A,R,X,sources/note2.md,needs_review,0.90,",
            ],
            [
                _run_row("A", "X", "sources/note1.md", "candidate"),
                _run_row("A", "X", "sources/note2.md", "needs_review"),
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
                "A,R,X,sources/note1.md,confirmed,0.90,",
                "A,R,X,sources/note2.md,candidate,0.90,",
            ],
            [
                _run_row("A", "X", "sources/note1.md", "candidate"),
                _run_row("A", "X", "sources/note2.md", "candidate"),
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
                "A,R,X,sources/note1.md#s1,candidate,0.90,",
                "A,R,X,sources/note2.md,confirmed,0.90,",
            ],
            [
                _run_row("A", "X", "sources/note1.md#s1", "candidate"),
                _run_row("A", "X", "sources/note2.md", "candidate"),
            ],
        )
        _invoke(kb, ["A", "R", "X"], "accepted", "accept")

        assert _by_source(kb) == {
            "sources/note1.md#s1": "accepted",
            "sources/note2.md": "candidate",
        }


class TestRunCountInvariant:
    """The reported runs count may never exceed the pending rows actually decided."""

    def _reported(self, out):
        # "... N candidate row(s) → S, M runs/*.json row(s) updated; ..."
        import re

        m = re.search(r"(\d+) candidate row\(s\) → \S+, (\d+) runs/\*\.json row\(s\)", out)
        assert m, out
        return int(m.group(1)), int(m.group(2))

    def test_multi_source_exact_triple(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            [
                "A,R,X,sources/note1.md,confirmed,0.90,",
                "A,R,X,sources/note2.md,candidate,0.90,",
            ],
            [
                _run_row("A", "X", "sources/note1.md", "candidate"),
                _run_row("A", "X", "sources/note2.md", "candidate"),
            ],
        )
        _invoke(kb, ["A", "R", "X"], "superseded", "reject")
        csv_changed, runs_changed = self._reported(capsys.readouterr().out)
        assert (csv_changed, runs_changed) == (1, 1)
        assert runs_changed <= csv_changed

    def test_multi_source_wildcard(self, tmp_path, capsys):
        kb = _kb(
            tmp_path,
            [
                "A,R,X,sources/note1.md,confirmed,0.90,",
                "A,R,X,sources/note2.md,candidate,0.90,",
                "B,R,Y,sources/note1.md,candidate,0.90,",
            ],
            [
                _run_row("A", "X", "sources/note1.md", "candidate"),
                _run_row("A", "X", "sources/note2.md", "candidate"),
                _run_row("B", "Y", "sources/note1.md", "candidate"),
            ],
        )
        _invoke(kb, ["-", "R", "-"], "accepted", "accept")
        csv_changed, runs_changed = self._reported(capsys.readouterr().out)
        assert (csv_changed, runs_changed) == (2, 2)
        assert runs_changed <= csv_changed
        rows = json.loads((kb / "runs" / "r1.json").read_text(encoding="utf-8"))
        assert {(r["subject"], r["source"]): r["status"] for r in rows} == {
            ("A", "sources/note1.md"): "candidate",  # confirmed in csv, untouched
            ("A", "sources/note2.md"): "accepted",
            ("B", "sources/note1.md"): "accepted",
        }
