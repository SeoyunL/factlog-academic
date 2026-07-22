#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Entity audit: surface entity fragmentation and literal-as-entity smells.

As a KB grows across sources, the same real-world thing can fragment into
several surface forms ('갑봇' / 'Samplebot'), and literal values (dates, numbers)
can leak in as entities. A plain notes wiki cannot see either. This reports,
deterministically and informationally (always exit 0):

  1. Entities — distinct engine-fact entities, each with its fact count and the
     statuses it appears under. Declared literals (objects of attribute
     relations, see policy/attribute-relations.md) are listed separately.
  2. Fragmentation — pairs of ENTITIES that may be the same thing: normalized-
     equal (spacing/punctuation/case only), substring-contained, or sharing a
     significant token. A heuristic — expect false positives; it surfaces
     candidates for human judgement, it does not merge anything.
  3. Literal suspects — objects that look like a literal (date / number /
     ordinal) under a relation NOT yet declared in attribute-relations.md;
     suggests declaring that relation (pairs with entity-vs-literal typing).

  4. Literal used as subject — a compound term in the subject position.
  5. Malformed typed literal — compound-term FORM the engine cannot parse.
  6. Conflicting typed declaration — one relation form claimed by two disagreeing
     typed-relations.md lines (its unit table is then unusable, so it is skipped).

A typed literal written as a COMPOUND TERM (`date(2020,3,8)`, `number(19)`) is a
literal by syntax alone, whatever the relation says, so it never counts as an
entity here — see `_compound_term_type`. It is still reported: as a declared
literal, a literal suspect, a literal-used-as-subject, or a malformed literal.
Excluding it from the entity set without reporting it anywhere would trade one
blind spot for another.

Usage:
    python3 entity_audit.py [--wiki <kb>]
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# Resolve the KB root and export it before importing common, which binds
# its module-level paths from FACTLOG_ROOT at import time.
import factlog_config  # noqa: E402

os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root_from_argv("--wiki")

from common import (  # noqa: E402
    CANDIDATES_CSV,
    attribute_relation_forms,
    is_attribute_relation,
    engine_facts,
    ensure_dirs,
    entity_set,
    load_facts,
    relation_aliases,
    surface_variants,
    typed_relations,
)
from factlog import literal_types  # noqa: E402

# Heuristic: looks like a literal VALUE rather than a first-class entity. Covers
# dates (2030.1 / 2024-07-01), plain/comma/decimal numbers (2026, 1,000, 3.14),
# and number+unit forms incl. an optional trailing word (1호, 1호 항목, 2026년,
# 100억, 제3호). Advisory only — a human confirms before declaring the relation;
# a few false positives (e.g. a named concept like '4차 산업혁명') are acceptable
# in exchange for not missing the motivating value forms.
_LITERAL_RE = re.compile(
    r"^\d{4}[.\-/]\d{1,2}([.\-/]\d{1,2})?$"                       # date
    r"|^\d[\d,]*(\.\d+)?$"                                        # number / comma / decimal
    r"|^제?\d+\s*(호|차|위|개|번|년|월|일|억|만|천|원|%)(\s+.+)?$"   # number + unit (+word)
)


# The compound-term wrapper text-to-fact.md mandates: `date(2020,3,8)`,
# `number(2.5)`, `ordinal(3)`, `amount(100,"억")`. The wrapper NAMES come from
# literal_types.TYPES — the single source of the notation — so this file never
# holds a second copy of the list to drift from.
#
# Deliberately TIGHT, because a match REMOVES the value from the entity listing:
# every widening here silently hides a real entity from the audit.
#   - case-sensitive: the mandated notation is lower-case, so `Date(Time)`,
#     `Amount(USD)` and `AMOUNT(Adjusted)` stay entities. A dataset column or an
#     institution may legitimately be named that — same reasoning as value-audit's
#     `ETC (electron transport chain)` (docs/reference/value-audit.md).
#   - `[^()\n]+`: a non-empty body with no nested or spanning parens, so `date()`
#     names nothing and `number(19) vs number(20)` is ONE entity, not a literal.
#   - `\A`/`\Z` without DOTALL: `date(2020)\n` does NOT match. A stored value
#     carrying a control character is not the mandated notation; flagging it as a
#     literal would hide it, and #373 wants such values visible, not swallowed.
# For the same reason nothing is stripped before matching: tolerating padding
# would re-admit `date(2020)\n` through the back door. Both predicates below judge
# the exact stored string, so neither is more permissive than the other.
_COMPOUND_TERM_RE = re.compile(
    r"\A(?P<type>" + "|".join(sorted(re.escape(t) for t in literal_types.TYPES)) + r")\([^()\n]+\)\Z"
)


def _compound_term_type(value: str) -> str | None:
    """The wrapper name if *value* is written in compound-term form, else None.

    Syntax settles it: nothing but a literal is spelled `date(...)`. So this does
    NOT consult attribute-relations.md. It cannot, and must not wait for it — a KB
    that follows the mandated notation but has not declared the relation yet was
    leaking every such value into the entity set, where `_tokens` split the wrapper
    off and made `date` a token shared by every date. All C(n,2) date pairs then
    surfaced as fragmentation candidates and buried the real ones (#386).
    """
    match = _COMPOUND_TERM_RE.match(value)
    return match.group("type") if match else None


def _is_compound_term(value: str) -> bool:
    return _compound_term_type(value) is not None


def _is_malformed_compound_term(value: str, spec: object | None = None) -> bool:
    """Wrapper-shaped, but the engine cannot parse it into a scalar.

    `date(abc)` and `date(2020,2,30)` wear the notation without being values. They
    must not be quietly filed as "a literal, nothing to see" — that is exactly the
    class of row a human needs to fix, so the audit names them separately.

    The parse question is delegated to literal_types (read-only), which owns the
    strict per-type notation; re-deciding it here would be a second definition to
    drift from. NOTE the coupling: `date(2020)` (year-only) does not parse TODAY,
    so it reports as malformed until #385 lands year-only date support — at which
    point it silently becomes well-formed here, with no change to this file.

    `amount` carries a UNIT, and which units exist is a per-KB declaration: a
    `typed-relations.md` line may attach an inline table (`(파운드=1700, 원=1)`).
    Judging it against literal_types' built-in table alone called `amount(5,"파운드")`
    malformed while the engine parsed it to 8500 — an advisory tool telling a human
    to fix correct data, which is worse than the noise this audit removes.

    So exactly ONE step is exempted, and only when the table is unreadable: UNIT
    RESOLUTION. An amount whose SHAPE already fails never reaches unit resolution,
    so no unit table could change the verdict and it is judged with or without a
    spec. Exempting the whole `amount` type instead (#394) silenced precisely the
    population this section exists for: a KB writing compound terms before declaring
    them.

    SHAPE here means the WHOLE of literal_types' `_AMOUNT_COMPOUND_RE`, not just its
    `num` group: a failed number (`amount(abc,"억")`, `amount(,"억")`), a missing
    unit argument (`amount(5)`) and a malformed one (`amount(5,)`) are all rejected
    before any unit lookup, so all of them are unit-table-independent alike. #394
    proposed splitting on the `num` group specifically; the whole-regex test is a
    superset with the same justification, and drawing the line at `num` would have
    left the arity failures exempt for no reason.

    The type is taken from the WRAPPER NAME, not from the relation's declared type,
    for EVERY type including `amount`. So `date(2020,1)` under a relation declared
    `number` reads as well-formed here even though the engine, which parses by the
    DECLARED type, would reject it. That mismatch belongs to a separate check
    (relation type vs value type) which does NOT exist yet — no caller performs it
    today, so the blind spot is real and merely out of scope here, not covered
    elsewhere. *spec* is consulted for one purpose only — to read
    a unit table — so a relation declared non-`amount` supplies no table and its
    amounts are treated exactly like the no-spec case: shape is still judged, unit
    resolution is not. That is a narrowing of the unit exemption, not a second
    typing rule.
    """
    type_tag = _compound_term_type(value)
    if type_tag is None:
        return False
    if type_tag == "amount":
        # Shape first: ANY whole-regex failure is unit-independent, so it is judged
        # before any spec/table question. Read-only use of literal_types' regex —
        # re-deriving the shape here would be a second definition to drift from.
        # (A shape-only public predicate in literal_types would be the cleaner
        # contract; that file is being changed by #388, so it is left untouched here
        # and this stays the single private reference to relocate later.)
        if literal_types._AMOUNT_COMPOUND_RE.match(value) is None:
            return True
        if getattr(spec, "type", None) != "amount":
            return False
        return literal_types.normalize(type_tag, value, getattr(spec, "units", None)) is None
    return literal_types.normalize(type_tag, value) is None


def _judged_fields(spec: object | None) -> tuple[object, object]:
    """The only parts of a TypedRelSpec that change a malformed verdict.

    Kept beside `_is_malformed_compound_term` because it must mirror it exactly:
    that function reads `type` and `units` and nothing else. `alias` names the
    engine side-relation and never reaches a verdict, so two declarations differing
    only there agree as far as this audit is concerned — and since common.py forbids
    a duplicate alias, comparing it would make EVERY pair of lines differ (#393).

    `units` is a dict, so this tuple is compared with `==`, never hashed.
    """
    return getattr(spec, "type", None), getattr(spec, "units", None)


def _typed_spec_by_form() -> tuple[dict[str, object], dict[str, list[str]]]:
    """Every SURFACE form naming a typed relation → its TypedRelSpec, plus the forms
    two or more declarations disagree about.

    Same alias/NFC expansion as `attribute_relation_forms`: a KB that declares the
    canonical while its facts carry an alias must still find the declaration, or the
    `amount` unit table silently goes missing and every non-default unit reads as
    malformed.

    That expansion makes forms COLLIDE. Declaring a canonical and its alias on
    separate lines with different unit tables made both lines expand onto the same
    forms, and the last one written silently won — so an amount written under the
    CANONICAL relation was judged against the ALIAS line's table and reported
    malformed while the engine parsed it fine (#393). A last-writer-wins map cannot
    be right here: the two declarations are contradictory, not ordered.

    So a contested form is dropped from the mapping entirely — the same treatment as
    an undeclared relation, i.e. its amounts are not judged (a miss beats a false
    accusation) — and returned separately so `audit` can say out loud that the KB
    contradicts itself. Dropping alone would fix the false accusation while leaving
    the author with no way to learn why their table stopped applying.

    "Disagree" is decided on the fields this audit actually CONSUMES — `type` and
    `units`, the only two `_is_malformed_compound_term` ever reads. Comparing whole
    TypedRelSpecs made the carve-out below unreachable: `alias` is a field, and
    common.py rejects a duplicate alias outright (`duplicate alias ...`), so two
    distinct lines can never be `==`. Every canonical/alias pair would then be
    reported as a self-contradiction and lose its table — including pairs declaring
    the SAME unit table, which main judged correctly. That is a new false positive
    in a fix whose whole point is removing one, so the comparison must ignore the
    fields nothing downstream reads.

    Two lines agreeing on `type` and `units` are therefore not a conflict: nothing
    the audit consumes is contested, so the form keeps its table.

    A contested form reports EVERY line that claims it, not just the two that first
    disagreed. Recording only the disagreeing pair dropped a claimant whenever two
    lines agreed before a third differed (the agreeing ones overwrite each other as
    "the" declarer), so the report named 2 of 3 lines and sent the author to fix the
    wrong ones. Claimants keep DECLARATION order — the order the author reads them
    in typed-relations.md — which is deterministic (`typed_relations` preserves file
    order) and more useful than an alphabetical one.
    """
    specs = typed_relations()
    if not specs:
        return {}, {}
    aliases = relation_aliases()
    by_form: dict[str, object] = {}
    # Every name claiming a form, in declaration order — the claimant list is built
    # for all forms, not only contested ones, because a form becomes contested after
    # some of its claimants have already been seen.
    claimants: dict[str, list[str]] = defaultdict(list)
    contested: set[str] = set()
    for name, spec in specs.items():
        nfc_name = unicodedata.normalize("NFC", name)
        canon = aliases.get(nfc_name, nfc_name)
        # The expansion is a set; sort it so the ITERATION is reproducible. Report
        # order does not depend on this (the return statement orders both levels),
        # and the forms of one spec are distinct, so this is defensive only.
        for form in sorted({nfc_name, canon} | surface_variants(canon, aliases)):
            if nfc_name not in claimants[form]:
                claimants[form].append(nfc_name)
            if form in contested:
                # Stays out of by_form no matter what a later line declares.
                continue
            previous = by_form.get(form)
            if previous is not None and _judged_fields(previous) != _judged_fields(spec):
                contested.add(form)
                del by_form[form]
                continue
            by_form[form] = spec
    return by_form, {form: claimants[form] for form in sorted(contested)}


def _looks_literal(value: str) -> bool:
    """Literal by compound-term syntax OR by the prose heuristic."""
    return _is_compound_term(value) or bool(_LITERAL_RE.match(value))


def _norm(s: str) -> str:
    return re.sub(r"[\s·_\-/().,]+", "", s).lower()


def _tokens(s: str) -> set[str]:
    return {t for t in re.split(r"[\s·_\-/().,]+", s) if len(t) >= 2}


def audit(facts: list[dict[str, str]]) -> dict[str, object]:
    rows = engine_facts(facts)
    # Surface forms via the shared predicate: comparing raw declarations made this
    # advise declaring a relation that WAS already declared, just under its alias.
    literal_rels = attribute_relation_forms()
    # Read once: the `amount` unit table a relation declares (see
    # _is_malformed_compound_term). Forms two declarations disagree about carry no
    # usable table and are reported instead (#393).
    typed_by_form, typed_form_conflicts = _typed_spec_by_form()
    # Excludes declared-literal objects; compound terms go too, since their form
    # already proves they are values and pairing them is pure noise (#386).
    entities = {e for e in entity_set(facts) if not _is_compound_term(e)}

    fact_count: Counter[str] = Counter()
    statuses: dict[str, set[str]] = defaultdict(set)
    declared_literals: set[str] = set()
    literal_suspects: dict[str, set[str]] = defaultdict(set)  # relation -> {objects}
    literal_subjects: set[str] = set()
    malformed_literals: set[str] = set()

    for row in rows:
        s, rel, o, st = row["subject"], row["relation"], row["object"], row["status"]
        for ent in (s, o):
            if ent:
                fact_count[ent] += 1
                statuses[ent].add(st)
        # A compound term in the SUBJECT position is reported on its own. Dropping
        # it from `entities` without this would make it vanish from every section
        # (declared_literals and literal_suspects only ever look at objects), and a
        # literal leaking into the subject slot is precisely a smell this tool exists
        # to show — losing it would be a regression in observability.
        if s and _is_compound_term(s):
            literal_subjects.add(s)
        # Only the OBJECT stands under this relation's declaration, so only the
        # object may borrow its unit table; a subject-position amount is judged
        # with no spec, i.e. not judged at all.
        spec = typed_by_form.get(unicodedata.normalize("NFC", rel)) if rel else None
        if s and _is_malformed_compound_term(s):
            malformed_literals.add(s)
        if o and _is_malformed_compound_term(o, spec):
            malformed_literals.add(o)
        if o and is_attribute_relation(rel, literal_rels):
            declared_literals.add(o)
        elif o and not is_attribute_relation(rel, literal_rels) and _looks_literal(o):
            literal_suspects[rel].add(o)

    # Fragmentation clusters among entities only. Precompute norm/tokens once per
    # entity (the pairing is O(n^2); don't re-normalise inside the inner loop).
    ents = sorted(entities)
    norm = {e: _norm(e) for e in ents}
    toks = {e: _tokens(e) for e in ents}
    clusters: list[tuple[str, str, str]] = []
    for i, a in enumerate(ents):
        for b in ents[i + 1:]:
            na, nb = norm[a], norm[b]
            shared = toks[a] & toks[b]
            if na == nb:
                clusters.append((a, b, "normalized-equal (spacing/punct/case only)"))
            elif na and (na in nb or nb in na):
                clusters.append((a, b, "substring-contained"))
            elif shared:
                clusters.append((a, b, f"shared token {sorted(shared)}"))

    return {
        "entities": sorted(entities),
        "declared_literals": sorted(declared_literals),
        "fact_count": fact_count,
        "statuses": statuses,
        "clusters": clusters,
        "literal_suspects": literal_suspects,
        "literal_subjects": sorted(literal_subjects),
        "malformed_literals": sorted(malformed_literals),
        "typed_form_conflicts": typed_form_conflicts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit entities for fragmentation / literal leakage.")
    parser.add_argument("--wiki", default=os.environ.get("FACTLOG_ROOT", "."), help="KB root")
    parser.parse_args(argv)

    ensure_dirs()
    facts = load_facts() if CANDIDATES_CSV.is_file() else []
    if not facts:
        print("entity_audit: no candidate facts")
        return 0

    a = audit(facts)
    ents = a["entities"]
    fc, st = a["fact_count"], a["statuses"]
    print(
        f"entity_audit: {len(ents)} entit(y/ies), {len(a['declared_literals'])} declared literal(s), "
        f"{len(a['clusters'])} fragmentation candidate(s), "
        f"{sum(len(v) for v in a['literal_suspects'].values())} literal suspect(s), "
        # Counted here too: a finding that only ever appears in a stderr section is
        # invisible to a human scanning the summary, and to a wrapper script reading
        # this one line. Every section the audit can print is countable here.
        f"{len(a['literal_subjects'])} literal subject(s), "
        f"{len(a['malformed_literals'])} malformed literal(s), "
        f"{len(a['typed_form_conflicts'])} conflicting typed declaration(s)"
    )

    print("\nentities (fact count, statuses):")
    for e in ents:
        print(f"  [{fc[e]:>2}] {e}  ({'/'.join(sorted(st[e]))})")
    if a["declared_literals"]:
        print("\ndeclared literals (attribute-relation objects, not entities):")
        for v in a["declared_literals"]:
            print(f"  [{fc[v]:>2}] {v}  ({'/'.join(sorted(st[v]))})")

    if a["clusters"]:
        print("\nfragmentation candidates (HEURISTIC — expect false positives; human judgement):", file=sys.stderr)
        for x, y, why in a["clusters"]:
            print(f"  • '{x}' ⟷ '{y}' — {why}", file=sys.stderr)

    if a["literal_suspects"]:
        print("\nliteral suspects (object looks literal under an undeclared relation):", file=sys.stderr)
        for rel in sorted(a["literal_suspects"]):
            vals = ", ".join(sorted(a["literal_suspects"][rel]))
            print(f"  • relation '{rel}' has literal-looking object(s): {vals}", file=sys.stderr)
            print(f"      → consider adding '{rel}' to policy/attribute-relations.md", file=sys.stderr)

    if a["literal_subjects"]:
        print("\nliteral used as subject (a typed value in the subject position):", file=sys.stderr)
        for v in a["literal_subjects"]:
            print(f"  • '{v}' ({fc[v]} fact(s)) — a value cannot be the thing a fact is about", file=sys.stderr)

    if a["malformed_literals"]:
        print("\nmalformed typed literal (compound-term form the engine cannot parse):", file=sys.stderr)
        for v in a["malformed_literals"]:
            print(f"  • '{v}' — not a value any typed relation can order or compare", file=sys.stderr)

    if a["typed_form_conflicts"]:
        # The parser accepts these declarations (exit 0), so without this section a
        # KB author is never told the two lines contradict each other — they only
        # see amounts stop being judged, with no cause (#393).
        print("\nconflicting typed declaration (one relation form, two declarations):", file=sys.stderr)
        for form, names in a["typed_form_conflicts"].items():
            claimants = ", ".join(f"'{n}'" for n in names)
            print(f"  • '{form}' is declared by {claimants} — amounts under it are left unjudged", file=sys.stderr)
            print("      → keep ONE typed-relations.md line per relation (aliases share the canonical's)", file=sys.stderr)

    return 0


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
