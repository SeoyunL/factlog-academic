# SPDX-License-Identifier: Apache-2.0
"""Reject control chars dl_string encodes as wirelog-undecodable escapes (#331).

dl_string is json.dumps; wirelog decodes only \\" and \\\\, so a \\t/\\n/\\uXXXX escape
(the C0 range U+0000-U+001F) is stored by the engine as a literal backslash+letter -
Python holds 'Fig<TAB>2', the engine holds 'Fig\\t2', their intern ids never meet, and the
value silently drops out of every query (the #308 witness even decodes to a bare integer).
compile refuses such a fact up front. candidates.csv still loads (so amend/eject can fix
the row), and U+0085/U+2028/U+2029 round-trip and are never rejected (#255, verified).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from factlog import common

HEADER = "subject,relation,object,source,status,confidence,note"


class TestWirelogUndecodableChars:
    """Pure helper - no engine, runs everywhere."""

    @pytest.mark.parametrize("ch", ["\t", "\n", "\r", "\b", "\f", "\x00", "\x1f"])
    def test_c0_controls_are_flagged(self, ch):
        assert common.wirelog_undecodable_chars(f"a{ch}b") == [ch]

    @pytest.mark.parametrize("ch", ["\u0085", "\u2028", "\u2029", "\u007f", "\u009f"])
    def test_line_separators_and_high_controls_round_trip(self, ch):
        # json.dumps(ensure_ascii=False) leaves these raw and wirelog parses them fine.
        assert common.wirelog_undecodable_chars(f"a{ch}b") == []

    def test_ordinary_text_is_clean(self):
        assert common.wirelog_undecodable_chars('q"z back\\slash café a b 값') == []

    def test_reports_each_distinct_control_sorted(self):
        # Sorted by code point: TAB (U+0009) before LF (U+000A); duplicates collapsed.
        assert common.wirelog_undecodable_chars("a\tb\nc\t") == ["\t", "\n"]


def _kb(tmp_path, rows):
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True, check=True,
    )
    (kb / "sources" / "x.md").write_text("x\n")
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


@pytest.mark.parametrize("field_idx,label", [(0, "subject"), (1, "relation"), (2, "object")])
def test_compile_rejects_a_tab_in_any_field(tmp_path, field_idx, label):
    parts = ["Fig", "cites", "Smith2020"]
    parts[field_idx] = parts[field_idx][:1] + "\t" + parts[field_idx][1:]  # embed a real tab
    row = ",".join(parts) + ",sources/x.md,confirmed,0.9,"
    kb = _kb(tmp_path, [row])
    proc = _compile(kb)
    assert proc.returncode != 0, proc.stdout + proc.stderr
    assert "control character" in proc.stderr and label in proc.stderr, proc.stderr
    assert "#331" in proc.stderr
    # The undecodable value never reaches the engine's trusted input.
    assert not (kb / "facts" / "accepted.dl").exists()


def test_compile_accepts_line_separators_that_round_trip(tmp_path):
    # U+2028 / U+0085 are NOT rejected - json keeps them raw and wirelog parses them.
    obj = "line1\u2028line2\u0085end"
    kb = _kb(tmp_path, [f"Doc,note,{obj},sources/x.md,confirmed,0.9,"])
    proc = _compile(kb)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    accepted = (kb / "facts" / "accepted.dl").read_text(encoding="utf-8")
    assert "\u2028" in accepted and "\u0085" in accepted  # kept raw, on one physical line


def test_candidates_csv_still_loads_a_tab_bearing_row(tmp_path):
    # The reject is at COMPILE, not at load: load_facts must still return the row so the
    # human gate (amend/eject) can correct it.
    kb = _kb(tmp_path, ["Fig\t2,cites,Smith2020,sources/x.md,confirmed,0.9,"])
    out = subprocess.run(
        [sys.executable, "-c",
         "import os, sys; sys.path.insert(0, os.getcwd())\n"
         "import factlog.common as c\n"
         "rows = c.load_facts()\n"
         "print(len(rows), repr(rows[0]['subject']))"],
        capture_output=True, text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    assert "1 'Fig\\t2'" in out.stdout, out.stdout


class TestCanonicalNameControlChars:
    """#357: the gate must also reject a control char authored INTO a canonical relation
    name via policy/relation-aliases.md. #331 only checks a fact row's subject/relation/
    object; a canonical name is derived from the alias policy, so a tab authored there
    reaches accepted.dl as a wirelog-undecodable escape through the canonical/3 EDB atom —
    the same silent identity loss, via the policy-authoring path.
    """

    def _kb_with_alias(self, tmp_path, canonical, rows):
        kb = tmp_path / "kb"
        subprocess.run(
            [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
            capture_output=True, check=True,
        )
        (kb / "sources" / "x.md").write_text("x\n")
        # `cites` is the alias KEY; canonical is the value under test. The fact below uses
        # `cites`, so canonical_atoms emits a canonical/3 atom carrying `canonical`.
        (kb / "policy" / "relation-aliases.md").write_text(
            f"# Relation aliases\n- `cites` -> `{canonical}`\n", encoding="utf-8"
        )
        (kb / "facts" / "candidates.csv").write_text(
            "\n".join([HEADER, *rows]) + "\n", encoding="utf-8"
        )
        return kb

    def test_compile_rejects_a_tab_in_a_canonical_name(self, tmp_path):
        # The fact fields are all CLEAN — only the canonical name (from the alias policy)
        # carries a tab, so only the #357 policy-path check can catch it.
        kb = self._kb_with_alias(
            tmp_path, "canon\tname",
            ["Fig,cites,Smith2020,sources/x.md,confirmed,0.9,"],
        )
        proc = _compile(kb)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "control character" in proc.stderr, proc.stderr
        assert "canonical relation name" in proc.stderr, proc.stderr
        assert "#357" in proc.stderr and "relation-aliases.md" in proc.stderr, proc.stderr
        # The undecodable canonical atom never reaches the engine's trusted input.
        assert not (kb / "facts" / "accepted.dl").exists()

    def test_compile_rejects_a_declared_but_unused_canonical_name(self, tmp_path):
        # #363: the #357 gate lived inside the canonical/3 emission loop, so a tab-bearing
        # canonical name whose alias key no fact uses was never visited and compile passed
        # rc 0. Nothing leaked — no canonical atom is emitted without a participating fact —
        # but detection was deferred to whenever such a fact appears. The declaration alone
        # must fail loud, so the policy defect surfaces where it was authored.
        kb = self._kb_with_alias(
            tmp_path, "canon\tname",
            # `mentions` is not the alias key `cites`: no fact participates in the alias.
            ["Fig,mentions,Smith2020,sources/x.md,confirmed,0.9,"],
        )
        proc = _compile(kb)
        assert proc.returncode != 0, proc.stdout + proc.stderr
        assert "control character" in proc.stderr, proc.stderr
        assert "canonical relation name" in proc.stderr, proc.stderr
        assert "relation-aliases.md" in proc.stderr, proc.stderr
        assert not (kb / "facts" / "accepted.dl").exists()

    def test_compile_accepts_a_clean_canonical_name(self, tmp_path):
        # A normal alias policy compiles with no regression; the canonical/3 atom is written.
        kb = self._kb_with_alias(
            tmp_path, "cited_by_paper",
            ["Fig,cites,Smith2020,sources/x.md,confirmed,0.9,"],
        )
        proc = _compile(kb)
        assert proc.returncode == 0, proc.stdout + proc.stderr
        accepted = (kb / "facts" / "accepted.dl").read_text(encoding="utf-8")
        assert "canonical(" in accepted and "cited_by_paper" in accepted, accepted


class TestAttrRelEmissionControlChars:
    """#373: the attr_rel emission site gates its own input, on BOTH branches.

    _attr_rel_facts writes a declared attribute relation name into the engine program.
    The name is a relation symbol read back out of accepted.dl, so the standing argument
    was that compile_facts' gate had already cleared it. Measurement says otherwise on
    both branches: `accepted=` lets a caller substitute rows the gate never saw, and the
    default branch reads accepted.dl straight from disk — compile_facts' gate ran in some
    earlier process, which describes that process and not these bytes.

    These are pure-function tests, so they monkeypatch the module paths the loaders read
    (the idiom in test_attribute_path_nodes.py) instead of driving a CLI in a subprocess
    as the compile tests above do.
    """

    @pytest.fixture
    def kb(self, tmp_path, monkeypatch):
        (tmp_path / "policy").mkdir()
        (tmp_path / "facts").mkdir()
        monkeypatch.setattr(common, "POLICY_DIR", tmp_path / "policy")
        monkeypatch.setattr(common, "ACCEPTED_DL", tmp_path / "facts" / "accepted.dl")
        return tmp_path

    def _declare(self, kb, name):
        # Backtick-quoted: _relation_names_from falls back to stripped.split()[0] for a
        # bare token, which would cut the name at the tab and never declare it.
        (kb / "policy" / "attribute-relations.md").write_text(
            f"# Attribute relations\n- `{name}`\n", encoding="utf-8"
        )

    @staticmethod
    def _rows(name):
        return [{"subject": "A", "relation": name, "object": "2020", "status": "accepted"}]

    def test_rejects_a_tab_in_rows_passed_through_the_accepted_argument(self, kb):
        self._declare(kb, "pub\tyear")
        # Premise first: without a matching declaration the function returns "" early and
        # a green result would mean nothing.
        assert common.attribute_relation_forms() == {"pub\tyear"}
        with pytest.raises(common.FactlogError) as exc:
            common._attr_rel_facts(self._rows("pub\tyear"))
        message = str(exc.value)
        assert "control character" in message, message
        assert "attribute relation name" in message, message
        assert "'\\t'" in message, message  # shown with !r so the tab stays visible
        assert "#373" in message, message

    def test_rejects_a_tab_reaching_the_default_branch_from_disk(self, kb):
        # No argument: rows come from load_accepted_facts(), which reads accepted.dl as it
        # is on disk. A hand-edited, truncated or externally generated file gets here with
        # no gate between it and the engine program.
        self._declare(kb, "pub\tyear")
        (kb / "facts" / "accepted.dl").write_text(
            'relation("A", "pub\\tyear", "2020").\n', encoding="utf-8"
        )
        # Premise: the JSON escape on disk decodes back to a real tab in Python.
        assert common.load_accepted_facts() == [
            {"subject": "A", "relation": "pub\tyear", "object": "2020"}
        ]
        with pytest.raises(common.FactlogError) as exc:
            common._attr_rel_facts()
        assert "control character" in str(exc.value), str(exc.value)

    def test_a_clean_name_emits_byte_identical_output(self, kb):
        self._declare(kb, "pub_year")
        assert common._attr_rel_facts(self._rows("pub_year")) == '\nattr_rel("pub_year").\n'

    # What the gate must NOT reject is pinned on the verdict itself, by
    # TestWirelogUndecodableChars::test_line_separators_and_high_controls_round_trip — this
    # gate calls that predicate and cannot disagree with it. Only the emission consequence is
    # worth a test here, and U+007F is the character to use: U+0085/U+2028/U+2029 cannot reach
    # this site at all, because _relation_names_from reads the policy file with
    # str.splitlines(), which splits on all three, so such a name is never declared (measured
    # — the parse yields '`pub' and 'year`' instead of one name).
    def test_round_tripping_control_survives_emission(self, kb):
        name = f"pub{chr(0x7F)}year"
        self._declare(kb, name)
        assert common.attribute_relation_forms() == {name}
        assert common._attr_rel_facts(self._rows(name)) == f'\nattr_rel("{name}").\n'


# --- Engine-backed reproduction: WHY the gate exists -------------------------
try:
    import pyrewire  # noqa: F401

    _HAVE_ENGINE = True
except ImportError:  # pragma: no cover - depends on the install
    _HAVE_ENGINE = False


@pytest.mark.skipif(not _HAVE_ENGINE, reason="pyrewire not installed")
def test_a_tab_value_corrupts_the_witness_the_gate_now_prevents(tmp_path):
    # Reproduce, through the production run_wirelog() path, the corruption the gate stops:
    # hand-write accepted.dl exactly as dl_string would emit a tab-bearing subject (the
    # gate is bypassed here on purpose), then read the #308 witness relation_alive. The
    # engine parsed 'Fig\\t2' (literal backslash-t), so it decodes to a bare integer id
    # instead of the Python 'Fig<TAB>2'. A plain subject decodes back to itself.
    kb = tmp_path / "kb"
    subprocess.run(
        [sys.executable, "-m", "factlog", "init", "--target", str(kb)],
        capture_output=True, check=True,
    )
    (kb / "sources" / "x.md").write_text("x\n")
    script = (
        "import os, sys; sys.path.insert(0, os.getcwd())\n"
        "import factlog.common as c\n"
        "def witness(pyval):\n"
        "    c.ACCEPTED_DL.write_text('relation(' + c.dl_string(pyval) + ', \"cites\", \"X\").' + chr(10), encoding='utf-8')\n"
        "    alive = c.run_wirelog().get('relation_alive', set())\n"
        "    return {str(t[0]) for t in alive if t}\n"
        "tab = witness('Fig\\t2')\n"
        "plain = witness('Fig2')\n"
        "print('TAB_HOLDS_PYVAL', 'Fig\\t2' in tab)\n"
        "print('PLAIN_HOLDS_PYVAL', 'Fig2' in plain)\n"
        "print('TAB_WITNESS', sorted(tab))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True, text=True,
        env={**os.environ, "FACTLOG_ROOT": str(kb), "PYTHONPATH": os.getcwd()},
    )
    assert out.returncode == 0, out.stdout + out.stderr
    # The plain subject round-trips to the witness; the tab subject does NOT — it is lost,
    # decoded to a bare id. That silent identity loss is why compile rejects it (#331).
    assert "PLAIN_HOLDS_PYVAL True" in out.stdout, out.stdout + out.stderr
    assert "TAB_HOLDS_PYVAL False" in out.stdout, out.stdout + out.stderr
