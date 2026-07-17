# SPDX-License-Identifier: Apache-2.0
"""An unrecognized `{...}` marker at a sentence start is rejected, not silently
downgraded to a relation body (#335).

The canonical marker anchors, so `see {canonical} for ...` (prose) correctly stays a
non-rule. But a case variant `{Canonical}`, a space-less `{canonical}`a`, or a non-ASCII
(NBSP) separator all fell through to `is_canonical=False` and compiled to a relation(...)
body — the author wrote a canonical rule and got a different one, with no warning. Since
RESERVED_PREDICATES blocks a canonical head anyway, an unrecognized marker has nowhere
safe to go: failing the load is the only correct fallback.
"""
from __future__ import annotations

import pytest

import common
import generate_logic_policy as g


def _md(*bullets: str) -> str:
    body = "\n".join(bullets)
    return f"# Logic policy\n\n## Rules\n\n{body}\n"


class TestExactMarkerIsAccepted:
    def test_lowercase_ascii_space_marker_is_canonical(self):
        is_canonical, body = g._strip_canonical_prefix("{canonical} `결론` 이면 철회.", 1)
        assert is_canonical is True
        assert body == "`결론` 이면 철회."

    def test_tab_separator_is_accepted(self):
        is_canonical, body = g._strip_canonical_prefix("{canonical}\t`결론`.", 1)
        assert is_canonical is True
        assert body == "`결론`."


class TestUnrecognizedMarkerIsRejected:
    @pytest.mark.parametrize(
        "sentence",
        [
            "{Canonical} `결론` 이면 철회.",       # capitalized
            "{CANONICAL} `결론` 이면 철회.",       # all caps
            "{canonical}`결론` 이면 철회.",        # no separator
            "{canonical}\xa0`결론` 이면 철회.",     # NBSP separator (Python \s used to match it)
            "{canonicals} `결론`.",               # near-miss name
            "{canon} `결론`.",                    # abbreviated
        ],
    )
    def test_leading_marker_variant_fails_loudly(self, sentence):
        with pytest.raises(common.FactlogError, match="unrecognized leading marker"):
            g._strip_canonical_prefix(sentence, 7)

    def test_error_names_the_line_and_marker(self):
        with pytest.raises(common.FactlogError, match=r"line 7.*\{Canonical\}"):
            g._strip_canonical_prefix("{Canonical} `결론`.", 7)


class TestProseMarkerStillNotARule:
    def test_mid_sentence_marker_is_not_canonical(self):
        prose = "이 규칙은 {canonical} 방식을 쓴다 `결론`."
        is_canonical, body = g._strip_canonical_prefix(prose, 1)
        assert is_canonical is False
        assert body == prose

    def test_no_marker_sentence_is_untouched(self):
        plain = "문서가 `결론` 이면 철회로 본다."
        assert g._strip_canonical_prefix(plain, 1) == (False, plain)


class TestRejectionSurfacesThroughCompile:
    def test_capitalized_marker_bullet_raises_through_fixture_json(self):
        md = _md("- [retracted] {Canonical} 문서가 `결론` 이면 철회.")
        with pytest.raises(common.FactlogError, match="unrecognized leading marker"):
            g.fixture_policy_json(md)

    def test_nbsp_marker_bullet_raises_through_fixture_json(self):
        md = _md("- [retracted] {canonical}\xa0문서가 `결론` 이면 철회.")
        with pytest.raises(common.FactlogError, match="unrecognized leading marker"):
            g.fixture_policy_json(md)

    def test_exact_marker_bullet_still_compiles_to_canonical(self):
        md = _md("- [retracted] {canonical} 문서가 `결론` 이면 철회.")
        draft = g.fixture_policy_json(md)
        assert draft["rules"][0]["canonical"] is True
