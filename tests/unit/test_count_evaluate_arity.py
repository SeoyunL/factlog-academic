# SPDX-License-Identifier: Apache-2.0
"""Regression tests: evaluate()'s count branch must guard arity (#257).

The relation and path branches of `evaluate` degrade cleanly on a malformed
query, but the count branch unpacked `args[0], args[1]` with no arity check:
  - a count query with < 2 args crashed with a raw IndexError (uncaught by
    cmd_evaluate, which only catches NotImplementedError -> a stack trace to the
    user on the documented `evaluate` subcommand);
  - a count query with > 2 args was silently accepted (extra arg ignored,
    returning a bogus count) even though classify_query rejects it as BAD_ARITY.
The fix aligns evaluate with the validator: arity != 2 raises NotImplementedError,
which cmd_evaluate already turns into a clean error JSON (rc 2).
"""
from __future__ import annotations

import pytest

import ask_router


def _facts():
    return [
        {"subject": "논문A", "relation": "게재연도", "object": "2005"},
        {"subject": "논문A", "relation": "저자", "object": "Kim"},
    ]


class TestCountEvaluateArity:
    def test_one_arg_count_raises_not_indexerror(self):
        with pytest.raises(NotImplementedError):
            ask_router.evaluate('count("논문A")?', _facts())

    def test_variable_only_count_raises(self):
        with pytest.raises(NotImplementedError):
            ask_router.evaluate('count(S)?', _facts())

    def test_three_arg_count_rejected_not_silently_accepted(self):
        with pytest.raises(NotImplementedError):
            ask_router.evaluate('count("논문A","저자","extra")?', _facts())

    def test_wellformed_two_arg_count_still_works(self):
        # regression anchor: a valid count is unchanged (2 distinct relations off
        # 논문A -> count of objects under one relation).
        result = ask_router.evaluate('count("논문A","게재연도")?', _facts())
        assert result == {"rows": [["1"]], "count": 1}
