# SPDX-License-Identifier: Apache-2.0
"""compile_facts refuses to compile while a single-valued contradiction stands (#327).

/factlog check is compile_facts → run_logic_check (SKILL.md), and the check_conflicts gate
lived only in finalize — so a finalize that deleted accepted.dl to heal a contradiction
(#212) was undone by the very next /factlog check, which recompiled the contradictory rows
straight back and blessed them errors: 0. compile_facts now gates before any write: on a
contradiction nothing is written, a stale accepted.dl is removed, and it exits non-zero, so
the #212 invariant holds across commands. Deterministic (candidates.csv only) — no pyrewire.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

HEADER = "subject,relation,object,source,status,confidence,note"


def _kb(tmp_path, rows, *, single_valued="주_속성"):
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True, check=True,
    )
    (kb / "sources" / "x.md").write_text("x\n")
    if single_valued is not None:
        (kb / "policy" / "single-valued.md").write_text(
            f"# single-valued relations\n\n- {single_valued}\n", encoding="utf-8"
        )
    (kb / "facts" / "candidates.csv").write_text(
        "\n".join([HEADER, *rows]) + "\n", encoding="utf-8"
    )
    return kb


def _compile(kb):
    return subprocess.run(
        [sys.executable, str(Path("tools") / "compile_facts.py")],
        capture_output=True, text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )


_CONFLICT_ROWS = [
    "을서비스,주_속성,값가,sources/x.md,confirmed,0.9,",
    "을서비스,주_속성,값나,sources/x.md,confirmed,0.9,",
]


def test_contradiction_makes_compile_fail_and_writes_nothing(tmp_path):
    kb = _kb(tmp_path, _CONFLICT_ROWS)
    accepted = kb / "facts" / "accepted.dl"
    assert not accepted.exists()  # fresh KB, nothing compiled yet
    proc = _compile(kb)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "CONFLICT" in proc.stderr, proc.stdout + proc.stderr
    # The contradiction never enters the engine's trusted input.
    assert not accepted.exists(), "a contradiction was compiled into accepted.dl"


def test_gate_removes_a_stale_accepted_dl_so_check_cannot_resurrect_it(tmp_path):
    # Reproduce the #212-across-commands scenario: a poisoned accepted.dl already on
    # disk (e.g. left by a pre-#212 finalize) while the contradiction still stands. The
    # next /factlog check (compile step) must REMOVE it, not recompile it back.
    kb = _kb(tmp_path, _CONFLICT_ROWS)
    accepted = kb / "facts" / "accepted.dl"
    accepted.write_text(
        'relation("을서비스", "주_속성", "값가").\n'
        'relation("을서비스", "주_속성", "값나").\n',
        encoding="utf-8",
    )
    proc = _compile(kb)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert not accepted.exists(), "stale poisoned accepted.dl survived the conflict gate"
    assert "removed" in proc.stderr


def test_resolved_kb_compiles(tmp_path):
    # Superseding the outdated row clears the contradiction; compile proceeds normally.
    kb = _kb(tmp_path, [
        "을서비스,주_속성,값가,sources/x.md,superseded,0.9,old",
        "을서비스,주_속성,값나,sources/x.md,confirmed,0.9,current",
    ])
    proc = _compile(kb)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    accepted = (kb / "facts" / "accepted.dl").read_text(encoding="utf-8")
    assert '"값나"' in accepted and '"값가"' not in accepted


def test_no_single_valued_policy_is_a_noop_gate(tmp_path):
    # Without policy/single-valued.md the gate has nothing to enforce and compile runs.
    kb = _kb(tmp_path, _CONFLICT_ROWS, single_valued=None)
    proc = _compile(kb)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (kb / "facts" / "accepted.dl").is_file()
