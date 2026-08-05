# SPDX-License-Identifier: Apache-2.0
"""#329 added ``attr_rel`` and ``entity_node`` to three separate reserved-name
lists, and NONE of the three was covered: reverting each one individually — and
all three together — left the whole suite and every shell harness green.

Each list guards a different way a colliding name reaches the engine program:

* ``common._TYPED_RESERVED`` — a typed-relation alias becomes a ``.decl`` in the
  assembled program. (``_assert_no_alias_collision`` backstops this one at run
  time; the other two have no backstop.)
* ``common.policy_predicates`` ``built_in`` — decides which ``.decl``s in the
  policy text are *policy* predicates to evaluate and answer queries for.
* ``generate_logic_policy.RESERVED_PREDICATES`` — a bullet in logic-policy.md
  compiles to a rule HEAD, which would make pyrewire treat the EDB as IDB and
  drop the injected atoms.

The hand-written-policy axis is guarded separately at load time; see
tests/unit/test_canonical_head_guard.py::TestReservedAttributePredicates.
"""
from __future__ import annotations

import pytest

import common
import generate_logic_policy as g


class TestTypedRelationAlias:
    """common._TYPED_RESERVED — a typed alias may not take an engine name."""

    @pytest.mark.parametrize("alias", ["entity_node", "attr_rel"])
    def test_alias_named_after_an_engine_predicate_is_rejected(self, alias):
        with pytest.raises(common.FactlogError, match="reserved or existing name"):
            common._parse_typed_relations(f"- `정식_운영` : date as {alias}\n")

    def test_a_neighbouring_name_is_still_accepted(self):
        # CONTROL — the guard must not widen to names that merely contain one.
        specs = common._parse_typed_relations("- `정식_운영` : date as entity_node_v2\n")
        assert specs["정식_운영"].alias == "entity_node_v2"


class TestPolicyPredicates:
    """common.policy_predicates — an engine .decl is never a policy predicate.

    A name reported here is evaluated and answered as a policy query; reporting
    an engine predicate would make `factlog check` claim to answer queries the
    policy does not define.
    """

    @pytest.mark.parametrize("name", ["entity_node", "attr_rel"])
    def test_engine_declaration_is_not_a_policy_predicate(self, name):
        assert common.policy_predicates(f".decl {name}(a: symbol, b: symbol)\n") == set()

    def test_a_real_policy_declaration_is_still_returned(self):
        # CONTROL — non-vacuous: the same call shape does return user predicates.
        assert common.policy_predicates(
            ".decl conflict(entity: symbol, reason: symbol)\n"
        ) == {"conflict"}


class TestGeneratedPolicyPredicateName:
    """generate_logic_policy.RESERVED_PREDICATES — a bullet may not generate a
    head on an engine predicate."""

    @pytest.mark.parametrize("name", ["attr_rel", "entity_node"])
    def test_reserved_predicate_bullet_is_rejected(self, name):
        rules = {"rules": [{
            "predicate": name,
            "reason": "literal_node",
            "conditions": [{"relation": "정식_운영"}],
        }]}
        with pytest.raises(ValueError, match="invalid policy predicate name"):
            g.normalized_rules(rules)

    def test_an_ordinary_predicate_bullet_still_compiles(self):
        # CONTROL — non-vacuous: the same rule shape is accepted under a free name.
        rules = {"rules": [{
            "predicate": "literal_rel",
            "reason": "literal_node",
            "conditions": [{"relation": "정식_운영"}],
        }]}
        assert g.normalized_rules(rules)[0]["predicate"] == "literal_rel"
