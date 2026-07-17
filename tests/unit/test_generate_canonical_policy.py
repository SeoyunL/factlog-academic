# SPDX-License-Identifier: Apache-2.0
"""Unit tests for canonical-bodied policy rules declared in logic-policy.md (#243).

A bullet whose sentence body starts with a literal, lowercase, anchored
``{canonical}`` marker compiles to ``canonical(X, "rel", _)`` bodies instead of
``relation(X, "rel", _)``.  The marker is stripped inside the generator BEFORE
relation extraction and predicate inference, so the shared #190 parsers
(``common.markdown_policy_items`` / ``common.logic_policy_md_relations``) never
special-case it and a no-marker bullet stays byte-identical to the pre-feature
output.
"""
from __future__ import annotations

import pytest

import common
import generate_logic_policy as g


def _md(*bullets: str) -> str:
    body = "\n".join(bullets)
    return f"# Logic policy\n\n## Rules\n\n{body}\n"


_CANON_BULLET = "- [retracted_conclusion] {canonical} 문서가 `결론` 이면서 `철회상태` 이면 철회로 본다."
_PLAIN_BULLET = "- [retracted_conclusion] 문서가 `결론` 이면서 `철회상태` 이면 철회로 본다."


def test_canonical_prefix_emits_canonical_bodies() -> None:
    """(1) A {canonical} bullet with two relations → canonical:True and
    canonical(...) body lines, no relation( body line."""
    draft = g.fixture_policy_json(_md(_CANON_BULLET))
    rule = draft["rules"][0]
    assert rule["canonical"] is True

    program = g.compile_policy(g.normalized_rules(draft))
    assert 'canonical(X, "결론", _)' in program
    assert 'canonical(X, "철회상태", _)' in program
    # No relation-bodied condition line was emitted.
    for line in program.splitlines():
        assert not line.lstrip().startswith("relation(")


def test_no_marker_is_byte_identical_pre_feature() -> None:
    """(2) The same bullet WITHOUT the marker has no canonical key and uses
    relation( bodies. The fixture dict equals the exact pre-feature shape."""
    draft = g.fixture_policy_json(_md(_PLAIN_BULLET))
    rule = draft["rules"][0]
    assert "canonical" not in rule
    assert rule == {
        "predicate": "policy_match",
        "reason": "retracted_conclusion",
        "conditions": [{"relation": "결론"}, {"relation": "철회상태"}],
    }

    program = g.compile_policy(g.normalized_rules(draft))
    assert 'relation(X, "결론", _)' in program
    assert "canonical(" not in program


def test_prose_mid_sentence_marker_is_not_canonical() -> None:
    """(3) A body mentioning {canonical} NOT as an anchored prefix is NOT a
    canonical rule; _strip_canonical_prefix returns False and the body uses
    relation(."""
    prose = "이 규칙은 {canonical} 방식을 쓴다 `결론`."
    is_canonical, body = g._strip_canonical_prefix(prose, 1)
    assert is_canonical is False
    assert body == prose

    draft = g.fixture_policy_json(_md("- [note] " + prose))
    assert "canonical" not in draft["rules"][0]
    program = g.compile_policy(g.normalized_rules(draft))
    assert "canonical(" not in program
    assert 'relation(X, "결론", _)' in program


def test_canonical_prefix_with_zero_relations_is_rejected() -> None:
    """(4) A {canonical} prefix but no backtick relation is rejected exactly like
    a non-canonical no-relation bullet (SystemExit, same rejected path)."""
    with pytest.raises(SystemExit) as excinfo:
        g.fixture_policy_json(_md("- [empty] {canonical} 아무 관계도 없다."))
    assert "at least one backtick relation name" in str(excinfo.value)


def test_normalized_rules_rejects_unknown_key_and_non_bool_canonical() -> None:
    """(5) Schema strictness: unknown key still raises; non-bool canonical raises."""
    with pytest.raises(ValueError, match="unsupported key"):
        g.normalized_rules(
            {
                "rules": [
                    {
                        "predicate": "conflict",
                        "reason": "r",
                        "conditions": [{"relation": "a"}],
                        "bogus": 1,
                    }
                ]
            }
        )

    with pytest.raises(ValueError, match="canonical flag must be a boolean"):
        g.normalized_rules(
            {
                "rules": [
                    {
                        "predicate": "conflict",
                        "reason": "r",
                        "conditions": [{"relation": "a"}],
                        "canonical": "yes",
                    }
                ]
            }
        )


def test_same_tuple_canonical_and_non_canonical_collision_raises() -> None:
    """(6) Two rules with identical (predicate, reason, relations) differing only
    in canonical → normalized_rules raises."""
    with pytest.raises(ValueError, match="canonical and non-canonical"):
        g.normalized_rules(
            {
                "rules": [
                    {
                        "predicate": "conflict",
                        "reason": "r",
                        "conditions": [{"relation": "a"}],
                        "canonical": True,
                    },
                    {
                        "predicate": "conflict",
                        "reason": "r",
                        "conditions": [{"relation": "a"}],
                    },
                ]
            }
        )


def test_mixed_file_is_deterministic_and_sorted() -> None:
    """(7) A policy with one canonical + one relation bullet compiles identically
    across two runs, sorted by (predicate, reason, relations)."""
    md = _md(
        _CANON_BULLET,
        "- [alpha_note] 문서가 `concludes` 이면 본다.",
    )
    first = g.compile_policy(g.normalized_rules(g.fixture_policy_json(md)))
    second = g.compile_policy(g.normalized_rules(g.fixture_policy_json(md)))
    assert first == second
    # alpha_note sorts before retracted_conclusion (same predicate policy_match).
    assert first.index("// alpha_note") < first.index("// retracted_conclusion")
    # Both body kinds coexist correctly.
    assert 'relation(X, "concludes", _)' in first
    assert 'canonical(X, "결론", _)' in first


def test_shared_parser_never_special_cases_the_marker() -> None:
    """(8) #190 drift guard: the shared parsers extract identical relations from a
    sentence whether or not the {canonical} marker rides inside it, and
    markdown_policy_items sees the marker as part of the sentence text (it does
    NOT strip it — only the generator does)."""
    with_marker = "{canonical} 문서가 `결론` 이면서 `철회상태` 이면 철회로 본다."
    without_marker = "문서가 `결론` 이면서 `철회상태` 이면 철회로 본다."

    # Relation extraction is identical: the marker carries no backticks.
    assert common.logic_policy_md_relations(with_marker) == common.logic_policy_md_relations(
        without_marker
    ) == ["결론", "철회상태"]

    # markdown_policy_items returns the marker verbatim inside the sentence — the
    # shared parser does not know about {canonical}; only the generator strips it.
    items = common.markdown_policy_items(_md("- [rc] " + with_marker))
    assert len(items) == 1
    _lineno, reason, sentence = items[0]
    assert reason == "rc"
    assert sentence == with_marker
    assert sentence.startswith("{canonical} ")
