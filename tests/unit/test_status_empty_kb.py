# SPDX-License-Identifier: Apache-2.0
"""``factlog status`` on a freshly ``init``ed KB must not claim a missing file (#327).

Since ``init`` scaffolds ``facts/candidates.csv``, the empty-facts line's old
text ("no facts/candidates.csv") was false at exactly the moment a new user
reads it. The line is keyed on the row list being empty, so it must distinguish
"the file is there and holds zero rows" from "the file is genuinely absent" —
the latter is a breakage ``validate`` reports as an error, and status is where a
user goes to see it.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from factlog.cli import _init_kb, cmd_status  # noqa: E402


@pytest.fixture()
def fresh_kb(tmp_path, capsys):
    kb = tmp_path / "kb"
    _init_kb(kb)
    capsys.readouterr()
    return kb


def _status_lines(kb, capsys):
    assert cmd_status(argparse.Namespace(target=str(kb))) == 0
    return capsys.readouterr().out.splitlines()


def _facts_line(lines):
    return next(line for line in lines if line.strip().startswith("facts:"))


def test_fresh_kb_does_not_claim_the_ledger_is_missing(fresh_kb, capsys):
    line = _facts_line(_status_lines(fresh_kb, capsys))
    assert "no facts/candidates.csv" not in line, line
    assert "0 rows" in line, line


def test_deleted_ledger_still_says_so(fresh_kb, capsys):
    # Control: passes before and after the fix. It is here so the fix cannot be
    # taken the lazy way — dropping the missing-file wording altogether would
    # satisfy the test above and lose a real breakage signal.
    (fresh_kb / "facts" / "candidates.csv").unlink()
    line = _facts_line(_status_lines(fresh_kb, capsys))
    assert "no facts/candidates.csv" in line, line
