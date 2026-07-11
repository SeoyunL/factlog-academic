"""A typed literal that does not parse must be visible in the report (#227).

The projection dropped such a fact from its comparison predicate and said so on
stderr only, so `facts/logic_report.txt` — the artifact the deterministic gate
makes you show verbatim before concluding — reported `warnings: 0` while the
fact was silently missing from every typed query.
"""

from factlog.common import TypedRelSpec, typed_projection_warnings
from factlog.literal_types import parse_number_scaled, parse_ordinal


def row(subject, relation, obj):
    return {"subject": subject, "relation": relation, "object": obj, "status": "accepted"}


SPECS = {
    "rank": TypedRelSpec(type="ordinal", alias="rankval"),
    "score": TypedRelSpec(type="number", alias="scoreval"),
}


def test_unparseable_object_is_reported():
    warns = typed_projection_warnings([row("A", "rank", "rank 3")], SPECS)
    assert len(warns) == 1
    # the fact, the type, and the consequence must all be legible to a human
    assert "rank 3" in warns[0]
    assert "ordinal" in warns[0]
    assert "EXCLUDED" in warns[0]
    assert "rankval" in warns[0]


def test_parseable_object_is_silent():
    assert typed_projection_warnings([row("A", "rank", "3rd")], SPECS) == []


def test_negative_number_is_parseable_and_silent():
    # the docs claimed `number` was positive-only; it is not
    assert parse_number_scaled("-3.5") == -3500
    assert typed_projection_warnings([row("A", "score", "-3.5")], SPECS) == []


def test_untyped_relation_is_ignored():
    assert typed_projection_warnings([row("A", "mentions", "whatever")], SPECS) == []


def test_readme_ordinal_examples_all_parse():
    # the exact values the README and the `init` scaffold now hand the user
    for value in ("3rd", "3위", "제3호"):
        assert parse_ordinal(value) == 3, value
    # and the one they used to hand the user, which does not
    assert parse_ordinal("rank 3") is None


def test_warnings_are_deterministically_ordered():
    rows = [row("B", "rank", "rank 9"), row("A", "rank", "rank 3"), row("A", "score", "n/a")]
    got = typed_projection_warnings(rows, SPECS)
    assert len(got) == 3
    assert got == typed_projection_warnings(list(reversed(rows)), SPECS)


def test_no_specs_means_no_warnings():
    assert typed_projection_warnings([row("A", "rank", "rank 3")], {}) == []


def test_int64_overflow_is_reported_not_just_unparseable():
    """The projection drops three ways; the report must know about all three.

    A `number` is scaled x1000, so a value past ~9.2e15 overflows int64 and the
    engine skips it -- and the report used to say `warnings: 0` because it only
    checked "does not parse". This KB's own examples reach 억/조 magnitudes.
    """
    warns = typed_projection_warnings([row("A", "score", "10000000000000000")], SPECS)
    assert len(warns) == 1
    assert "int64" in warns[0]
    assert "EXCLUDED" in warns[0]


def test_the_report_and_the_projection_cannot_disagree():
    """Both sides call typed_projection_outcome, so a new guard reaches both."""
    from factlog.common import typed_projection_outcome

    for value, drops in (
        ("3rd", False),
        ("rank 3", True),
        ("99999999999999999999th", True),  # int64 overflow, not a parse failure
    ):
        spec = SPECS["rank"]
        scalar, reason = typed_projection_outcome(row("A", "rank", value), spec)
        assert (scalar is None) is drops, value
        assert (reason is not None) is drops, value
        warned = bool(typed_projection_warnings([row("A", "rank", value)], SPECS))
        assert warned is drops, f"report and projection disagree on {value!r}"
