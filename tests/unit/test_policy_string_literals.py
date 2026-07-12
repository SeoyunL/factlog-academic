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


def test_an_unterminated_string_is_skipped_not_crashed():
    # the engine would reject the program; extraction must not raise
    assert policy_string_literals('r("open) :- x.') == []


def test_no_quotes():
    assert policy_string_literals("edge(S, O) :- relation(S, R, O).") == []
