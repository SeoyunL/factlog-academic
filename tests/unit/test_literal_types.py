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
    bibliographic record normally knows only the year — so it must parse (#385)."""

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

    # The docs enumerate the full rank-unit list; 호/위/번/rd/st/th and the
    # optional 제 are already covered above, so these are only the units that
    # no other case reaches.
    @pytest.mark.parametrize("raw,expected", [
        ("3차", 3), ("3등", 3), ("3째", 3), ("2nd", 2),
    ])
    def test_accepts_remaining_documented_units(self, raw, expected):
        assert lt.parse_ordinal(raw) == expected

    # The docs state whitespace between the number and the unit is allowed, but
    # that a leading 제 must sit directly against the number.
    @pytest.mark.parametrize("raw,expected", [("3 위", 3), ("3 rd", 3), ("제3 호", 3)])
    def test_accepts_space_before_unit(self, raw, expected):
        assert lt.parse_ordinal(raw) == expected

    def test_rejects_space_after_leading_je(self):
        assert lt.parse_ordinal("제 3호") is None

    # A bare 제 with no rank unit does not parse. (`3` and `rank 3` are the
    # docs' other counter-examples; they are pinned by ``test_rejects`` above
    # and by test_typed_parse_warnings.test_readme_ordinal_examples_all_parse.)
    def test_rejects_leading_je_without_unit(self):
        assert lt.parse_ordinal("제3") is None

    # The number itself is bare digits: no sign, grouping separator or decimal.
    @pytest.mark.parametrize("raw", ["-3위", "3,000위", "3.5위"])
    def test_rejects_non_bare_digits(self, raw):
        assert lt.parse_ordinal(raw) is None

    # The compound form is a SEPARATE branch (_ORDINAL_COMPOUND_RE, tried
    # first) that carries no rank unit — the "unit required" rule applies to
    # the prose form only. ``ordinal(3)`` in ``test_accepts`` is the base case;
    # these pin the tolerances the docs rely on when calling it a separate path.
    @pytest.mark.parametrize("raw,expected", [
        ("ORDINAL(3)", 3), ("ordinal( 3 )", 3),
    ])
    def test_compound_form_needs_no_unit(self, raw, expected):
        assert lt.parse_ordinal(raw) == expected


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


class TestFullWidthDigitsRejected:
    """All four parsers read ASCII digits only (#388).

    Python's ``\\d`` is the whole Unicode ``Nd`` category, so a full-width
    ``date(２０２０,１)`` used to parse to the SAME scalar as ``date(2020,1)`` while the
    stored object string stayed different. One value then behaved as two: a typed
    relation grouped the pair as equal (``_group_key`` keys on the scalar), an
    object-match query for the ASCII spelling missed the full-width row, and with no
    typed declaration the two became separate entities. NFC (this codebase's
    normalization) preserves full-width digits — only NFKC folds them — so nothing
    upstream collapsed them either. Rejecting routes such a value to the visible
    "does not parse" path instead.
    """

    FULLWIDTH = "０１２３４５６７８９"

    # (parser-name, full-width form, the ASCII form that MUST still parse, scalar)
    @pytest.mark.parametrize("raw,ascii_raw,expected", [
        # date: compound and prose, plus a MIXED half/full-width form — the mixed
        # case is the one a partial fix (year only, or compound only) would miss.
        ("date(２０２０,１)", "date(2020,1)", 20200101),
        ("date(２０２０)", "date(2020)", 20200101),
        ("date(2020,１)", "date(2020,1)", 20200101),
        ("date(２０２０,1)", "date(2020,1)", 20200101),
        ("date(2020,1,１５)", "date(2020,1,15)", 20200115),
        ("２０３０.１", "2030.1", 20300101),
        ("2030.１.１５", "2030.1.15", 20300115),
    ])
    def test_date(self, raw, ascii_raw, expected):
        assert lt.parse_date(raw) is None
        assert lt.parse_date(ascii_raw) == expected

    @pytest.mark.parametrize("raw,ascii_raw,expected", [
        ("１２３", "123", 123.0),
        ("number(１２３)", "number(123)", 123.0),
        ("1,０００", "1,000", 1000.0),      # mixed: full-width inside the group
        ("3.１４", "3.14", 3.14),           # mixed: full-width in the fraction
        ("-１", "-1", -1.0),
    ])
    def test_number(self, raw, ascii_raw, expected):
        assert lt.parse_number(raw) is None
        assert lt.parse_number(ascii_raw) == expected

    @pytest.mark.parametrize("raw,ascii_raw,expected", [
        ("１２３", "123", 123000),
        ("number(１２３)", "number(123)", 123000),
        ("2.５", "2.5", 2500),
    ])
    def test_number_scaled(self, raw, ascii_raw, expected):
        """The scaled parser shares _NUMBER_RE, so it must reject identically —
        it, not parse_number, is what `normalize('number', …)` dispatches to."""
        assert lt.parse_number_scaled(raw) is None
        assert lt.parse_number_scaled(ascii_raw) == expected

    @pytest.mark.parametrize("raw,ascii_raw,expected", [
        ("ordinal(３)", "ordinal(3)", 3),
        ("제３호", "제3호", 3),        # Korean ordinal branch
        ("３위", "3위", 3),
        ("３rd", "3rd", 3),           # English ordinal branch
        ("제1０호", "제10호", 10),     # mixed within one rank
    ])
    def test_ordinal(self, raw, ascii_raw, expected):
        assert lt.parse_ordinal(raw) is None
        assert lt.parse_ordinal(ascii_raw) == expected

    @pytest.mark.parametrize("raw,ascii_raw,expected", [
        ('amount(１００,"억")', 'amount(100,"억")', 10000000000),
        ("amount(１００,억)", "amount(100,억)", 10000000000),   # bare-unit form
        ("１００억", "100억", 10000000000),                      # prose form
        ("1,０００원", "1,000원", 1000),                         # mixed, comma group
        ("2.６억", "2.6억", 260000000),                          # mixed, fraction
    ])
    def test_amount(self, raw, ascii_raw, expected):
        assert lt.parse_amount(raw, lt.DEFAULT_AMOUNT_UNITS) is None
        assert lt.parse_amount(ascii_raw, lt.DEFAULT_AMOUNT_UNITS) == expected

    def test_amount_canonicalisation_also_rejects(self):
        """canonical_amount shares _AMOUNT_COMPOUND_RE. If it still matched, a
        full-width amount would be rewritten into the canonical form and land in
        accepted.dl looking well-formed while parse_amount calls it untyped."""
        assert lt.canonical_amount('amount(１００,"억")') is None
        assert lt.canonical_amount('amount(100,"억")') == 'amount(100,"억")'

    @pytest.mark.parametrize("type_tag,raw", [
        ("date", "date(２０２０,１)"),
        ("number", "number(１２３)"),
        ("ordinal", "ordinal(３)"),
        ("amount", 'amount(１００,"억")'),
    ])
    def test_normalize_dispatcher_rejects(self, type_tag, raw):
        """The dispatcher is what every caller (projection, _group_key,
        entity_audit) actually goes through, so pin the rejection there too."""
        assert lt.normalize(type_tag, raw) is None

    @pytest.mark.parametrize("raw,ascii_raw,shown", [
        ("date(２０２０,１)", "date(2020,1)", "2020-01"),
        ("number(１２３)", "number(123)", "123"),
        ('amount(１００,"억")', 'amount(100,"억")', "100억"),
    ])
    def test_humanize_leaves_full_width_verbatim(self, raw, ascii_raw, shown):
        """humanize must not display a value the parsers reject as if it were clean.

        This is the only place the compound regexes' digit class is observable for
        `number` THROUGH BEHAVIOUR: parse_number re-validates the captured group
        against _NUMBER_RE, so a full-width `number(１２３)` is rejected either way.
        humanize does not re-validate — it strips the wrapper and prints. Left wide,
        it would render `number(１２３)` as `123`, i.e. show a human the ASCII value
        the KB does NOT hold, while the projection warns the fact is excluded. The
        display and the warning would then contradict each other.

        This test was once the ONLY mutation defence for `_NUMBER_COMPOUND_RE`, which
        is a bad place for it: `humanize`'s own docstring declares it DISPLAY-ONLY, so
        a parsing-layer invariant was being held up by a display-layer test — a
        refactor of the display path would have dropped the coverage silently. The
        invariant now has a direct anchor in
        `test_number_compound_regex_rejects_full_width_directly` below; this test
        keeps only the display claim it is actually about.
        """
        assert lt.humanize(raw) == raw
        assert lt.humanize(ascii_raw) == shown

    def test_number_compound_regex_rejects_full_width_directly(self):
        """Anchor `_NUMBER_COMPOUND_RE`'s digit class at the parsing layer itself.

        Reaching into a private regex is deliberate here. `_NUMBER_COMPOUND_RE` is the
        one narrowed regex with NO observable behavioural consequence of its own —
        parse_number re-validates the captured group against `_NUMBER_RE`, so widening
        this one back to `\\d` changes no parse result. Every other narrowed regex is
        covered by a real parsing test (`_AMOUNT_COMPOUND_RE`, for one, feeds
        parse_amount/canonical_amount directly and fails loudly there).

        Without this assertion the only thing that noticed the mutation was a
        display-only humanize test. Asserting the regex directly costs a private
        reference and buys a defence that lives in the same layer as the invariant.
        """
        assert lt._NUMBER_COMPOUND_RE.match("number(１２３)") is None
        assert lt._NUMBER_COMPOUND_RE.match("number(1２3)") is None
        # the ASCII form must still match, or the narrowing went too far
        assert lt._NUMBER_COMPOUND_RE.match("number(123)") is not None
        assert lt._NUMBER_COMPOUND_RE.match('number("-1,234.5")') is not None

    def test_every_ascii_digit_still_parses(self):
        """Guard against a fix that narrows too far (e.g. `[1-9]`)."""
        assert lt.parse_number("1234567890") == 1234567890.0
        assert lt.parse_ordinal("ordinal(1234567890)") == 1234567890


class TestNonAsciiDigits:
    """The diagnostic that names WHY a value did not parse (#388). Full-width
    digits are near-invisible in a warning line, so the report sites append this."""

    def test_reports_offending_characters(self):
        assert lt.non_ascii_digits("date(２０２０,１)") == "２０１"

    def test_first_appearance_order_and_deduplicated(self):
        assert lt.non_ascii_digits("１１２") == "１２"

    def test_empty_for_ascii(self):
        assert lt.non_ascii_digits("date(2020,1)") == ""
        assert lt.non_ascii_digits("") == ""

    def test_ignores_non_decimal_digit_lookalikes(self):
        """`²` is category `No`, which `\\d` never matched, so it was never the
        cause of a rejection and must not be named as one."""
        assert lt.non_ascii_digits("2²") == ""

    def test_matches_exactly_what_the_parsers_reject(self):
        """The helper's claim and the parsers' behaviour must not drift: every
        digit it names must in fact fail, and a value it calls clean must parse."""
        for digit in TestFullWidthDigitsRejected.FULLWIDTH:
            assert lt.non_ascii_digits(digit) == digit
            assert lt.parse_number(f"1{digit}") is None
        assert lt.non_ascii_digits("100") == ""
        assert lt.parse_number("100") == 100.0


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
