# SPDX-License-Identifier: Apache-2.0
"""The policy compiler rejects control chars it would emit as wirelog-undecodable
escapes into policy/logic-policy.dl (#359).

Same silent identity loss as #331/#357, reached through the third authoring surface:
a backtick relation name in policy/logic-policy.md. ``RELATION_RE`` (``^[^\\s"`(),.]+$``)
excludes only whitespace, so 23 C0 controls (\\x00-\\x08, \\x0e-\\x1b) pass it and
``dl_string`` writes them as ``relation(X, "cites\\u0001evil", _).`` — an escape the
engine does not decode, so the rule body can never match any fact and the policy is
silently dead.

There are TWO gates because there are two paths to emission, and every claim about
reachability below holds only on one of them:

- DETERMINISTIC path: ``fixture_policy_json`` parses the .md and gates both axes there,
  because that is the only place where the source lineno is still known, so the error can
  point at the exact bullet.
- LLM DRAFT path: a model returns JSON that goes ``parse_json_object`` ->
  ``normalized_rules``, never touching ``fixture_policy_json``. Its relation gate (#365)
  therefore sits in ``normalized_rules``, right after ``RELATION_RE``, which is the one
  choke point both paths share — ``main()`` builds every program as
  ``compile_policy(normalized_rules(draft))``.

The ``reason`` axis has no red test on either path, but for different reasons, and only
the first defence on each path decides reachability:

1. ``markdown_policy_items`` (common.py) requires the bullet tag to match
   ``^\\[([a-z0-9_]+)\\]\\s+(.+)$``. A control char in the tag means the bullet is not a
   policy item AT ALL — measured: all 32 C0 characters yield zero items. This is the
   boundary on the DETERMINISTIC path.
2. ``REASON_RE`` (``^[a-z0-9_]+$``) rejects the same 32 in ``normalized_rules``. It runs
   after ``fixture_policy_json``, so it decides nothing on the deterministic path — but on
   the DRAFT path it is the reason axis's only defence, and it runs before the #365 gate,
   which is why that gate covers relation names only.

Both are pinned below, and relaxing either one needs a red test. The fixture gate stays
regardless — the tag regex is a PARSING rule, so whoever widens it is deciding bullet
syntax (#190) and has no cue that engine integrity hangs on it. See
``_reject_undecodable_policy_name``.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

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


def test_no_c0_character_survives_the_bullet_tag_regex():
    """Pin the boundary that actually keeps the reason axis unreachable.

    markdown_policy_items runs BEFORE the gate, so this — not REASON_RE — is what stops a
    control-char reason from ever arriving. If it ever fails, the reason axis has become
    reachable and its gate needs a red test of its own.
    """
    survivors = [
        ch
        for ch in (chr(i) for i in range(0x20))
        if common.markdown_policy_items(_md(f"- [retr{ch}acted] 문서가 `cites` 이면 철회."))
    ]
    assert survivors == []


def test_no_c0_character_survives_the_reason_regex():
    """REASON_RE's role splits by path, so this is not merely a second-line alarm.

    On the DETERMINISTIC path it is redundant: markdown_policy_items already refused the
    bullet, and REASON_RE runs after fixture_policy_json anyway. On the LLM DRAFT path
    there is no bullet and no fixture_policy_json — a draft goes parse_json_object ->
    normalized_rules, so REASON_RE is the ONLY defence the reason axis has. That is also
    why normalized_rules gates relation names (#365) but not reason names: REASON_RE
    already stops every C0 there, earlier in the same function. Relaxing it would open the
    reason axis on the draft path with nothing behind it.
    """
    survivors = [
        ch for ch in (chr(i) for i in range(0x20)) if g.REASON_RE.match(f"a{ch}b".strip())
    ]
    assert survivors == []


# The C0 characters that clear RELATION_RE and so reach the #365 gate. Written out as an
# explicit set rather than derived from RELATION_RE: a parametrize list computed from the
# thing under test degrades silently — narrow RELATION_RE and the list becomes empty, and
# pytest reports success for a test that ran zero cases. test_gate_parameters_still_cover
# below compares this constant against the live regex, so a narrowing is a FAILURE here
# instead of a vanishing.
RELATION_RE_PASSING_C0 = [chr(i) for i in [*range(0x00, 0x09), *range(0x0E, 0x1C)]]


def test_gate_parameters_still_cover_every_relation_re_passing_c0():
    """Guard the parametrize source above against silent shrinkage."""
    live = [chr(i) for i in range(0x20) if g.RELATION_RE.match(f"a{chr(i)}b")]
    assert len(RELATION_RE_PASSING_C0) == 23, RELATION_RE_PASSING_C0
    assert live == RELATION_RE_PASSING_C0, (
        "RELATION_RE changed which C0 characters reach the gate; update the constant and "
        "check whether the gate's coverage claim still holds"
    )


def _draft(relation: str, reason: str = "bad_rel") -> dict:
    """A draft as the LLM path produces it: JSON text through parse_json_object, never
    through fixture_policy_json. This is the input shape normalized_rules must defend."""
    payload = json.dumps(
        {"rules": [{"predicate": "policy_match", "reason": reason,
                    "conditions": [{"relation": relation}]}]}
    )
    return g.parse_json_object(payload)


class TestDraftPathRelationControlChars:
    """The emission-boundary gate in normalized_rules (#365).

    fixture_policy_json's gate covers the deterministic path only. A draft reaches
    compile_policy without ever touching it, so a control char in a relation name used to
    land in the .dl as ``relation(X, "cites\\u0000evil", _).`` — measured on main.
    """

    def test_a_control_char_relation_in_a_draft_is_rejected(self):
        with pytest.raises(ValueError) as exc:
            g.normalized_rules(_draft("cites\x00evil"))
        message = str(exc.value)
        assert "rule 1" in message, message
        # repr'd so the invisible control char is legible in the error (#363).
        assert repr("cites\x00evil") in message, message
        assert "control character" in message, message

    @pytest.mark.parametrize("ch", RELATION_RE_PASSING_C0)
    def test_every_relation_re_passing_c0_is_rejected(self, ch):
        # The 23 C0 characters (\x00-\x08, \x0e-\x1b) that clear RELATION_RE — it excludes
        # whitespace and nothing else. Each one is wirelog-undecodable, so each must die
        # here rather than reach dl_string.
        assert common.wirelog_undecodable_chars(ch), ch
        with pytest.raises(ValueError, match="control character"):
            g.normalized_rules(_draft(f"cites{ch}evil"))

    def test_delete_still_passes(self):
        # U+007F is the one non-alphanumeric control-adjacent character that both clears
        # RELATION_RE and round-trips through wirelog. The new gate must not sweep it up.
        assert common.wirelog_undecodable_chars("\x7f") == []
        rules = g.normalized_rules(_draft("cites\x7fevil"))
        assert rules[0]["relations"] == ["cites\x7fevil"]

    @pytest.mark.parametrize(
        "ch", ["\t", "\n", "\v", "\f", "\r", "\x1c", "\x1d", "\x1e", "\x1f"]
    )
    def test_undecodable_whitespace_is_still_refused_by_relation_re_first(self, ch):
        # The other nine C0 characters: undecodable exactly like the 23, but Python's
        # \s covers them, so RELATION_RE refuses them one line earlier. The gate
        # WOULD catch them — that is what the first assert states — which is precisely
        # why the message must still be RELATION_RE's. A gate that widened to claim
        # these would re-diagnose inputs already rejected for another reason: same rc,
        # different explanation, a regression the rc alone would never reveal.
        assert common.wirelog_undecodable_chars(ch), ch
        with pytest.raises(ValueError) as exc:
            g.normalized_rules(_draft(f"cites{ch}evil"))
        assert "invalid relation name" in str(exc.value), str(exc.value)
        assert "control character" not in str(exc.value), str(exc.value)

    @pytest.mark.parametrize("ch", ["\u0085", "\u2028", "\u2029"])
    def test_round_tripping_separators_are_never_called_undecodable(self, ch):
        # #255 proper: these three round-trip through the engine, so nothing here may
        # treat them as an integrity problem. They are still refused, but only because
        # Python's \s puts them outside the NAME grammar — a syntax verdict, not
        # an engine one. The `== []` is the load-bearing half; the message asserts pin
        # which of the two verdicts the caller is being given.
        assert common.wirelog_undecodable_chars(ch) == [], ch
        with pytest.raises(ValueError) as exc:
            g.normalized_rules(_draft(f"cites{ch}evil"))
        assert "invalid relation name" in str(exc.value), str(exc.value)
        assert "control character" not in str(exc.value), str(exc.value)

def test_compile_policy_is_only_ever_fed_normalized_rules_output():
    """Why the gate belongs in normalized_rules rather than beside dl_string.

    Both call sites in main() — the --check branch and the write branch — build their
    program as compile_policy(normalized_rules(draft)). normalized_rules is therefore the
    single choke point shared by everything DERIVED from the rules: the .dl via
    compile_policy and the trace via write_trace, deterministic and draft alike. If a
    future call site feeds compile_policy directly, this breaks and the gate needs to move
    down to the emission site.

    The claim stops there, and deliberately. Two files under runs/ are written before this
    gate, and they are NOT equally reachable — measured, not assumed:

    - ``PROMPT_OUT`` (main() writes it before either gate) survives a FAILING run. A .md
      with a control char in a backtick relation name exits rc=1 from the #359 gate, and
      runs/natural-language-to-policy-prompt.md is still on disk containing that byte.
      What it holds is not a compiled relation name but the author's original .md text —
      a prompt exists to hand the model the source verbatim, so stripping it would defeat
      the file's purpose. It is not engine input; nothing reads it back into the .dl.
    - ``RESPONSE_OUT`` is before THIS gate but after the #359 one. On the deterministic
      path fixture_policy_json raises first, so the file is never created at all —
      measured: rc=1 and no natural-language-to-policy-response.json. It becomes reachable
      only once a real LLM draft is wired in at its call site, replacing fixture output.

    Both orderings are decisions, not oversights: these files are the audit record of what
    went in and what came back, and an audit record that only survives validation cannot
    show why validation failed. Whoever wires a real draft in should keep the order.
    """
    # These are TEXT pins: they match source spelling, so an innocent rename or reflow
    # breaks them without anything being wrong. The messages say what to check, because a
    # bare count mismatch reads like a defect and would send the next person hunting one.
    bypass_hint = (
        "This is a text pin on tools/generate_logic_policy.py, so a rename or reflow can "
        "break it harmlessly. Before adjusting the pin, confirm the #365 gate still cannot "
        "be bypassed: every compile_policy/write_trace call must still be fed the output "
        "of normalized_rules. If some path now reaches emission without it, fix that "
        "instead — the gate has a hole."
    )
    source = Path(g.__file__).read_text(encoding="utf-8")
    calls = re.findall(r"(?<!def )compile_policy\((.*?)\)", source)
    assert len(calls) == 2, f"expected 2 compile_policy call sites, found {calls}. {bypass_hint}"
    assert set(calls) == {"rules"}, f"compile_policy fed something else: {calls}. {bypass_hint}"
    assert source.count("rules = normalized_rules(draft)") == 2, (
        f"expected 2 gate call sites spelled 'rules = normalized_rules(draft)', found "
        f"{source.count('rules = normalized_rules(draft)')}. {bypass_hint}"
    )

    # Pin the ordering the docstring calls intentional, so flipping it is a test failure
    # rather than a silent change to what the audit record captures.
    lines = source.splitlines()
    response_at = next(i for i, ln in enumerate(lines) if "RESPONSE_OUT.write_text" in ln)
    gate_at = next(
        i for i, ln in enumerate(lines[response_at:], start=response_at)
        if "rules = normalized_rules(draft)" in ln
    )
    trace_at = next(
        i for i, ln in enumerate(lines)
        if "write_trace(rules" in ln and not ln.lstrip().startswith("def ")
    )
    assert response_at < gate_at, (response_at, gate_at)
    assert gate_at < trace_at, (gate_at, trace_at)
