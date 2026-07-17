# SPDX-License-Identifier: Apache-2.0
"""Unit + end-to-end tests for the typed side-relation projection (#119)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import common
import pytest


def _run_child(command: list[str], *, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


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

    def test_number_emits_int64_line(self):
        # number now projects as a scaled int64 (×1000) side-relation (#125).
        specs = {"버전": common.TypedRelSpec("number", "version_num")}
        assert common._typed_decls(specs) == "\n.decl version_num(subject: symbol, v: int64)\n"

    def test_sorted_by_alias(self):
        specs = {
            "b": common.TypedRelSpec("date", "zzz"),
            "a": common.TypedRelSpec("ordinal", "aaa"),
            "n": common.TypedRelSpec("number", "mmm"),
        }
        out = common._typed_decls(specs)
        # number now projects too, sorted alongside date/ordinal by alias.
        assert out == (
            "\n.decl aaa(subject: symbol, v: int64)"
            "\n.decl mmm(subject: symbol, v: int64)"
            "\n.decl zzz(subject: symbol, v: int64)\n"
        )


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

    def test_number_alias_is_collision_checked(self):
        # number now projects (#125), so a number alias that duplicates an
        # existing .decl IS a collision (it would emit a duplicate decl).
        specs = {"버전": common.TypedRelSpec("number", "version_num")}
        program = ".decl version_num(s: symbol, v: int64)\n"
        with pytest.raises(common.FactlogError):
            common._assert_no_alias_collision(specs, program)


class TestUnscaledNumberThreshold:
    """#125 fail-loud lint: an unscaled float threshold against a number alias in
    logic-policy.extra.dl raises a clean FactlogError, not a bare engine
    ParseError that silently kills the whole program."""

    SPECS = {"버전": common.TypedRelSpec("number", "version_num")}

    def test_unscaled_float_raises(self):
        extra = (
            ".decl ge_v2(entity: symbol, reason: symbol)\n"
            'ge_v2(S, "ge_2_0") :- version_num(S, V), V >= 2.0.\n'
        )
        with pytest.raises(common.FactlogError) as exc:
            common._assert_no_unscaled_number_threshold(self.SPECS, extra)
        msg = str(exc.value)
        assert "version_num" in msg
        assert "2.0" in msg
        assert "scaled" in msg  # actionable: names the ×1000 contract

    def test_scaled_int_threshold_ok(self):
        extra = (
            ".decl ge_v2(entity: symbol, reason: symbol)\n"
            'ge_v2(S, "ge_2_0") :- version_num(S, V), V >= 2000.\n'
        )
        common._assert_no_unscaled_number_threshold(self.SPECS, extra)  # no raise

    def test_float_inside_quoted_reason_is_not_a_threshold(self):
        # The threshold is correctly scaled (2000); the only float-looking token
        # (2.0) lives INSIDE the quoted reason string, which the engine accepts.
        # Stripping quoted spans before the scan avoids this false positive.
        extra = (
            ".decl ge(entity: symbol, reason: symbol)\n"
            'ge(S, "v2.0_plus") :- version_num(S, V), V >= 2000.\n'
        )
        common._assert_no_unscaled_number_threshold(self.SPECS, extra)  # no raise

    def test_no_number_specs_never_fires(self):
        # A date KB with a (would-be) float literal is NOT a number threshold:
        # the guard only fires when a number alias is declared.
        specs = {"정식_운영": common.TypedRelSpec("date", "launch_date")}
        extra = 'after(S, "x") :- launch_date(S, D), D >= 2.0.\n'
        common._assert_no_unscaled_number_threshold(specs, extra)  # no raise

    def test_float_on_unrelated_alias_line_ignored(self):
        # A float literal on a line that does NOT reference the number alias is
        # not flagged (narrow scan avoids false positives).
        extra = (
            'other(S, "x") :- some_date(S, D), D >= 2.0.\n'
            'ge_v2(S, "ge_2_0") :- version_num(S, V), V >= 2000.\n'
        )
        common._assert_no_unscaled_number_threshold(self.SPECS, extra)  # no raise

    def test_comment_line_ignored(self):
        # A float in a commented-out line is not a live threshold.
        extra = "// version_num(S, V), V >= 2.0  (old, do not use)\n"
        common._assert_no_unscaled_number_threshold(self.SPECS, extra)  # no raise


_RUNNER = textwrap.dedent(
    """
    import os, sys, json
    sys.path.insert(0, os.path.join(os.environ["KB_TOOLS"]))
    import common
    a = common.run_wirelog()
    b = common.run_wirelog()
    assert a == b, "non-deterministic engine output"
    # after2030 rows are (subject, reason) 2-tuples; emit the sorted subjects.
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
        # Arity-2 head (subject, reason). #322 promoted the arity-2 convention to a
        # LOAD-time guard, and run_wirelog() loads the policy through it, so the old
        # arity-1 shape this fixture used to carry is now rejected outright — exactly
        # the "Do NOT copy this shape" warning, now enforced instead of commented. The
        # reason column is a constant label; this test only reads the subject.
        '.decl after2030(s: symbol, reason: symbol)\n'
        'after2030(S, "launched 2030+") :- launch_date(S, D), D >= 20300101.\n',
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
    proc = _run_child([sys.executable, "-c", _RUNNER], env=env)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    import json

    result = json.loads(proc.stdout.strip().splitlines()[-1])
    # 갑 (2030.1 -> 20300101) clears the threshold; 을 (2029.6) is below;
    # 병 ("미정") does not parse -> skipped (graceful degrade).
    assert result == ["갑서비스"]
    assert "does not parse" in proc.stderr  # 병서비스 warned


# --- #120: hand-authored comparison predicates surface in the report -----------

_CANDIDATES_HEADER = "subject,relation,object,source,status,confidence,note\n"

# Minimal valid generated policy (one decl, like the sample-kb requires_review).
_MINIMAL_POLICY_DL = (
    "// generated from policy/logic-policy.md\n"
    ".decl requires_review(entity: symbol, reason: symbol)\n"
)


def _build_comparison_kb(root: Path, *, threshold: int = 20300101) -> None:
    """A `date` KB whose comparison rule lives in logic-policy.extra.dl (#120).

    을서비스 launches 2030.1 (-> 20300101), 갑서비스 2025.12 (-> 20251201).
    """
    for name in ("sources", "pages", "facts", "decisions", "policy"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "facts" / "candidates.csv").write_text(
        _CANDIDATES_HEADER
        + '을서비스,정식_운영,2030.1,sources/a.md,accepted,0.9,\n'
        + '갑서비스,정식_운영,2025.12,sources/a.md,accepted,0.9,\n',
        encoding="utf-8",
    )
    (root / "facts" / "accepted.dl").write_text(
        'relation("을서비스", "정식_운영", "2030.1").\n'
        'relation("갑서비스", "정식_운영", "2025.12").\n',
        encoding="utf-8",
    )
    (root / "policy" / "typed-relations.md").write_text(
        "- `정식_운영` : date as launch_date\n", encoding="utf-8"
    )
    (root / "policy" / "logic-policy.dl").write_text(_MINIMAL_POLICY_DL, encoding="utf-8")
    # Arity-2 (entity, reason) head with a quoted reason string; the scalar D
    # stays in the body (the reconciled #120 shape — NOT an arity-1 head).
    (root / "policy" / "logic-policy.extra.dl").write_text(
        ".decl after2030(entity: symbol, reason: symbol)\n"
        f'after2030(S, "launch_after_2030") :- launch_date(S, D), D >= {threshold}.\n',
        encoding="utf-8",
    )


def _run_logic_check(root: Path) -> str:
    """Run run_logic_check.py against a temp KB and return logic_report.txt."""
    env = dict(os.environ)
    env["FACTLOG_ROOT"] = str(root)
    tools_dir = Path(common.__file__).resolve().parent
    proc = _run_child([sys.executable, str(tools_dir / "run_logic_check.py")], env=env)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return (root / "facts" / "logic_report.txt").read_text(encoding="utf-8")


@pytest.mark.skipif(common.EasySession is None, reason="pyrewire not installed")
def test_comparison_predicate_surfaces_in_report(tmp_path: Path):
    _build_comparison_kb(tmp_path, threshold=20300101)
    report = _run_logic_check(tmp_path)
    # 을 (20300101) clears the threshold; 갑 (20251201) is below -> excluded.
    assert "after2030: 을서비스 (launch_after_2030)" in report
    assert "갑서비스" not in report.split("Policy Findings:", 1)[-1]


@pytest.mark.skipif(common.EasySession is None, reason="pyrewire not installed")
def test_threshold_is_not_hardcoded(tmp_path: Path):
    # Raise the threshold past both subjects -> no findings.
    high = tmp_path / "high"
    _build_comparison_kb(high, threshold=20310101)
    high_report = _run_logic_check(high)
    # No subject clears the bar, so the predicate infers no rows.
    assert "- after2030: 0 rows" in high_report  # evaluation summary
    assert "Policy Findings:" not in high_report  # nothing surfaced
    assert "launch_after_2030" not in high_report

    # Lower the threshold below both subjects -> both inferred.
    low = tmp_path / "low"
    _build_comparison_kb(low, threshold=20250101)
    report = _run_logic_check(low)
    assert "after2030: 을서비스 (launch_after_2030)" in report
    assert "after2030: 갑서비스 (launch_after_2030)" in report


# --- #121: a superseded typed row is excluded from the engine input ------------


def _compile_facts(root: Path) -> subprocess.CompletedProcess[str]:
    """Run the REAL compile_facts.py against a temp KB (no pyrewire needed)."""
    env = dict(os.environ)
    env["FACTLOG_ROOT"] = str(root)
    tools_dir = Path(common.__file__).resolve().parent
    return _run_child([sys.executable, str(tools_dir / "compile_facts.py")], env=env)


def _build_superseded_kb(root: Path) -> None:
    """A `date` KB seeded via candidates.csv (NOT a hand-written accepted.dl):

    을서비스 (accepted, 2030.1 -> 20300101) clears the threshold;
    구서비스 (superseded, 2032.1 -> 20320101) WOULD clear but is retired, so it
    must never reach accepted.dl and must never appear in a comparison finding.
    """
    for name in ("sources", "pages", "facts", "decisions", "policy"):
        (root / name).mkdir(parents=True, exist_ok=True)
    (root / "facts" / "candidates.csv").write_text(
        _CANDIDATES_HEADER
        + "을서비스,정식_운영,2030.1,sources/a.md,accepted,0.9,\n"
        + "구서비스,정식_운영,2032.1,sources/a.md,superseded,0.9,retired\n",
        encoding="utf-8",
    )
    (root / "policy" / "typed-relations.md").write_text(
        "- `정식_운영` : date as launch_date\n", encoding="utf-8"
    )
    (root / "policy" / "logic-policy.dl").write_text(_MINIMAL_POLICY_DL, encoding="utf-8")
    (root / "policy" / "logic-policy.extra.dl").write_text(
        ".decl after2030(entity: symbol, reason: symbol)\n"
        'after2030(S, "launch_after_2030") :- launch_date(S, D), D >= 20300101.\n',
        encoding="utf-8",
    )


@pytest.mark.skipif(common.EasySession is None, reason="pyrewire not installed")
def test_superseded_typed_row_excluded_from_comparison(tmp_path: Path):
    # Route through the REAL compile_facts.py: it filters candidates.csv by status
    # so a `superseded` row never reaches accepted.dl (the engine input). A
    # hand-written accepted.dl would make this assertion vacuous.
    _build_superseded_kb(tmp_path)
    proc = _compile_facts(tmp_path)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    accepted = (tmp_path / "facts" / "accepted.dl").read_text(encoding="utf-8")
    # 구서비스 is superseded -> excluded; 을서비스 (accepted) -> present (positive
    # control). Both prove the filter happens at the compile layer, not the rule.
    assert "구서비스" not in accepted
    assert "을서비스" in accepted

    # The retired row's value (20320101) WOULD clear the threshold, so if it
    # leaked it would surface in the finding. The engine sees only 을서비스.
    report = _run_logic_check(tmp_path)
    assert "after2030: 을서비스 (launch_after_2030)" in report
    assert "구서비스" not in report.split("Policy Findings:", 1)[-1]


# --- #122: amount projects into an int64 base-unit side-relation ---------------

_AMOUNT_RUNNER = textwrap.dedent(
    """
    import os, sys, json
    sys.path.insert(0, os.path.join(os.environ["KB_TOOLS"]))
    import common
    a = common.run_wirelog()
    b = common.run_wirelog()
    assert a == b, "non-deterministic engine output"
    print(json.dumps(sorted(row[0] for row in a.get("over10b", set()))))
    """
)


def _run_amount_kb(tmp_path: Path, units_clause: str) -> tuple[list[str], str]:
    """Build an amount KB (옵셔널 inline unit clause) and return (subjects, stderr).

    갑서비스 예산 100억 (=1e10) clears `>= 1e10`; 을서비스 50억 (=5e9) does not.
    """
    (tmp_path / "facts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "policy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "facts" / "accepted.dl").write_text(
        'relation("갑서비스","예산","100억").\n'
        'relation("을서비스","예산","50억").\n',
        encoding="utf-8",
    )
    (tmp_path / "policy" / "typed-relations.md").write_text(
        f"- `예산` : amount as budget_krw{units_clause}\n", encoding="utf-8"
    )
    (tmp_path / "policy" / "logic-policy.dl").write_text(
        # Arity-2 head (run_wirelog-direct test; see the date e2e note above).
        ".decl over10b(s: symbol, reason: symbol)\n"
        'over10b(S, "ge_10b") :- budget_krw(S, V), V >= 10000000000.\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["FACTLOG_ROOT"] = str(tmp_path)
    env["KB_TOOLS"] = str(Path(common.__file__).resolve().parent)
    proc = _run_child([sys.executable, "-c", _AMOUNT_RUNNER], env=env)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    return json.loads(proc.stdout.strip().splitlines()[-1]), proc.stderr


@pytest.mark.skipif(common.EasySession is None, reason="pyrewire not installed")
def test_amount_threshold_with_inline_units(tmp_path: Path):
    # 100억 = 1e10 clears `>= 1e10`; 50억 = 5e9 does not. Exact int base unit.
    subjects, _ = _run_amount_kb(tmp_path, " (억=1e8, 만=1e4, 원=1)")
    assert subjects == ["갑서비스"]


@pytest.mark.skipif(common.EasySession is None, reason="pyrewire not installed")
def test_amount_threshold_with_default_table(tmp_path: Path):
    # No inline clause -> projection resolves to DEFAULT_AMOUNT_UNITS (억=1e8).
    subjects, _ = _run_amount_kb(tmp_path, "")
    assert subjects == ["갑서비스"]


@pytest.mark.skipif(common.EasySession is None, reason="pyrewire not installed")
def test_amount_out_of_int64_range_is_skipped(tmp_path: Path):
    # An amount that resolves above 2**63 must be skipped + warned rather than
    # silently truncated/misinserted into the int64 column. 큰예산 = 1e11 * 1e8 =
    # 1e19 (> 9.22e18 = 2**63); 갑서비스 100억 = 1e10 still clears the threshold.
    # (#205) parse_amount now guards int64 itself: an out-of-range amount returns
    # None (untyped), so the projection drops the row via the generic
    # "does not parse; loading untyped" contract shared with the other parsers.
    (tmp_path / "facts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "policy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "facts" / "accepted.dl").write_text(
        'relation("갑서비스","예산","100억").\n'
        'relation("큰예산서비스","예산","100000000000억").\n',
        encoding="utf-8",
    )
    (tmp_path / "policy" / "typed-relations.md").write_text(
        "- `예산` : amount as budget_krw (억=1e8, 만=1e4, 원=1)\n", encoding="utf-8"
    )
    (tmp_path / "policy" / "logic-policy.dl").write_text(
        ".decl over10b(s: symbol, reason: symbol)\n"
        'over10b(S, "ge_10b") :- budget_krw(S, V), V >= 10000000000.\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["FACTLOG_ROOT"] = str(tmp_path)
    env["KB_TOOLS"] = str(Path(common.__file__).resolve().parent)
    proc = _run_child([sys.executable, "-c", _AMOUNT_RUNNER], env=env)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    subjects = json.loads(proc.stdout.strip().splitlines()[-1])
    # The out-of-range row is dropped from projection; the in-range one survives.
    assert subjects == ["갑서비스"]
    # (#205) parse_amount rejects the out-of-range value, so the projection reports
    # it via the shared "does not parse; loading untyped" warning (not truncated).
    assert "does not parse as amount" in proc.stderr
    assert "큰예산서비스" in proc.stderr


# --- #125: number projects into a scaled int64 (×1000) side-relation ----------

_NUMBER_RUNNER = textwrap.dedent(
    """
    import os, sys, json
    sys.path.insert(0, os.path.join(os.environ["KB_TOOLS"]))
    import common
    a = common.run_wirelog()
    b = common.run_wirelog()
    assert a == b, "non-deterministic engine output"
    print(json.dumps(sorted(row[0] for row in a.get("ge_v2", set()))))
    """
)


@pytest.mark.skipif(common.EasySession is None, reason="pyrewire not installed")
def test_number_threshold_scaled(tmp_path: Path):
    # appA 버전 2.5 (-> 2500) clears `>= 2000` (i.e. version 2.0 in SCALED units);
    # appB 버전 1.999 (-> 1999) is one scaled unit below -> the determinism proof.
    (tmp_path / "facts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "policy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "facts" / "accepted.dl").write_text(
        'relation("appA","버전","2.5").\n'
        'relation("appB","버전","1.999").\n',
        encoding="utf-8",
    )
    (tmp_path / "policy" / "typed-relations.md").write_text(
        "- `버전` : number as version_num\n", encoding="utf-8"
    )
    (tmp_path / "policy" / "logic-policy.dl").write_text(
        # Arity-2 head (run_wirelog-direct test); the threshold is in SCALED
        # units: version 2.0 -> 2000 (×1000). See the date e2e note above.
        ".decl ge_v2(s: symbol, reason: symbol)\n"
        'ge_v2(S, "ge_2_0") :- version_num(S, V), V >= 2000.\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["FACTLOG_ROOT"] = str(tmp_path)
    env["KB_TOOLS"] = str(Path(common.__file__).resolve().parent)
    proc = _run_child([sys.executable, "-c", _NUMBER_RUNNER], env=env)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    subjects = json.loads(proc.stdout.strip().splitlines()[-1])
    # 2500 >= 2000; 1999 < 2000 — the 1.999 boundary proves exact scaled ordering.
    assert subjects == ["appA"]


@pytest.mark.skipif(common.EasySession is None, reason="pyrewire not installed")
def test_number_out_of_int64_range_is_skipped(tmp_path: Path):
    # A number whose scaled value (×1000) exceeds 2**63 must be skipped + warned
    # rather than misinserted. 1e16 * 1000 = 1e19 (> 9.22e18 = 2**63); appA's
    # 2.5 (-> 2500) still clears the threshold.
    (tmp_path / "facts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "policy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "facts" / "accepted.dl").write_text(
        'relation("appA","버전","2.5").\n'
        'relation("bigApp","버전","10000000000000000").\n',
        encoding="utf-8",
    )
    (tmp_path / "policy" / "typed-relations.md").write_text(
        "- `버전` : number as version_num\n", encoding="utf-8"
    )
    (tmp_path / "policy" / "logic-policy.dl").write_text(
        ".decl ge_v2(s: symbol, reason: symbol)\n"
        'ge_v2(S, "ge_2_0") :- version_num(S, V), V >= 2000.\n',
        encoding="utf-8",
    )
    env = dict(os.environ)
    env["FACTLOG_ROOT"] = str(tmp_path)
    env["KB_TOOLS"] = str(Path(common.__file__).resolve().parent)
    proc = _run_child([sys.executable, "-c", _NUMBER_RUNNER], env=env)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    subjects = json.loads(proc.stdout.strip().splitlines()[-1])
    # The out-of-range row is dropped from projection; the in-range one survives.
    assert subjects == ["appA"]
    assert "out of int64 range" in proc.stderr
    assert "bigApp" in proc.stderr


_NUMBER_RAISE_RUNNER = textwrap.dedent(
    """
    import os, sys
    sys.path.insert(0, os.path.join(os.environ["KB_TOOLS"]))
    import common
    try:
        common.run_wirelog()
    except common.FactlogError as e:
        print("FACTLOGERROR:" + str(e))
        sys.exit(0)
    sys.exit(1)
    """
)


@pytest.mark.skipif(common.EasySession is None, reason="pyrewire not installed")
def test_unscaled_number_threshold_extra_dl_fails_loud(tmp_path: Path):
    # An UNSCALED float threshold against a number alias in extra.dl must raise a
    # clean FactlogError naming the alias + contract — NOT a bare engine
    # ParseError that silently kills relation/3 + all facts (#125 critic blocker).
    (tmp_path / "facts").mkdir(parents=True, exist_ok=True)
    (tmp_path / "policy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "facts" / "accepted.dl").write_text(
        'relation("appA","버전","2.5").\n', encoding="utf-8"
    )
    (tmp_path / "policy" / "typed-relations.md").write_text(
        "- `버전` : number as version_num\n", encoding="utf-8"
    )
    (tmp_path / "policy" / "logic-policy.dl").write_text(
        ".decl ge_v2(s: symbol, reason: symbol)\n", encoding="utf-8"
    )
    (tmp_path / "policy" / "logic-policy.extra.dl").write_text(
        'ge_v2(S, "ge_2_0") :- version_num(S, V), V >= 2.0.\n', encoding="utf-8"
    )
    env = dict(os.environ)
    env["FACTLOG_ROOT"] = str(tmp_path)
    env["KB_TOOLS"] = str(Path(common.__file__).resolve().parent)
    proc = _run_child([sys.executable, "-c", _NUMBER_RAISE_RUNNER], env=env)
    assert proc.returncode == 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    assert "FACTLOGERROR:" in proc.stdout
    assert "version_num" in proc.stdout
    assert "scaled" in proc.stdout
    # The clean guard fired before the engine ever saw the float literal.
    assert "ParseError" not in proc.stdout


@pytest.mark.skipif(common.EasySession is None, reason="pyrewire not installed")
def test_no_typed_file_no_extra_is_byte_identical(tmp_path: Path):
    # A KB with neither typed-relations.md nor logic-policy.extra.dl must produce
    # a report byte-identical to the same KB run again — the feature is inert when
    # unused (#116 invariant 1). We build it WITHOUT the typed/extra files.
    for name in ("sources", "pages", "facts", "decisions", "policy"):
        (tmp_path / name).mkdir(parents=True, exist_ok=True)
    (tmp_path / "facts" / "candidates.csv").write_text(
        _CANDIDATES_HEADER + '을서비스,develops,갑서비스,sources/a.md,accepted,0.9,\n',
        encoding="utf-8",
    )
    (tmp_path / "facts" / "accepted.dl").write_text(
        'relation("을서비스", "develops", "갑서비스").\n', encoding="utf-8"
    )
    (tmp_path / "policy" / "logic-policy.dl").write_text(_MINIMAL_POLICY_DL, encoding="utf-8")
    report = _run_logic_check(tmp_path)
    assert "after2030" not in report
    assert "launch_date" not in report
    # Re-run is byte-identical (determinism + no hidden typed state).
    assert _run_logic_check(tmp_path) == report
