# SPDX-License-Identifier: Apache-2.0
"""The policy compiler rejects control chars it would emit as wirelog-undecodable
escapes into policy/logic-policy.dl (#359).

Same silent identity loss as #331/#357, reached through the third authoring surface:
a backtick relation name in policy/logic-policy.md. ``RELATION_RE`` (``^[^\\s"`(),.]+$``)
excludes only whitespace, so 22 C0 controls (\\x00-\\x08, \\x0e-\\x1b) pass it and
``dl_string`` writes them as ``relation(X, "cites\\u0001evil", _).`` — an escape the
engine does not decode, so the rule body can never match any fact and the policy is
silently dead. The gate lives in ``fixture_policy_json`` because that is the only place
where the source lineno is still known, so the error can point at the exact bullet.

The ``reason`` axis is gated too (defence in depth: the emission site is ``dl_string``,
and relying on a distant regex means the hole reopens quietly if that regex is relaxed),
but it has no red test — ``REASON_RE`` (``^[a-z0-9_]+$``) plus ``.strip()`` rejects all
32 C0 characters, so no input reaches the gate and any test would be vacuously green.
"""
from __future__ import annotations

import pytest

import common
import generate_logic_policy as g


def _md(*bullets: str) -> str:
    body = "\n".join(bullets)
    return f"# Logic policy\n\n## Rules\n\n{body}\n"


class TestRelationNameControlChars:
    def test_control_char_in_a_backtick_relation_is_rejected(self):
        md = _md("- [retracted] 문서가 `cites\x01evil` 이면 철회.")
        with pytest.raises(common.FactlogError) as exc:
            g.fixture_policy_json(md)
        message = str(exc.value)
        assert "control character" in message, message
        assert "policy/logic-policy.md line 5" in message, message
        # The offending name is shown repr'd, so the invisible control char is legible.
        assert repr("cites\x01evil") in message, message
        assert "backtick relation name" in message, message

    @pytest.mark.parametrize("ch", ["\t", "\x00", "\x08", "\x1b"])
    def test_every_c0_class_is_rejected(self, ch):
        # A tab would survive this stage and only die later in normalized_rules (RELATION_RE
        # excludes whitespace) with a rule-index message; the rest reach emission untouched.
        # The gate catches all of them here, where the bullet's line number is still known.
        md = _md(f"- [retracted] 문서가 `cites{ch}evil` 이면 철회.")
        with pytest.raises(common.FactlogError, match="control character"):
            g.fixture_policy_json(md)

    def test_a_clean_relation_still_compiles(self):
        md = _md("- [retracted] 문서가 `결론` 이면 철회.")
        draft = g.fixture_policy_json(md)
        assert draft["rules"][0]["conditions"] == [{"relation": "결론"}]

    @pytest.mark.parametrize("ch", ["\u0085", "\u2028", "\u2029"])
    def test_line_separators_are_not_rejected(self, ch):
        # These round-trip through the engine (#255) and must never be swept up by the gate.
        # str.splitlines() breaks the bullet at them, so the item stops parsing as a rule and
        # the run ends in the ordinary "no compilable policies" exit, not a control-char error.
        md = _md(f"- [retracted] 문서가 `cites{ch}evil` 이면 철회.")
        with pytest.raises(SystemExit) as exc:
            g.fixture_policy_json(md)
        assert "control character" not in str(exc.value), str(exc.value)


def test_no_c0_character_survives_the_reason_regex():
    """Pin the premise behind the untested reason gate: if this ever fails, the reason
    axis has become reachable and needs a red test of its own."""
    survivors = [
        ch for ch in (chr(i) for i in range(0x20)) if g.REASON_RE.match(f"a{ch}b".strip())
    ]
    assert survivors == []
