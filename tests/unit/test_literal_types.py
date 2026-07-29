# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the deterministic literal normalizers (#117)."""
from __future__ import annotations

import literal_types as lt
import pytest


class TestParseDate:
    @pytest.mark.parametrize("raw,expected", [
        ("2030.1", 20300101),
        ("2030-01", 20300101),
        ("2030.01.15", 20300115),
        ("2024/07/01", 20240701),
        ("2030.12.31", 20301231),
        ("date(2030)", 20300101),
        ("date(2030, 1)", 20300101),
        ("date(2030, 1, 15)", 20300115),
    ])
    def test_accepts(self, raw, expected):
        assert lt.parse_date(raw) == expected

    @pytest.mark.parametrize("raw", ["2026", "not a date", "2030.13.01", "2030.1.32", ""])
    def test_rejects(self, raw):
        assert lt.parse_date(raw) is None


class TestParseDateYearPrecision:
    """`date(YYYY)` is a valid form in the extraction spec (text-to-fact.md), and a
    bibliographic record normally knows only the year — so it must parse."""

    @pytest.mark.parametrize("raw,expected", [
        ("date(2020)", 20200101),      # the spec form that used to degrade to untyped
        ("date(2020,1)", 20200101),    # month precision: same scalar
        ("date(2020,1,15)", 20200115),
        ("2020.1", 20200101),          # prose path unchanged
        ("date( 1998 )", 19980101),    # whitespace tolerated like the other arities
        ("DATE(2020)", 20200101),      # the compound regex is case-insensitive
    ])
    def test_year_precision_parses(self, raw, expected):
        assert lt.parse_date(raw) == expected

    @pytest.mark.parametrize("raw", [
        "2020",         # bare year: no wrapper, no separator — indistinguishable from a number
        " 2020 ",       # stripping whitespace does not make a bare year a date
        "date(20)",     # a 2-digit year is not a year
        "date(20200)",  # a 5-digit year is not a year
        "date()",       # no year at all
        "date(,1)",     # month without a year
        "date(2020,)",  # a dangling separator is malformed, not year precision
    ])
    def test_bare_or_malformed_year_still_rejected(self, raw):
        assert lt.parse_date(raw) is None

    def test_year_precision_sorts_before_later_months(self):
        # The default month/day must make a year-precision value the *earliest*
        # point in its year, so a `D >= 20200101` threshold includes it.
        assert lt.parse_date("date(2020)") < lt.parse_date("date(2020,2)")
        assert lt.parse_date("date(2019)") < lt.parse_date("date(2020)")

    def test_year_precision_via_normalize(self):
        # The typed projection goes through `normalize`, not `parse_date` directly.
        assert lt.normalize("date", "date(2020)") == 20200101

    @pytest.mark.parametrize("raw", ["date(0000)", "date(0)"])
    def test_year_precision_out_of_range(self, raw):
        # datetime.MINYEAR is 1: a year-precision term must degrade like any other.
        assert lt.parse_date(raw) is None

    @pytest.mark.parametrize("raw", [
        "2024-02-30",       # February never has 30 days
        "2024-04-31",       # April has 30 days
        "2024-06-31",       # June has 30 days
        "2024-11-31",       # November has 30 days
        "2023-02-29",       # 2023 is not a leap year
        "date(2024,2,30)",  # compound path, calendar-impossible
        "date(2023,2,29)",  # compound path, non-leap Feb 29
        "0000-01-01",       # year 0 is below datetime MINYEAR (1) — degrade, not a scalar
    ])
    def test_rejects_calendar_impossible(self, raw):
        # docstring contract: "Returns None if out of range" — a day <= 31 that is
        # nonetheless impossible for its month must degrade to untyped (None).
        assert lt.parse_date(raw) is None

    @pytest.mark.parametrize("raw,expected", [
        ("2024-02-29", 20240229),   # 2024 IS a leap year
        ("2024-01-31", 20240131),   # January really has 31 days
        ("2024-12-31", 20241231),   # December really has 31 days
        ("9999-12-31", 99991231),   # extreme-future valid date must pass
        ("2030.1", 20300101),       # month precision: day defaults to valid 01
        ("2030-01-15", 20300115),
        ("date(2024,2,29)", 20240229),  # compound path, leap-year Feb 29
    ])
    def test_accepts_calendar_valid(self, raw, expected):
        assert lt.parse_date(raw) == expected


class TestParseNumber:
    @pytest.mark.parametrize("raw,expected", [
        ("2026", 2026.0),
        ("3.14", 3.14),
        ("1,000", 1000.0),
        ("1,000,000", 1000000.0),
        ("number(3.14)", 3.14),
        ('number("1,000")', 1000.0),
    ])
    def test_accepts(self, raw, expected):
        assert lt.parse_number(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "", "3호", "1.2.3", "number(abc)"])
    def test_rejects(self, raw):
        assert lt.parse_number(raw) is None

    @pytest.mark.parametrize("raw,expected", [
        ("-672", -672.0),
        ("-2.5", -2.5),
        ("-1,000", -1000.0),
        ("number(-672)", -672.0),
    ])
    def test_accepts_negative(self, raw, expected):
        # a loss / credit / delta may be negative — number is not magnitude-only.
        assert lt.parse_number(raw) == expected


class TestParseNumberScaled:
    @pytest.mark.parametrize("raw,expected", [
        ("2.5", 2500),
        ("2026", 2026000),
        ("1,000", 1000000),
        ("0", 0),
        # IEEE-754 divergence proofs: a float path mis-rounds these; Decimal is
        # exact. 1.0005 * 1000 == 1000.4999999... as a float -> 1000, but the
        # exact scaled value is 1000.5 -> ROUND_HALF_UP -> 1001.
        ("1.0005", 1001),
        ("0.0005", 1),
        ("number(2.5)", 2500),
        ('number("1,000")', 1000000),
    ])
    def test_accepts(self, raw, expected):
        assert lt.parse_number_scaled(raw) == expected

    @pytest.mark.parametrize("raw", ["abc", "", "3호", "1.2.3", "number(abc)"])
    def test_rejects(self, raw):
        assert lt.parse_number_scaled(raw) is None

    def test_returns_int_never_float(self):
        assert type(lt.parse_number_scaled("2.5")) is int

    @pytest.mark.parametrize("raw,expected", [
        ("-672", -672000),
        ("-2.5", -2500),
        ("-1,000", -1000000),
        ("number(-672000000)", -672000000000),
        # ROUND_HALF_UP on a negative ties away from zero: -1000.5 -> -1001.
        ("-1.0005", -1001),
    ])
    def test_accepts_negative(self, raw, expected):
        assert lt.parse_number_scaled(raw) == expected


class TestParseOrdinal:
    @pytest.mark.parametrize("raw,expected", [
        ("제3호", 3), ("3위", 3), ("3rd", 3), ("1st", 1), ("12th", 12), ("제5번", 5),
        ("ordinal(3)", 3),
    ])
    def test_accepts(self, raw, expected):
        assert lt.parse_ordinal(raw) == expected

    @pytest.mark.parametrize("raw", ["3", "100억", "2026년", "", "third"])
    def test_rejects(self, raw):
        # bare numbers, amount/date units, and words are not ordinals
        assert lt.parse_ordinal(raw) is None


class TestParseAmount:
    @pytest.mark.parametrize("raw,expected", [
        ("100억", 10000000000),
        ("1,000원", 1000),
        ("50억", 5000000000),
        ("1조", 1000000000000),
        ("100 억", 10000000000),  # single space allowed
        ("amount(100, 억)", 10000000000),
        ('amount("2.675", "억")', 267500000),
        ('amount(100,"억")', 10000000000),  # quoted table unit
    ])
    def test_accepts(self, raw, expected):
        assert lt.parse_amount(raw, lt.DEFAULT_AMOUNT_UNITS) == expected

    @pytest.mark.parametrize("raw", [
        'amount(120,"kilometer per hour")',  # quoted, spaced, not a table unit
        'amount(2,"달러,센트")',                # quoted, comma, not a table unit
    ])
    def test_quoted_unknown_unit_is_none(self, raw):
        # A quoted unit with spaces/commas parses structurally but is not in the
        # unit table, so it has no comparable scalar (still a valid stored object).
        assert lt.parse_amount(raw, lt.DEFAULT_AMOUNT_UNITS) is None

    def test_decimal_is_exact(self):
        # int(2.675 * 1e8) == 267499999 (IEEE-754 error); Decimal is exact.
        assert lt.parse_amount("2.675억", lt.DEFAULT_AMOUNT_UNITS) == 267500000

    @pytest.mark.parametrize("raw", ["3GB", "제3호", "50%", "2026년", "3 GB", "", "억"])
    def test_rejects(self, raw):
        # unknown/ASCII units, ordinal marker, percent, date unit -> None
        assert lt.parse_amount(raw, lt.DEFAULT_AMOUNT_UNITS) is None

    def test_returns_int_never_float(self):
        result = lt.parse_amount("2.675억", lt.DEFAULT_AMOUNT_UNITS)
        assert type(result) is int

    @pytest.mark.parametrize("raw,expected", [
        ("-100억", -10000000000),
        ("-1,000원", -1000),
        ('amount(-100, "억")', -10000000000),
    ])
    def test_accepts_negative(self, raw, expected):
        # a negative amount (a loss / refund) projects to a negative base unit.
        assert lt.parse_amount(raw, lt.DEFAULT_AMOUNT_UNITS) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("100억원", 10000000000),      # fused 억 + 원
        ("12000억원", 1200000000000),
        ("1.2조원", 1200000000000),
        ("-500억원", -50000000000),
        ("5,400억원", 540000000000),
        ("100 억원", 10000000000),      # single space + fused suffix
        ("100원원", 100),               # redundant marker: stem 원 is still ×1
    ])
    def test_accepts_fused_currency_suffix(self, raw, expected):
        # (#205) a fused currency suffix (억원/조원) recovers by stripping one
        # trailing 원 and re-looking-up the stem in the unit table. Recovery is
        # gated on the stem being a real unit, so a redundant 원원 or a spaced
        # 100 억원 resolves, but an unknown stem never does (see rejects below).
        assert lt.parse_amount(raw, lt.DEFAULT_AMOUNT_UNITS) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("100억", 10000000000),        # bare stem still parses first-pass
        ("1,000원", 1000),             # 원 is a valid unit, matched first-pass
        ("1.2조", 1200000000000),
        ('amount(100,"억")', 10000000000),
        ("2.675억", 267500000),
    ])
    def test_fused_suffix_does_not_regress_known_inputs(self, raw, expected):
        # (#205) the 원-strip retry only fires after a first-pass miss, so inputs
        # that already parsed are unchanged (esp. 1,000원 where 원 IS the unit).
        assert lt.parse_amount(raw, lt.DEFAULT_AMOUNT_UNITS) == expected

    @pytest.mark.parametrize("raw", ["백만원", "3 GB", "50%", "$22M", "22M", "달러원"])
    def test_unknown_unit_still_none(self, raw):
        # (#205) stripping 원 must not guess: 백만원 -> 백만 (unknown) stays None,
        # and foreign-currency / non-KRW units remain None (no-guess contract).
        assert lt.parse_amount(raw, lt.DEFAULT_AMOUNT_UNITS) is None

    @pytest.mark.parametrize("raw,expected", [
        # int64 max is 9_223_372_036_854_775_807; 9,223,372조 == 9.223372e18 fits.
        ("9223372조", 9223372000000000000),
        (str(lt._INT64_MAX), lt._INT64_MAX),          # exactly max, unit 원
        (str(lt._INT64_MIN), lt._INT64_MIN),          # exactly min, unit 원
    ])
    def test_int64_boundary_in_range(self, raw, expected):
        # (#205) values inside the signed-64-bit range are returned as-is.
        assert lt.parse_amount(raw + "원" if raw[-1].isdigit() else raw,
                               lt.DEFAULT_AMOUNT_UNITS) == expected

    @pytest.mark.parametrize("raw", [
        "9300000조",                  # 9.3e18 > int64 max -> overflow guard
        "-9300000조",                 # symmetric lower-bound overflow
        str(lt._INT64_MAX + 1),       # one past max (unit 원 appended below)
        str(lt._INT64_MIN - 1),       # one past min
    ])
    def test_int64_out_of_range_is_none(self, raw):
        # (#205) values outside [int64_min, int64_max] would overflow the engine's
        # int64 column, so parse_amount returns None (untyped) instead of guessing.
        text = raw + "원" if raw.lstrip("-").isdigit() else raw
        assert lt.parse_amount(text, lt.DEFAULT_AMOUNT_UNITS) is None

    def test_fused_suffix_with_custom_units(self):
        # (#205) the 원-strip retry uses the caller-supplied table, not a hardcoded
        # default: 억원 -> 억 looked up in the custom table.
        custom = {"원": 1, "억": 10**8}
        assert lt.parse_amount("3억원", custom) == 300000000
        # a stem absent from the custom table stays None (no-guess).
        assert lt.parse_amount("3조원", custom) is None


class TestCanonicalAmount:
    """always-quote (wirelog#924): an amount compound term stores its unit always
    quoted as ``amount(N,"unit")``. The engine .dl text parser supports \\" escapes,
    so the quoted unit loads cleanly, and quoting keeps a unit with spaces/commas
    unambiguous."""

    @pytest.mark.parametrize("raw,expected", [
        ('amount(7,"억")', 'amount(7,"억")'),
        ('amount(7,억)', 'amount(7,"억")'),               # bare unit -> quoted
        ('amount(1,000,"억")', 'amount(1000,"억")'),       # comma stripped from the number
        ('amount("2.675", "억")', 'amount(2.675,"억")'),
        ("amount(100, 억)", 'amount(100,"억")'),           # bare + spacing normalised
        ('amount(-100,"억")', 'amount(-100,"억")'),         # negative preserved
        ('amount(120,"kilometer per hour")', 'amount(120,"kilometer per hour")'),  # spaces in unit
        ('amount(2,"달러,센트")', 'amount(2,"달러,센트")'),   # comma in (quoted) unit
    ])
    def test_always_quoted_canonical(self, raw, expected):
        assert lt.canonical_amount(raw) == expected

    def test_canonical_quotes_the_unit(self):
        canon = lt.canonical_amount('amount(7,억)')
        assert canon == 'amount(7,"억")' and canon.count('"') == 2

    def test_canonical_is_idempotent(self):
        canon = lt.canonical_amount('amount(7,억)')
        assert lt.canonical_amount(canon) == canon

    def test_canonical_still_parses_to_same_scalar(self):
        canon = lt.canonical_amount('amount(7,"억")')
        assert lt.parse_amount(canon, lt.DEFAULT_AMOUNT_UNITS) == 700000000

    @pytest.mark.parametrize("raw", ["100억", "number(5)", "date(2030,1)", "", "Acme"])
    def test_non_amount_is_none(self, raw):
        assert lt.canonical_amount(raw) is None


class TestNormalizeDispatcher:
    def test_dispatches_by_tag(self):
        assert lt.normalize("date", "2030.1") == 20300101
        # number now projects as a scaled int64 (×1000), not a float (#125).
        assert lt.normalize("number", "3.14") == 3140
        assert lt.normalize("ordinal", "3위") == 3

    def test_amount_uses_default_table(self):
        # amount is no longer an unknown tag: with no table it uses the default.
        assert lt.normalize("amount", "100억") == 10000000000

    def test_amount_uses_passed_table(self):
        assert lt.normalize("amount", "3.3억", {"억": 10**8}) == 330000000

    def test_unknown_tag_is_none(self):
        assert lt.normalize("nonsense", "x") is None

    def test_non_parsing_is_none(self):
        assert lt.normalize("date", "not a date") is None

    def test_types_constant(self):
        assert lt.TYPES == {"date", "number", "ordinal", "amount"}

    def test_deterministic(self):
        assert lt.normalize("date", "2030.1") == lt.normalize("date", "2030.1")

    def test_module_is_pure(self):
        # the module must not import the engine into its namespace
        assert not hasattr(lt, "pyrewire")
        assert not hasattr(lt, "EasySession")


class TestLiteralReConsistency:
    """Pinning test (#117 option b): the entity_audit detector and these
    normalizers must not drift. Every canonical literal example that entity_audit
    flags as a literal is parseable by its intended-type normalizer."""

    # (raw, intended type, expected scalar)
    # NB: only amount canonicals that _LITERAL_RE ALREADY detects belong here.
    # entity_audit's amount detection is partial/advisory (e.g. it does not flag
    # `1,000원` or `3.3억`); parse_amount is intentionally more permissive. We do
    # not widen the advisory detector to match — a known minor gap.
    CANONICAL = [
        ("2030.1", "date", 20300101),
        ("2024-07-01", "date", 20240701),
        ("2026", "number", 2026000),
        ("1,000", "number", 1000000),
        ("3.14", "number", 3140),
        ("제3호", "ordinal", 3),
        ("3위", "ordinal", 3),
        ("100억", "amount", 10000000000),
    ]

    @pytest.mark.parametrize("raw,type_tag,expected", CANONICAL)
    def test_detected_and_parsed(self, raw, type_tag, expected):
        from entity_audit import _LITERAL_RE
        assert _LITERAL_RE.match(raw), f"entity_audit no longer detects {raw!r}"
        assert lt.normalize(type_tag, raw) == expected

    @pytest.mark.parametrize("raw,type_tag", [
        ("１００억", "amount"),
        ("２０２６", "number"),
        ("제３호", "ordinal"),
        ("２０３０.１", "date"),
    ])
    def test_full_width_divergence_is_intended(self, raw, type_tag):
        """The two must NOT be made consistent for full-width digits (#331).

        entity_audit's detector is a loose smell test; these normalizers are a
        strict ASCII-only contract. That divergence is load-bearing: a relation
        that is not declared an attribute puts a matching object into
        ``literal_suspects``, which is the second user-visible path a rejected
        full-width value takes. Narrowing ``_LITERAL_RE`` to ``[0-9]`` "for
        consistency" would silently close it — so pin both halves together.
        """
        from entity_audit import _LITERAL_RE
        assert _LITERAL_RE.match(raw), f"entity_audit must still SEE {raw!r}"
        assert lt.normalize(type_tag, raw) is None, f"{raw!r} must NOT parse"


class TestFullWidthDigitsRejected:
    """ASCII-only digits (#331). Python's ``\\d`` covers the whole Unicode ``Nd``
    category, so a full-width ``１００억`` used to normalize to the SAME scalar as
    ``100억`` while ``relation/3`` stored a different object string — the two
    spellings merged under a typed relation but stayed separate entities and
    missed each other in object-match queries. The policy is reject, not fold: a
    full-width value takes the ordinary "does not parse -> untyped" path."""

    @pytest.mark.parametrize("type_tag,raw", [
        # Each of these returned a scalar before the fix (20200101 / 20200101 /
        # 123000 / 3 / 10000000000 / 10000000000), never None.
        ("date", "date(２０２０,１)"),
        ("date", "２０２０.１"),
        ("number", "number(１２３)"),
        ("number", "１２３"),
        ("ordinal", "ordinal(３)"),
        ("ordinal", "３위"),
        ("ordinal", "제３호"),
        ("ordinal", "３rd"),   # the English ordinal form has its own regex
        ("amount", "１００억"),
        ("amount", 'amount(１００,"억")'),
    ])
    def test_normalize_rejects(self, type_tag, raw):
        assert lt.normalize(type_tag, raw) is None

    @pytest.mark.parametrize("parser,raw", [
        (lt.parse_date, "date(２０２０,１)"),
        (lt.parse_number, "number(１２３)"),
        (lt.parse_number_scaled, "number(１２３)"),
        (lt.parse_ordinal, "ordinal(３)"),
    ])
    def test_public_parsers_reject(self, parser, raw):
        assert parser(raw) is None

    def test_parse_amount_rejects(self):
        assert lt.parse_amount("１００억", lt.DEFAULT_AMOUNT_UNITS) is None

    @pytest.mark.parametrize("type_tag,raw", [
        # Half-and-half spellings are the realistic accident (an IME left in
        # full-width mode mid-token), and they must not parse either.
        ("amount", "1２3억"),
        ("amount", 'amount(１0,"억")'),
        ("amount", 'amount("１００","억")'),
        ("number", 'number("1２3")'),
        ("number", "1２3"),
        ("date", "date(20２0,1)"),
        ("ordinal", "ordinal(1２)"),
        ("ordinal", "1２th"),
    ])
    def test_mixed_width_rejects(self, type_tag, raw):
        assert lt.normalize(type_tag, raw) is None

    def test_full_width_no_longer_shares_a_scalar_with_ascii(self):
        # The motivating bug in one line: same scalar, different stored string.
        assert lt.normalize("amount", "100억") == 10000000000
        assert lt.normalize("amount", "１００억") is None

    @pytest.mark.parametrize("raw", ['amount(１００,억)', 'amount(１００,"억")'])
    def test_canonical_amount_rejects(self, raw):
        # Consequence, by design: merge_candidates' dedup key no longer collapses
        # the bare and quoted full-width spellings into one row. Folding them is
        # the silent rewrite this policy refuses.
        assert lt.canonical_amount(raw) is None

    @pytest.mark.parametrize("raw", ['amount(１００,"억")', "date(２０２０,１)", "number(１２３)"])
    def test_humanize_returns_full_width_verbatim(self, raw):
        # Unrecognized -> verbatim, the documented humanize fallback.
        assert lt.humanize(raw) == raw


class TestFullWidthSurfaces:
    """AC3 (#331): a rejected full-width value must reach a user-visible path at
    least once. Under a relation declared typed, the projection loop warns on
    stderr and loads the fact untyped — the same path any malformed literal takes.

    Residual symptom, deliberately NOT covered: a relation declared as an
    attribute but NOT typed has no spec, so ``_project_typed_relations`` skips it
    without warning and the full-width value surfaces nowhere."""

    class _FakeSession:
        """Records inserts; _project_typed_relations touches nothing else."""

        def __init__(self):
            self.inserts = []

        def intern(self, value):
            return 1

        def insert(self, alias, payload):
            self.inserts.append((alias, payload))

    def test_projection_warns_and_loads_untyped(self, capsys):
        import common

        specs = {"출시일": common.TypedRelSpec("date", "launch_date")}
        rows = [
            {"subject": "제품", "relation": "출시일", "object": "date(２０２０,１)"},
            {"subject": "제품2", "relation": "출시일", "object": "date(2020,1)"},
        ]
        session = self._FakeSession()
        common._project_typed_relations(session, specs, rows)
        err = capsys.readouterr().err
        assert "date(２０２０,１)" in err
        assert "does not parse as date" in err
        # Only the ASCII row projects; the full-width fact still loads untyped.
        assert session.inserts == [("launch_date", (1, 20200101))]


class TestAsciiDigitsUnchanged:
    """Regression guard for the narrowing in #331: the ASCII forms that already
    parsed must keep parsing, including the ones a careless narrowing would break
    (a space-separated unit, a comma inside the number, and — the reason
    ``re.ASCII`` is NOT used — a U+3000 ideographic space as separator whitespace).
    These pin existing behaviour; they do not prove the fix."""

    @pytest.mark.parametrize("type_tag,raw,expected", [
        ("amount", "100 억", 10000000000),
        ("amount", "100억", 10000000000),
        ("amount", 'amount(1,000,"억")', 100000000000),
        ("amount", "1,000원", 1000),
        ("date", "date(2020)", 20200101),
        ("date", "date(2020,　1)", 20200101),  # U+3000 stays whitespace
        ("date", "2030.1", 20300101),
        ("number", "1,000", 1000000),
        ("number", "3.14", 3140),
        ("ordinal", "제3호", 3),
        ("ordinal", "3rd", 3),
    ])
    def test_ascii_forms_still_parse(self, type_tag, raw, expected):
        assert lt.normalize(type_tag, raw) == expected

    def test_ascii_canonical_amount_unchanged(self):
        assert lt.canonical_amount("amount(1,000,억)") == 'amount(1000,"억")'

    def test_ascii_humanize_unchanged(self):
        assert lt.humanize('amount(7,"억")') == "7억"
        assert lt.humanize("date(2030,1,15)") == "2030-01-15"
