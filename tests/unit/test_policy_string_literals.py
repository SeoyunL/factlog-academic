"""String literals in a .dl policy are extracted escape-decoded (#250).

run_wirelog pre-interns every quoted literal in the policy program so
decode_wirelog_value can turn an engine-emitted symbol id back into its text. The old
`re.findall(r'"([^"]+)"', ...)` cut a literal at the first `\\"`, so it interned the
wrong pieces and left the real symbol out -- a policy finding whose reason held an
escaped quote then printed as a bare integer.
"""

from factlog.common import policy_string_literals


def test_a_plain_program():
    assert policy_string_literals('r("a", "b", "c").') == ["a", "b", "c"]


def test_an_escaped_quote_is_not_a_boundary():
    got = policy_string_literals('after(X, "size 5\\" bolt") :- relation(X, "s", _).')
    assert got == ['size 5" bolt', "s"]


def test_an_escaped_backslash_decodes():
    got = policy_string_literals(r'flagged(X, "C:\\path") :- r(X, "s", "a").')
    assert got == ["C:\\path", "s", "a"]


def test_the_old_findall_would_have_split_this():
    import re

    prog = 'after(X, "size 5\\" bolt") :- relation(X, "s", _).'
    old = re.findall(r'"([^"]+)"', prog)
    assert "size 5\" bolt" not in old  # the bug: the real symbol is absent
    assert "size 5\" bolt" in policy_string_literals(prog)  # the fix: it is present


def test_an_unterminated_string_raises():
    # The shared lexer is strict for policy text: an unterminated literal means the
    # engine would reject the whole program, and load_logic_policy runs the reserved-head
    # guard (same lexer) BEFORE interning, so this raise is reached loudly, not guessed.
    import pytest

    from factlog.common import FactlogError

    with pytest.raises(FactlogError, match="unterminated string"):
        policy_string_literals('r("open) :- x.')


def test_no_quotes():
    assert policy_string_literals("edge(S, O) :- relation(S, R, O).") == []


def test_non_json_escapes_are_kept_literal_matching_the_engine():
    # The engine un-escapes only the quote and backslash; it stores backslash-n as two
    # chars, not a newline. json.loads would over-decode these and re-create the mismatch.
    assert policy_string_literals(r'r("a\nb").') == ["a\\nb"]
    assert policy_string_literals(r'r("a\tb").') == ["a\\tb"]
    assert policy_string_literals(r'r("a\/b").') == ["a\\/b"]
    # but the two escapes the engine DOES honour are decoded
    assert policy_string_literals('r("a\\" b").') == ['a" b']
    assert policy_string_literals(r'r("C:\\p").') == ["C:\\p"]


def test_an_odd_quote_in_a_comment_does_not_shift_the_boundaries():
    # `// the 5" bolt rule` has an odd quote; the shared lexer skips comments, so the
    # real literals after it are still found. The old regex paired the comment quote with
    # a later one and lost them (#250 review).
    prog = '// the 5" bolt rule\nflagged(X, "real") :- relation(X, "s", "a").'
    assert policy_string_literals(prog) == ["real", "s", "a"]


def test_quoted_constants_shares_the_lexer():
    from factlog.common import _quoted_constants

    # an escaped quote inside a review_required question is decoded, not truncated
    assert _quoted_constants('review_required("who said \\" hi")?') == ['who said " hi']
    # a comment marker inside the literal is not a comment
    assert _quoted_constants('review_required("a # b")?') == ["a # b"]


def test_an_empty_literal_is_one_literal():
    # `review_required("")?` yields one empty literal. The old regex `[^"]+` matched zero
    # (min one char); the lexer returns ['']. Both the report and the gate see the same
    # count, so parity holds -- pinned so the behaviour is explicit, not accidental.
    assert policy_string_literals('review_required("")?') == [""]
    assert policy_string_literals('r("", "b").') == ["", "b"]
