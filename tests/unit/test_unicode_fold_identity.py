# SPDX-License-Identifier: Apache-2.0
"""subject/relation/object are folded to NFC in the ONE fact identity (#482).

Before #482 fact_key folded only the source; an NFC and an NFD spelling of the same
subject/object were TWO facts, which the review docs told a human to reconcile by hand.
The decision (user-chosen) is to unify every field to NFC, so the two spellings are ONE
fact end to end. These assert the OBSERVABLE consequences:

* merge collapses the two spellings to a single candidates.csv row, and the value it
  STORES is on NFC form (so key, CSV and engine stay one consistent form);
* an accept keyed on that single row reaches the run rows behind BOTH spellings;
* the one-shot `migrate-unicode` command reports a post-fold collision, changes nothing
  by default, and folds deterministically only with --resolve-status=priority — keeping
  the survivor's own confidence while REPORTING the discarded ones.
"""
from __future__ import annotations

import csv
import json
import unicodedata
from types import SimpleNamespace

import merge_candidates as mc
import pytest

from factlog import cli
from factlog.common import fact_key

NFC = unicodedata.normalize("NFC", "가나")
NFD = unicodedata.normalize("NFD", "가나")
# amount unit with an NFD vs NFC combining spelling ("억"/"만" are single code points;
# use a syllable that decomposes to prove the unit folds inside an amount compound).
UNIT_NFC = unicodedata.normalize("NFC", "톤")
UNIT_NFD = unicodedata.normalize("NFD", "톤")


def _kb(tmp_path, names=("a.md",)):
    (tmp_path / "sources").mkdir()
    for name in names:
        (tmp_path / "sources" / name).write_text("# heading\n", encoding="utf-8")
    return tmp_path


def _row(subject, relation, obj, source, status="candidate", confidence="0.50", note=""):
    return {
        "subject": subject,
        "relation": relation,
        "object": obj,
        "source": source,
        "status": status,
        "confidence": confidence,
        "note": note,
    }


def _write_candidates(kb, rows):
    facts = kb / "facts"
    facts.mkdir(exist_ok=True)
    header = ["subject", "relation", "object", "source", "status", "confidence", "note"]
    with (facts / "candidates.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return facts / "candidates.csv"


# --- merge observable: two spellings collapse and the STORED value is NFC ----------


@pytest.mark.parametrize(
    "field",
    ["subject", "relation", "object"],
)
def test_nfd_and_nfc_spelling_collapse_to_one_row(tmp_path, field):
    kb = _kb(tmp_path)
    base = {"subject": "A", "relation": "R", "obj": "X", "source": "sources/a.md"}
    key = "obj" if field == "object" else field
    r_nfc = dict(base, **{key: NFC})
    r_nfd = dict(base, **{key: NFD})
    out = mc.normalize_rows(kb, [_row(**r_nfc), _row(**r_nfd)])
    assert len(out) == 1, f"{field}: NFC and NFD spelling must be ONE fact"
    stored = out[0][field]
    assert stored == unicodedata.normalize("NFC", stored), f"{field}: stored value must be NFC"
    assert stored == NFC


def test_amount_unit_nfd_and_nfc_collapse(tmp_path):
    kb = _kb(tmp_path)
    out = mc.normalize_rows(
        kb,
        [
            _row("A", "weighs", f'amount(7,"{UNIT_NFC}")', "sources/a.md"),
            _row("A", "weighs", f'amount(7,"{UNIT_NFD}")', "sources/a.md"),
        ],
    )
    assert len(out) == 1, "an NFD vs NFC amount unit must fold to ONE fact"
    assert out[0]["object"] == unicodedata.normalize("NFC", out[0]["object"])


def test_run_row_and_stored_row_key_to_same_fact(tmp_path):
    """A run row in the OTHER spelling must key to the fact merge stored, so an accept
    keyed on the stored row finds it."""
    kb = _kb(tmp_path)
    (stored,) = mc.normalize_rows(kb, [_row(NFC, "R", "X", "sources/a.md")])
    stored_key = fact_key(stored["subject"], stored["relation"], stored["object"], stored["source"])
    run_key = fact_key(NFD, "R", "X", "sources/a.md")
    assert stored_key == run_key


# --- accept reaches the run rows behind BOTH spellings ------------------------------


def test_accept_reaches_both_notations(tmp_path):
    kb = _kb(tmp_path)
    # runs/*.json holds the same fact in two Unicode forms (macOS paste + extractor).
    runs = kb / "runs"
    runs.mkdir()
    (runs / "r.json").write_text(
        json.dumps(
            [
                _row(NFC, "R", "X", "sources/a.md", status="candidate"),
                _row(NFD, "R", "X", "sources/a.md", status="candidate"),
            ]
        ),
        encoding="utf-8",
    )
    # candidates.csv is what merge would write: ONE NFC row.
    _write_candidates(kb, [_row(NFC, "R", "X", "sources/a.md", status="candidate")])

    args = SimpleNamespace(
        terms=[NFC, "R", "X"], target=str(kb), dry_run=False
    )
    rc = cli.cmd_accept(args)
    assert rc in (0, 1)  # 1 only if recompile fails (no engine in tmp KB)

    data = json.loads((runs / "r.json").read_text(encoding="utf-8"))
    statuses = [d["status"] for d in data]
    assert statuses == ["accepted", "accepted"], "accept must reach BOTH spellings' run rows"


# --- migrate-unicode: report by default, fold only on opt-in ------------------------


def _migrate(kb, resolve_status=None):
    return cli.cmd_migrate_unicode(
        SimpleNamespace(target=str(kb), resolve_status=resolve_status)
    )


def test_migrate_no_targets_when_single_form(tmp_path, capsys):
    kb = _kb(tmp_path)
    _write_candidates(kb, [_row(NFC, "R", "X", "sources/a.md", status="confirmed")])
    rc = _migrate(kb)
    out = capsys.readouterr().out
    assert rc == 0
    assert "no targets" in out


def test_migrate_reports_conflict_and_changes_nothing(tmp_path, capsys):
    kb = _kb(tmp_path)
    csv_path = _write_candidates(
        kb,
        [
            _row(NFC, "R", "X", "sources/a.md", status="confirmed", confidence="0.90"),
            _row(NFD, "R", "X", "sources/a.md", status="superseded", confidence="0.10"),
        ],
    )
    before = csv_path.read_bytes()
    rc = _migrate(kb)  # default: no --resolve-status
    out = capsys.readouterr().out
    assert rc == 0
    assert "conflict" in out
    assert "1 with a status/confidence conflict" in out
    assert csv_path.read_bytes() == before, "default run must NOT rewrite the file"


def test_migrate_resolve_priority_folds_to_confirmed(tmp_path, capsys):
    kb = _kb(tmp_path)
    csv_path = _write_candidates(
        kb,
        [
            # survivor by status priority is the confirmed row, regardless of order/source
            _row(NFD, "R", "X", "sources/a.md", status="superseded", confidence="0.10"),
            _row(NFC, "R", "X", "sources/a.md", status="confirmed", confidence="0.90"),
        ],
    )
    rc = _migrate(kb, resolve_status="priority")
    out = capsys.readouterr().out
    assert rc == 0

    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1, "priority fold collapses the collision to one row"
    (kept,) = rows
    assert kept["status"] == "confirmed", "highest-priority status survives"
    assert kept["subject"] == NFC, "survivor value is stored on NFC form"
    # survivor keeps its OWN confidence, discarded ones are only REPORTED (never max'd in)
    assert kept["confidence"] == "0.90"
    assert "discarded confidence" in out
    assert "0.10" in out
    # priority is a WRITE mode: it must name the exact file it rewrites, right before the
    # write, so a run that reached the wrong KB via the active-KB default is visible.
    assert f"rewriting {csv_path}" in out
    # and it must point the operator at the re-merge that completes NFC unification.
    assert "re-merge" in out


def test_migrate_resolve_reports_discarded_confidence_not_max(tmp_path, capsys):
    kb = _kb(tmp_path)
    # Same status, differing confidence: survivor picked by source order, keeps its own.
    csv_path = _write_candidates(
        kb,
        [
            _row(NFC, "R", "X", "sources/a.md", status="candidate", confidence="0.20"),
            _row(NFD, "R", "X", "sources/a.md", status="candidate", confidence="0.99"),
        ],
    )
    rc = _migrate(kb, resolve_status="priority")
    out = capsys.readouterr().out
    assert rc == 0
    with csv_path.open(newline="", encoding="utf-8") as f:
        (kept,) = list(csv.DictReader(f))
    # survivor confidence is NOT the max — the higher one must appear as discarded.
    assert kept["confidence"] in ("0.20", "0.99")
    discarded = "0.99" if kept["confidence"] == "0.20" else "0.20"
    assert "discarded confidence" in out
    assert discarded in out


def test_migrate_help_warns_about_reviving_superseded():
    """priority can revive a retired (superseded) row; the --resolve-status help must
    say so, and flag that priority rewrites the KB immediately."""
    parser = cli.build_parser()
    action = next(
        a
        for a in parser._subparsers._group_actions[0].choices["migrate-unicode"]._actions
        if getattr(a, "dest", "") == "resolve_status"
    )
    help_text = (action.help or "").lower()
    assert "superseded" in help_text and "reviv" in help_text
    assert "rewrite" in help_text or "rewrites" in help_text
