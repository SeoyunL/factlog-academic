# SPDX-License-Identifier: Apache-2.0
"""The reserved-predicate sets must cover every engine .decl (#332/#334).

WIRELOG_PROGRAM declares six predicates. A generated bullet, a typed-relation alias,
or a hand-authored policy .decl that HEADS one of them is silently mishandled by the
engine with rc=0. That concept lives in FOUR hand-managed sets, and hand-managed sets
drift: #332 is where relation_alive (the #308 witness) was never added to the
generator's RESERVED_PREDICATES and review_required (declared by no .decl) lingered.
These tests pin the coverage so a future engine predicate cannot slip in unguarded.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

import factlog.common as fcommon

sys.path.insert(0, str(Path.cwd() / "tools"))
import generate_logic_policy as glp  # noqa: E402


def _wirelog_decls() -> set[str]:
    return set(
        re.findall(r"^\.decl\s+([a-z_][a-z0-9_]*)\(", fcommon.WIRELOG_PROGRAM, re.MULTILINE)
    )


class TestReservedPredicatesCoverEngineDecls:
    def test_reserved_predicates_superset_of_wirelog_decls(self):
        """Every predicate the engine declares must be reserved from generated heads."""
        decls = _wirelog_decls()
        assert decls, "WIRELOG_PROGRAM declared no .decl — regex or program changed"
        missing = decls - glp.RESERVED_PREDICATES
        assert not missing, f"RESERVED_PREDICATES misses engine .decl(s): {sorted(missing)}"

    def test_relation_alive_is_reserved(self):
        """The #308 witness predicate is explicitly covered (the drift #332 fixes)."""
        assert "relation_alive" in glp.RESERVED_PREDICATES

    def test_review_required_is_not_reserved(self):
        """review_required is declared by no .decl; keeping it only blocked a valid name."""
        assert "review_required" not in glp.RESERVED_PREDICATES


class TestGeneratorRejectsReservedHeadBullet:
    def test_relation_alive_bullet_is_rejected(self):
        """A bullet whose inferred predicate is relation_alive must fail at GENERATION.

        Otherwise it compiles to `.decl relation_alive(entity, reason)` — an arity-2
        re-declaration of the engine's arity-1 witness that pyrewire parses with rc=0,
        so main() writes the file and then EVERY load rejects it as a reserved-predicate
        clash: the KB is bricked until the generated output is hand-edited (#332).
        """
        payload = {
            "rules": [
                {
                    "predicate": "relation_alive",
                    "reason": "hijack",
                    "conditions": [{"relation": "cites"}],
                }
            ]
        }
        with pytest.raises(ValueError, match="relation_alive"):
            glp.normalized_rules(payload)

    def test_an_ordinary_policy_predicate_still_compiles(self):
        """Control: a non-reserved predicate name is still accepted."""
        payload = {
            "rules": [
                {
                    "predicate": "conflict",
                    "reason": "dup",
                    "conditions": [{"relation": "cites"}],
                }
            ]
        }
        rules = glp.normalized_rules(payload)
        assert rules[0]["predicate"] == "conflict"
