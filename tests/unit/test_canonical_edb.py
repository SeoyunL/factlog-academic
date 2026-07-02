# SPDX-License-Identifier: Apache-2.0
"""Unit tests for #227 SLICE 2 COMMIT 1: canonical/3 EDB relation.

- _load_accepted_facts_from skips canonical(...) lines without raising.
- canonical_atoms: alias-key→canon, alias-value→itself, non-participating→skipped,
  dedup, empty aliases.
- policy_predicates(load_logic_policy()) does NOT contain 'canonical'.
- classify_query rejects a canonical(...)? draft as unknown_predicate.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import factlog.common as fcommon


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_accepted_dl(tmp_path: Path, content: str) -> Path:
    facts = tmp_path / "facts"
    facts.mkdir(parents=True, exist_ok=True)
    p = facts / "accepted.dl"
    p.write_text(content, encoding="utf-8")
    return p


def _make_facts(rows: list[tuple[str, str, str]]) -> list[dict[str, str]]:
    return [
        {"subject": s, "relation": r, "object": o, "status": "confirmed",
         "source": "sources/test.md", "confidence": "0.90", "note": ""}
        for s, r, o in rows
    ]


# ---------------------------------------------------------------------------
# _load_accepted_facts_from: reader isolation
# ---------------------------------------------------------------------------

class TestLoadAcceptedFactsCanonicalIsolation:
    """canonical(...) lines must be silently skipped; relation(...) lines kept."""

    def test_canonical_lines_skipped_no_raise(self, tmp_path):
        """A file with canonical(...) lines does not raise and the lines are not parsed."""
        content = textwrap.dedent("""\
            // generated from facts/candidates.csv
            // only confirmed/accepted facts become engine input

            relation("doc1", "결론", "true").
            canonical("doc1", "결론", "true").
            relation("doc2", "uses", "FastAPI").
        """)
        p = _make_accepted_dl(tmp_path, content)
        rows = fcommon._load_accepted_facts_from(p)
        # Only the two relation(...) rows survive
        assert len(rows) == 2
        assert all(r["relation"] != "canonical" for r in rows)

    def test_canonical_lines_never_appear_in_rows(self, tmp_path):
        """canonical names never appear as relation values in the parsed rows."""
        content = textwrap.dedent("""\
            relation("A", "결론", "X").
            canonical("A", "결론", "X").
            canonical("B", "결론", "Y").
        """)
        p = _make_accepted_dl(tmp_path, content)
        rows = fcommon._load_accepted_facts_from(p)
        relations = [r["relation"] for r in rows]
        assert "canonical" not in relations
        assert len(rows) == 1
        assert rows[0] == {"subject": "A", "relation": "결론", "object": "X"}

    def test_only_canonical_lines_returns_empty(self, tmp_path):
        """A file with ONLY canonical lines yields an empty list."""
        content = textwrap.dedent("""\
            // only canonical lines
            canonical("A", "rel", "B").
            canonical("C", "rel", "D").
        """)
        p = _make_accepted_dl(tmp_path, content)
        rows = fcommon._load_accepted_facts_from(p)
        assert rows == []

    def test_relation_fact_re_matches_only_relation(self):
        """RELATION_FACT_RE must NOT match a canonical(...). line."""
        line = 'canonical("doc1", "결론", "true").'
        assert fcommon.RELATION_FACT_RE.match(line) is None
        # But it does match relation(...)
        rel_line = 'relation("doc1", "결론", "true").'
        assert fcommon.RELATION_FACT_RE.match(rel_line) is not None


# ---------------------------------------------------------------------------
# canonical_atoms helper
# ---------------------------------------------------------------------------

class TestCanonicalAtoms:
    def test_alias_key_maps_to_canonical(self):
        """A row whose relation is an alias key → (S, canonical, O)."""
        rows = _make_facts([("doc1", "결론", "true")])
        aliases = {"결론": "concludes"}
        result = fcommon.canonical_atoms(rows, aliases)
        assert result == [("doc1", "concludes", "true")]

    def test_alias_value_maps_to_itself(self):
        """A row whose relation IS the canonical value → (S, R, O) with canon=R."""
        rows = _make_facts([("doc1", "concludes", "true")])
        aliases = {"결론": "concludes"}
        result = fcommon.canonical_atoms(rows, aliases)
        assert result == [("doc1", "concludes", "true")]

    def test_non_participating_skipped(self):
        """A relation in neither alias keys nor alias values → skipped."""
        rows = _make_facts([("doc1", "unrelated", "X")])
        aliases = {"결론": "concludes"}
        result = fcommon.canonical_atoms(rows, aliases)
        assert result == []

    def test_dedup_two_variants_same_subject_object(self):
        """Two rows with different surface relations but same (S, canon, O) → one triple."""
        rows = _make_facts([
            ("doc1", "결론", "true"),
            ("doc1", "concludes", "true"),  # concludes is the canonical value itself
        ])
        aliases = {"결론": "concludes"}
        result = fcommon.canonical_atoms(rows, aliases)
        assert len(result) == 1
        assert result[0] == ("doc1", "concludes", "true")

    def test_dedup_preserves_first_occurrence(self):
        """First-occurrence stable: the first triple is kept, not the second."""
        rows = _make_facts([
            ("doc1", "결론", "true"),  # alias key → (doc1, concludes, true)
            ("doc1", "결론", "false"),  # different object → kept separately
        ])
        aliases = {"결론": "concludes"}
        result = fcommon.canonical_atoms(rows, aliases)
        assert len(result) == 2
        assert ("doc1", "concludes", "true") in result
        assert ("doc1", "concludes", "false") in result

    def test_empty_aliases_returns_empty(self):
        """Empty aliases dict → [] regardless of rows."""
        rows = _make_facts([("A", "rel", "B"), ("C", "rel2", "D")])
        result = fcommon.canonical_atoms(rows, {})
        assert result == []

    def test_empty_rows_returns_empty(self):
        """Empty rows → []."""
        result = fcommon.canonical_atoms([], {"a": "b"})
        assert result == []

    def test_multiple_aliases_same_canonical(self):
        """Multiple surface variants all mapping to the same canonical."""
        rows = _make_facts([
            ("doc1", "결론", "X"),
            ("doc2", "conclusion", "Y"),
            ("doc3", "concludes", "Z"),  # canonical itself stored literally
        ])
        aliases = {"결론": "concludes", "conclusion": "concludes"}
        result = fcommon.canonical_atoms(rows, aliases)
        assert len(result) == 3
        assert all(r[1] == "concludes" for r in result)

    def test_nfd_relation_normalized_before_lookup(self):
        """NFD-authored relation names are NFC-normalized before alias lookup."""
        import unicodedata
        nfd_rel = unicodedata.normalize("NFD", "결론")
        nfc_rel = unicodedata.normalize("NFC", "결론")
        assert nfd_rel != nfc_rel  # sanity: forms differ on macOS
        # aliases has NFC key (as relation_aliases() always produces)
        aliases = {nfc_rel: "concludes"}
        rows = [{"subject": "doc1", "relation": nfd_rel, "object": "X",
                 "status": "confirmed", "source": "s.md", "confidence": "0.9", "note": ""}]
        result = fcommon.canonical_atoms(rows, aliases)
        assert result == [("doc1", "concludes", "X")]

    def test_distinct_subjects_each_get_their_triple(self):
        """Different subjects with the same relation → separate triples."""
        rows = _make_facts([
            ("doc1", "결론", "X"),
            ("doc2", "결론", "Y"),
        ])
        aliases = {"결론": "concludes"}
        result = fcommon.canonical_atoms(rows, aliases)
        assert len(result) == 2
        subjects = {r[0] for r in result}
        assert subjects == {"doc1", "doc2"}


# ---------------------------------------------------------------------------
# canonical is NOT a query predicate
# ---------------------------------------------------------------------------

class TestCanonicalNotQueryPredicate:
    def test_policy_predicates_does_not_contain_canonical(self):
        """policy_predicates on any policy text that uses canonical/3 as EDB
        must NOT surface 'canonical' — it has no .decl in policy (it's in
        WIRELOG_PROGRAM, not in policy_program)."""
        # A policy that references canonical in a rule body but never .decl-s it
        policy_text = (
            ".decl conflict(x: symbol, reason: symbol)\n"
            'conflict(X, "retracted") :- canonical(X, "concludes", _), '
            'canonical(X, "retraction_status", _).\n'
        )
        preds = fcommon.policy_predicates(policy_text)
        assert "canonical" not in preds
        assert "conflict" in preds

    def test_classify_query_rejects_canonical_as_unknown_predicate(self):
        """canonical(...)? is rejected as QUERY_UNKNOWN_PREDICATE — it is engine-only."""
        facts = _make_facts([("doc1", "결론", "X")])
        ok, code, reason = fcommon.classify_query(
            'canonical("doc1", "concludes", X)?', facts, policy_program=""
        )
        assert ok is False
        assert code == fcommon.QUERY_UNKNOWN_PREDICATE
