# SPDX-License-Identifier: Apache-2.0
"""Unit + end-to-end tests for the typed side-relation projection (#119)."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import common
import pytest


class TestTypedDecls:
    def test_empty_specs_is_empty_string(self):
        # Byte-identity gate (#116 invariant 1): nothing projectable -> "".
        assert common._typed_decls({}) == ""

    def test_date_emits_int64_line(self):
        specs = {"정식_운영": common.TypedRelSpec("date", "launch_date")}
        assert common._typed_decls(specs) == "\n.decl launch_date(subject: symbol, v: int64)\n"

    def test_ordinal_emits_int64_line(self):
        specs = {"우선순위": common.TypedRelSpec("ordinal", "priority_rank")}
        assert common._typed_decls(specs) == "\n.decl priority_rank(subject: symbol, v: int64)\n"

    def test_number_is_skipped(self):
        # No float text column in this pyrewire build (#125 follow-up).
        specs = {"버전": common.TypedRelSpec("number", "version_num")}
        assert common._typed_decls(specs) == ""

    def test_sorted_by_alias(self):
        specs = {
            "b": common.TypedRelSpec("date", "zzz"),
            "a": common.TypedRelSpec("ordinal", "aaa"),
            "n": common.TypedRelSpec("number", "skipme"),
        }
        out = common._typed_decls(specs)
        assert out == (
            "\n.decl aaa(subject: symbol, v: int64)"
            "\n.decl zzz(subject: symbol, v: int64)\n"
        )
        assert "skipme" not in out


class TestAliasCollision:
    def test_collision_raises(self):
        specs = {"정식_운영": common.TypedRelSpec("date", "launch_date")}
        program = ".decl launch_date(s: symbol, d: int64)\n"
        with pytest.raises(common.FactlogError):
            common._assert_no_alias_collision(specs, program)

    def test_no_collision_ok(self):
        specs = {"정식_운영": common.TypedRelSpec("date", "launch_date")}
        program = ".decl relation(subject: symbol, rel: symbol, object: symbol)\n"
        common._assert_no_alias_collision(specs, program)  # does not raise

    def test_non_projectable_alias_ignored(self):
        # A `number` alias that happens to match an existing decl is not a
        # collision because number is never projected.
        specs = {"버전": common.TypedRelSpec("number", "version_num")}
        program = ".decl version_num(s: symbol, v: int64)\n"
        common._assert_no_alias_collision(specs, program)  # does not raise


_RUNNER = textwrap.dedent(
    """
    import os, sys, json
    sys.path.insert(0, os.path.join(os.environ["KB_TOOLS"]))
    import common
    a = common.run_wirelog()
    b = common.run_wirelog()
    assert a == b, "non-deterministic engine output"
    # after2030 rows are subject-only 1-tuples; emit the sorted subjects.
    print(json.dumps(sorted(row[0] for row in a.get("after2030", set()))))
    """
)


def _build_kb(root: Path) -> None:
    (root / "facts").mkdir(parents=True, exist_ok=True)
    (root / "policy").mkdir(parents=True, exist_ok=True)
    (root / "facts" / "accepted.dl").write_text(
        'relation("갑서비스","정식_운영","2030.1").\n'
        'relation("을서비스","정식_운영","2029.6").\n'
        'relation("병서비스","정식_운영","미정").\n',
        encoding="utf-8",
    )
    (root / "policy" / "typed-relations.md").write_text(
        "- `정식_운영` : date as launch_date\n", encoding="utf-8"
    )
    (root / "policy" / "logic-policy.dl").write_text(
        ".decl after2030(s: symbol)\n"
        "after2030(S) :- launch_date(S, D), D >= 20300101.\n",
        encoding="utf-8",
    )


@pytest.mark.skipif(
    common.EasySession is None, reason="pyrewire not installed"
)
def test_end_to_end_threshold_inference(tmp_path: Path):
    # run_wirelog() reads module-level path globals bound at import from
    # FACTLOG_ROOT, so we run it in a SUBPROCESS with FACTLOG_ROOT pointed at a
    # temp KB. sys.executable so the child has pyrewire.
    _build_kb(tmp_path)
    env = dict(os.environ)
    env["FACTLOG_ROOT"] = str(tmp_path)
    env["KB_TOOLS"] = str(Path(common.__file__).resolve().parent)
    proc = subprocess.run(
        [sys.executable, "-c", _RUNNER],
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    import json

    result = json.loads(proc.stdout.strip().splitlines()[-1])
    # 갑 (2030.1 -> 20300101) clears the threshold; 을 (2029.6) is below;
    # 병 ("미정") does not parse -> skipped (graceful degrade).
    assert result == ["갑서비스"]
    assert "does not parse" in proc.stderr  # 병서비스 warned
