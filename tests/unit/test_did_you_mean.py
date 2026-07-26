"""Deterministic, trusted-vocabulary spelling hints (#273)."""

from factlog.common import nearby_vocabulary


def test_nearby_vocabulary_is_nfc_case_exact_safe_sorted_and_capped():
    vocabulary = {"CATS", "catz", "cata", "catb", "catc", "가기업"}

    # Exact values, including case/NFC-equivalent spellings, are excluded from
    # the candidate list even if a caller asks for nearby alternatives.
    assert "CATS" not in nearby_vocabulary("cats", vocabulary)
    assert nearby_vocabulary("가기업", vocabulary) == []
    # Distance ties have a stable lexical order and the public cap is three.
    assert nearby_vocabulary("cat", vocabulary) == ["cata", "catb", "catc"]


def test_nearby_vocabulary_rejects_distant_and_one_character_terms():
    vocabulary = {"Acme API", "Postgres"}
    assert nearby_vocabulary("Completely Distant", vocabulary) == []
    assert nearby_vocabulary("A", vocabulary) == []
