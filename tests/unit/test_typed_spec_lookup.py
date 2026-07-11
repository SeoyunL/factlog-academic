"""A typed fact stored under an alias or NFD name must still find its spec (#244).

#227 surfaced value PARSE failures. The lookup one step earlier -- finding the spec for a
row's relation -- still missed an alias surface form or an NFD name, because specs are
keyed by the NFC canonical while accepted.dl stores the relation verbatim. So a fact
dropped from the typed comparison with warnings: 0, the same silent omission #227 is
about, one step up.
"""

import unicodedata

from factlog.common import (
    TypedRelSpec,
    _lookup_typed_spec,
    typed_projection_warnings,
)


def row(subject, relation, obj, status="accepted"):
    return {"subject": subject, "relation": relation, "object": obj, "status": status}


SPECS = {"published_year": TypedRelSpec(type="date", alias="pubdate")}
ALIASES = {"pub_year": "published_year"}


def test_lookup_folds_an_alias_to_its_canonical():
    assert _lookup_typed_spec("pub_year", SPECS, ALIASES) is SPECS["published_year"]


def test_lookup_folds_nfc():
    hangul_specs = {"발표일": TypedRelSpec(type="date", alias="pubdate")}
    nfd_name = unicodedata.normalize("NFD", "발표일")
    assert nfd_name != "발표일"  # genuinely decomposed
    assert _lookup_typed_spec(nfd_name, hangul_specs, {}) is hangul_specs["발표일"]


def test_lookup_returns_none_for_an_undeclared_relation():
    assert _lookup_typed_spec("mentions", SPECS, ALIASES) is None


def test_an_alias_fact_that_does_not_parse_is_warned():
    warns = typed_projection_warnings([row("C", "pub_year", "not-a-date")], SPECS, ALIASES)
    assert len(warns) == 1
    assert "pub_year" in warns[0]
    assert "EXCLUDED" in warns[0]


def test_an_alias_fact_that_parses_is_silent():
    assert typed_projection_warnings([row("B", "pub_year", "2031-02-20")], SPECS, ALIASES) == []


def test_an_nfd_fact_that_does_not_parse_is_warned():
    hangul_specs = {"발표일": TypedRelSpec(type="date", alias="pubdate")}
    nfd = unicodedata.normalize("NFD", "발표일")
    warns = typed_projection_warnings([row("A", nfd, "not-a-date")], hangul_specs, {})
    assert len(warns) == 1
    assert "EXCLUDED" in warns[0]


def test_without_the_alias_map_the_alias_fact_would_be_invisible():
    # the bug: no alias map -> spec not found -> no warning even though the fact drops
    assert typed_projection_warnings([row("C", "pub_year", "not-a-date")], SPECS, {}) == []
    # ...and WITH it, the drop is surfaced
    assert typed_projection_warnings([row("C", "pub_year", "not-a-date")], SPECS, ALIASES)
