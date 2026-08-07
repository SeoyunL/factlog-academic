#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Detect contradictions among engine-input facts.

A relation declared *single-valued* (functional) in policy/single-valued.md may
hold at most one object per subject. If two distinct objects are asserted for the
same (subject, relation) among engine-input facts (status confirmed/accepted;
'superseded' rows are ignored), that is a contradiction — the kind of silent rot
a plain notes wiki accumulates. This surfaces it deterministically.

Resolution is human-in-the-loop and non-destructive: mark the outdated row's
status as 'superseded' in facts/candidates.csv (it stays for audit, drops out of
engine input, and the conflict clears). A value carrying non-ASCII digits is the
exception: it does not parse, so supersession can clear the gate while keeping the
unreadable value (see non_ascii_digit_note).

Exit code: 0 if no conflicts, 1 if any conflict is found.

Usage:
    python3 check_conflicts.py [--wiki <kb>]
"""

from __future__ import annotations

import argparse
import os
import sys
import unicodedata
from pathlib import Path
from typing import NamedTuple

_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# Resolve the KB root and export it before importing common, which binds
# its module-level paths from FACTLOG_ROOT at import time.
import factlog_config  # noqa: E402

os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root_from_argv("--wiki")

import literal_types  # noqa: E402
from common import (  # noqa: E402
    TypedRelSpec,
    composed_spelling,
    engine_facts,
    ensure_dirs,
    fold_relation_name,
    folded_relation_names,
    load_facts,
    normalization_form,
    relation_aliases,
    single_valued_relations,
    typed_relations,
)


def _canonicalize(relation: str, aliases: dict[str, str]) -> str:
    """Return the canonical relation name when *relation* participates in the
    alias map; otherwise return *relation* verbatim (NFD-preserving).

    Participation mirrors ``common.canonical_atoms``:

    * relation is an alias **key** (raw predicate) → ``aliases[NFC(relation)]``
    * relation **is** a canonical value (stored literally) → its NFC form
    * relation is not in the alias map → verbatim (no normalization)

    When *aliases* is empty the function short-circuits and returns *relation*
    unchanged, preserving byte-identical behaviour for KBs without a
    relation-aliases.md file.
    """
    if not aliases:
        return relation
    rn = unicodedata.normalize("NFC", relation)
    if rn in aliases:
        return aliases[rn]
    if rn in set(aliases.values()):
        return rn
    return relation


def _fold(value: str) -> str:
    """Return *value* under Unicode canonical composition (NFC).

    NFC only — **not** NFKC and **not** casefold. NFC merges strings that are
    *canonically equivalent*: the same abstract characters written with
    precomposed vs decomposed code points. macOS filesystems and IMEs routinely
    emit Hangul decomposed, so a KB assembled from mixed sources naturally
    collects both spellings of one string. It does **not** merge compatibility
    variants (fullwidth ``ＡＢＣ`` stays distinct from ``ABC``) nor case — those
    are genuinely different values and must keep firing a conflict.

    Precomposed-vs-decomposed is the case that matters here, but not the only one
    NFC folds: it also collapses the *canonical singletons* Unicode has
    deprecated in favour of an existing character — ``Ω`` U+2126 → U+03A9, ``K``
    U+212A → U+004B, ``Å`` U+212B → U+00C5. Merging them is correct (Unicode
    itself defines them as the same character), and it is why the report says
    "canonically equivalent" rather than "renders identically": equivalence is
    guaranteed by the standard, identical rendering is only a font's habit.

    ``common._canonical_value`` is deliberately *not* reused: it layers
    amount-quote normalization on top of the Unicode fold, which would perturb
    the typed-literal key space this module builds through ``literal_types``.

    One operation, three call sites, so: this is the *grouping* fold, applied to
    subjects and objects inside this module. ``common.fold_relation_name`` is the
    same NFC applied to a policy relation name for a *membership* test, and every
    consumer that tests membership must use it (see its docstring).
    ``common.composed_spelling`` picks which raw spelling of a folded group to
    display. Nothing else should re-derive any of the three.
    """
    return unicodedata.normalize("NFC", value)


def _representative(raws: set[str]) -> str:
    """Return the string reported on behalf of a folded group of *raws*.

    Deterministic, and always one of the strings as written (provenance). Where
    the group holds several normalization forms, the **composed (NFC)** spelling
    wins; ties break lexicographically.

    Plain ``min`` would be deterministic too, but it picks the wrong member in
    practice: Hangul conjoining jamo (U+1100…) sort below precomposed syllables
    (U+AC00…), so ``min`` on a mixed group *always* returns the decomposed form —
    the one that will not match if the reader types or pastes the name into a
    search from an NFC editor. Preferring NFC makes the reported string the one
    most likely to grep, and the full spelling list is printed alongside it for
    the rows it cannot reach.

    The grep argument only holds where the group *has* a composed member. On a
    uniformly decomposed KB every candidate is NFD and this returns NFD — still
    deterministic and still a spelling actually written (which is the guarantee
    that matters), but no more greppable than any other member. The labelled
    spelling list is what carries the reader there in that case.

    Shared with ``corroboration`` through ``common.composed_spelling``: both
    modules stand a representative in front of a folded group and must pick the
    same one, or the two reports name different rows for one value.
    """
    return composed_spelling(raws)


def _form_label(value: str) -> str:
    """Name the Unicode normalization form *value* is written in.

    ``common.normalization_form``, not a local copy: ``factlog vocab`` labels the
    same forms for the same reason (two names that render identically), and a
    report that disagrees with another about which form a string is in is worse
    than one that says nothing. See that docstring for what ``"mixed"`` covers.
    """
    return normalization_form(value)


def _spellings(raws: list[str]) -> str:
    """Render *raws* as an escaped, form-labelled list for the report.

    Escaped because the whole difficulty of this conflict class is that the
    strings are indistinguishable on screen, so the code points are the only way
    to tell them apart; labelled because the code points alone do not say which
    form to unify *to*. The answer is on the CONFLICT line above —
    ``_representative`` already picked the NFC spelling — and the label is what
    connects the two.
    """
    return ", ".join(f"{ascii(s)} ({_form_label(s)})" for s in raws)


def _fold_classes(raws: list[str]) -> list[list[str]]:
    """Partition *raws* into canonical-equivalence classes, keeping merged ones.

    Returns one sorted list per class that holds more than one spelling, sorted
    between classes; an empty result means no Unicode merge happened here. This
    is *the* answer to both "did folding merge anything" and "which strings did
    it merge" — the gate and the payload have to be one computation, because a
    value group is keyed on the typed scalar (#116) and can hold strings that are
    not canonically equivalent at all. ``amount(5400,"억")`` and
    ``amount(0.54,"조")`` share a group by parsing to 5.4e11, ``제3호`` and ``3위``
    by ordinal rank (#218). Reporting the whole group calls those "canonically
    equivalent" and asks the reader to unify notations #116 exists to keep apart.

    Two failure modes this avoids, both worse than the bug:

    * **Do not drop the non-equivalent members from the group itself.** They are
      the same value and must keep collapsing, or #116's false CONFLICT returns.
      The narrowing is for the *report*, never for the grouping.
    * **Do not keep only the members where ``_fold(r) != r``.** That drops the
      composed twin — precisely the spelling the reader must unify *to*, and the
      one ``_representative`` already put on the CONFLICT line.

    One class per line matters too: a single group can hold several independent
    classes (NFC/NFD of ``amount(5400,"억")`` *and* NFC/NFD of
    ``amount(0.54,"조")`` all key to 5.4e11), and "canonically equivalent" is
    true within a class but false across them.
    """
    classes: dict[str, list[str]] = {}
    for raw in raws:
        classes.setdefault(_fold(raw), []).append(raw)
    return [sorted(members) for _, members in sorted(classes.items()) if len(members) > 1]


def _parse_merge(raws: set[str], unfolded_keys: dict[str, tuple]) -> list[str]:
    """Return the notations folding merged that nothing else explains, or ``[]``.

    ``_fold_classes`` reports the merges Unicode equivalence explains. This
    reports the ones it does not: a typed literal that ``literal_types.normalize``
    parses **only after** the fold joins a group it could never have reached
    unfolded — ``NFD('제3호')`` degrades to ``("raw", …)`` as written and keys as
    ordinal rank 3 once folded, meeting ``'3위'``. Calling those two "canonically
    equivalent" would be false, so they need their own sentence, and without one
    the fold silently resolves a contradiction the previous release reported.

    The test is *not* "the group spans several equivalence classes" — a #116
    scalar merge does that with no decomposed code point in sight
    (``amount(5400,"억")`` and ``amount(0.54,"조")`` both key to 5.4e11 with or
    without the fold), and announcing it as a Unicode merge is the exact false
    diagnostic ``_fold_classes`` exists to avoid. Nor is it "folding changed the
    partition", which fires on ``{NFC('제3호'), NFD('제3호'), '3위'}``: there the
    fold joined ``NFD('제3호')`` to a class ``_fold_classes`` already names, and
    the rank equivalence to ``'3위'`` was #218's doing, not the fold's.

    So both explanations get to speak first. Two raws are joined when they share
    an **unfolded** group key (they were one value before the fold — #116/#218
    equivalence) or when they are **canonically equivalent** (the plain Unicode
    merge, already listed by ``_fold_classes``). Whatever components survive that
    were joined by nothing but the fold-enabled parse, and only those are
    reported — one representative each (NFC spelling preferred, see
    ``_representative``), since the members inside a component are covered above.
    """
    parent = {raw: raw for raw in raws}

    def find(raw: str) -> str:
        while parent[raw] != raw:
            parent[raw] = parent[parent[raw]]
            raw = parent[raw]
        return raw

    seen: dict[tuple, str] = {}
    for raw in sorted(raws):
        for label in (("key", unfolded_keys[raw]), ("fold", _fold(raw))):
            other = seen.setdefault(label, raw)
            roots = sorted((find(raw), find(other)))
            parent[roots[1]] = roots[0]
    components: dict[str, set[str]] = {}
    for raw in raws:
        components.setdefault(find(raw), set()).add(raw)
    if len(components) <= 1:
        return []
    return sorted(_representative(members) for members in components.values())


def _variant_map(groups: dict[tuple, set[str]]) -> dict[str, list[str]]:
    """Return ``{reported object: sorted raw objects}`` for *groups*, key-sorted.

    Sorted rather than insertion-ordered. ``main`` reads this through the sorted
    ``conflicts`` list and each key's sorted object list, so the report never
    depended on it — but *groups* is built by iterating sets, so insertion order
    tracks row order, and this is a public return value of a module whose entire
    contract is determinism. Leaving row order in it is a trap for the next
    caller, not a defect in this one.

    Group representatives are distinct by construction (each raw object falls in
    exactly one group, so the groups' raw sets are disjoint), hence sorting on
    the key alone is a total order.
    """
    return dict(
        sorted(
            ((_representative(raws), sorted(raws)) for raws in groups.values()),
            key=lambda item: item[0],
        )
    )


def _group_key(obj: str, spec: TypedRelSpec | None) -> tuple:
    """Return the equivalence key an *object* string is grouped under.

    For a relation declared typed (#116), the object's canonical scalar
    (``literal_types.normalize``) is the key, so equivalent notations of the same
    value (e.g. ``amount(5400,"억")`` and ``amount(0.54,"조")`` -> 5.4e11) collapse
    to one value instead of firing a false CONFLICT. ``amount`` needs its unit
    table, so ``spec.units`` is passed through.

    Falls back to the raw object string when the relation is untyped OR the value
    does not parse (normalize -> None): backward-compatible, lossless degrade. The
    two key spaces are tagged (``"scalar"`` vs ``"raw"``) so a scalar never
    collides with an unrelated raw string. Total: never raises (normalize is
    total).

    **ordinal unit loss (#218 / #224 A):** ``normalize("ordinal", …)`` keeps only
    the integer *rank* — the ordinal-class unit (호/위/번/차/등/째) is dropped at
    parse time (``literal_types.parse_ordinal``), so it never enters the key. A
    cross-unit pair therefore collapses onto one scalar: ``제3호`` and ``3위`` both
    key as ``("scalar", 3)``. This is **by design** and consistent with the engine,
    which likewise compares ordinals on rank alone (``_TYPED_COL["ordinal"]`` is a
    bare int64, no unit column). ordinal is a *rank-only* contract: same rank =
    same value. If two notations denote genuinely different domains (a rank vs a
    house number), that distinction belongs in the model — declare them as
    **separate relations**, not one single-valued ordinal relation. (Contrast
    ``amount``, where 억↔조 equivalence is the intended collapse.)

    **int64 divergence note (#224 C):** ``normalize`` can return a scalar wider
    than int64 (mainly ``number`` via ``parse_number_scaled``, and unbounded
    ``ordinal`` ranks — both lack a range guard; ``amount`` already degrades to raw
    when ``parse_amount`` overflows, #205). The engine, by contrast, **skips insertion** of an out-of-int64-range
    scalar (see ``insert_typed_facts`` in ``common.py`` ~ the ``-(2**63) <= scalar
    < 2**63`` guard). So this checker may group under a scalar the engine would
    drop. That affects **grouping only** (never insertion) and is harmless: the
    checker is strictly more willing to merge equivalents, never less. No behaviour
    change here — note only.

    **Unicode folding (#325):** *obj* is NFC-folded **once, on entry**, and the
    folded string feeds *both* the typed ``normalize`` call and the raw fallback.
    Folding before ``normalize`` is required, not cosmetic. An NFD-authored typed
    literal fails to parse — ``normalize("amount", NFD('amount(5400,"억")'), units)``
    and ``normalize("ordinal", NFD("제3호"), None)`` both return ``None`` — so it
    degrades to ``("raw", …)`` while its NFC twin keys as ``("scalar", …)``. The
    two tags never meet, and folding only the fallback would not reach them.
    Typed relations are in fact the *more* exposed axis, since amount and ordinal
    units are Hangul (억/조/호/위/번/차). The ``scalar``/``raw`` tag split itself is
    correct and unchanged.

    Consequence, stated plainly: an **all-NFD typed KB can change output**. Two
    NFD rows that previously degraded to distinct raw strings now parse and may
    collapse onto one scalar. That is not a regression — it is #116's cross-
    notation equivalence (억↔조) starting to work on that KB for the first time.
    The invariant held here is the one the issue asks for: an **NFC-only** KB is
    byte-identical.

    That the change is merge-only is **not** free, and it is worth naming the way
    it was nearly lost. Folding here also folds the string handed to
    ``parse_amount``, which resolves a Hangul unit (억/조/원) against a table
    ``policy/typed-relations.md`` supplies. While that table kept the policy
    file's own spelling, an NFD units clause plus a folded object was a *miss*:
    the amount degraded to ``("raw", …)``, and ``5400억`` and ``0.54조`` — one
    value, 5.4e11, which parsed and merged **unfolded** — split into a CONFLICT
    this checker did not previously report, on the very macOS-decomposed KB
    #325 exists for. Folding can add a conflict as easily as it removes one when
    only one end of a lookup folds.

    So the merge-only direction rests on a stated dependency: every table this
    key parses against is composed on both ends (``common._parse_amount_units``
    stores NFC keys, ``parse_amount`` composes the lookup key). Given that, no
    parser matches a decomposed string its composed form fails — the ordinal and
    amount markers are all spelled composed in their regexes — so folding can
    only join groups, never split one. A future typed parser that resolves a
    non-ASCII token against a table has to fold that table too, or it reopens
    exactly this hole."""
    return _typed_key(_fold(obj), spec)


def _typed_key(value: str, spec: TypedRelSpec | None) -> tuple:
    """Tag *value* as a typed scalar when *spec* parses it, else as a raw string."""
    if spec is not None:
        scalar = literal_types.normalize(spec.type, value, spec.units)
        if scalar is not None:
            return ("scalar", scalar)
    return ("raw", value)


def _group_key_unfolded(obj: str, spec: TypedRelSpec | None) -> tuple:
    """Return the key *obj* would group under if this module did not fold (#325).

    Used only by the disclosure, to answer one question ``_fold_classes`` cannot:
    **did folding change how these rows partition?** Canonical equivalence is the
    right axis for the untyped fallback and the wrong one for a typed relation,
    because folding also decides whether ``literal_types.normalize`` parses at
    all. ``NFD('제3호')`` does not parse and keys ``("raw", …)``; folded it parses
    to ordinal rank 3 and meets ``'3위'`` under ``("scalar", 3)``. Those two
    strings are *not* canonically equivalent, so the fold is what merged them and
    nothing on the equivalence axis can say so — which is how a pair that main
    reported as a CONFLICT became a silent exit 0.

    Two rows sharing this key were already one value before the fold, so
    ``_parse_merge`` uses it to subtract the merges #116/#218 equivalence
    accounts for on its own.
    """
    return _typed_key(obj, spec)


class ConflictScan(NamedTuple):
    """Everything one pass over the facts learned about a single-valued KB.

    A NamedTuple rather than a plain tuple because the channels are no longer two
    symmetric ones: folding merges strings on several axes and each merge has a
    different consequence, so the caller has to be able to name the one it is
    reporting. ``detect_conflicts`` still reads ``.conflicts`` and nothing else.

    * *conflicts* — ``{(reported subject, canonical relation): sorted objects}``,
      the pairs holding more than one distinct value. The report's exit-1 payload.
    * *subject_variants* — for each conflicting key, the sorted raw subject
      spellings the fold merged under it. Conflicting pairs only.
    * *object_variants* — ``{reported object: sorted raw objects}`` per key. Its
      key set is a **superset** of *conflicts*: a pair whose objects folding
      collapsed to a single value is not a conflict, yet is retained so the
      caller can disclose that the fold is what resolved it.
    * *parse_merges* — the subset of *object_variants* where folding merged
      values that neither #116/#218 equivalence nor canonical equivalence
      explains, by making a typed literal parse (see ``_parse_merge``). Same
      shape, but the value list is one representative per merged component,
      because "these are the same string written two ways" is false here and the
      reader needs the other sentence — including the part where the engine
      cannot reproduce the merge at all.
    * *relation_variants* — ``{(reported subject, NFC relation): sorted raw
      relation spellings}`` wherever one folded relation was written more than
      one way for a subject. Membership folds but grouping does not, so those
      rows sit in **separate** pairs: a contradiction between them is invisible
      to this module, and at exit 0 nothing else in the run mentions the rows at
      all. Keyed on all pairs, not only conflicting ones, for that reason.
    """

    conflicts: dict[tuple[str, str], list[str]]
    subject_variants: dict[tuple[str, str], list[str]]
    object_variants: dict[tuple[str, str], dict[str, list[str]]]
    parse_merges: dict[tuple[str, str], dict[str, list[str]]]
    relation_variants: dict[tuple[str, str], list[str]]


def collect_conflicts(
    facts: list[dict[str, str]],
    single_valued: set[str],
    typed: dict[str, TypedRelSpec] | None = None,
    aliases: dict[str, str] | None = None,
) -> ConflictScan:
    """Return a :class:`ConflictScan` — the conflicts plus every spelling channel.

    ``scan.conflicts`` is exactly what ``detect_conflicts`` returns (see there).
    The remaining fields are the disclosure channels; each is documented on the
    NamedTuple. They exist so ``main`` can report what folding did without
    duplicating the grouping logic, while ``detect_conflicts`` keeps its
    established return shape (a large body of pinned tests asserts that dict
    directly).

    A variant list holds one entry when nothing was merged. More than one means
    the KB writes that string in several Unicode normalization forms, so the
    reported representative does not grep to every row behind it — which is
    exactly what the caller has to disclose.

    **Subject axis folding (#325):** rows group under the NFC fold of the
    subject, so a contradiction written with the subject NFC on one row and NFD
    on another is detected instead of splitting into two singleton groups. That
    split was an *unsound* false negative: the finalize gate passed KBs that do
    contain a contradiction, and nobody ever saw it. Conversely the untyped
    object axis folds too, so two objects that render identically stop being
    reported as a contradiction the reader cannot act on.

    The *reported* key preserves provenance: a raw subject actually seen for that
    (folded subject, canonical relation) pair, chosen by ``_representative`` (NFC
    spelling preferred, ties lexicographic — not a plain ``min``, see there). The
    map is keyed per **pair**, never per folded subject globally — a global map
    would rewrite the reported subject spelling of unrelated relations and break
    byte-identity on inputs where folding merges nothing.

    **Relation axis (#325):** it is two mechanisms, and only *grouping* is
    deferred. Membership — whether a relation is declared single-valued at all —
    is folded above, because it gates entry to the loop and
    ``common._relation_names_from`` does not normalize the names it reads from
    policy/single-valued.md. Left raw it was a byte comparison between two
    hand-written files, so a KB written uniformly in NFD never reached the check
    and exited 0 with a contradiction in it. That is a *wider* false negative
    than the mixed-subject one above — it needs no mixed spelling at all, just
    one consistently decomposed KB, which is the scenario ``_fold`` itself cites.
    Grouping stays verbatim: two spellings of one relation remain two groups, and
    the reported relation is byte-for-byte as written. Folding that as well is
    mechanically possible and the #210 pins survive it, so "the pins forbid it"
    would be a false reason; the real reason is that it changes which rows
    collide, and how far #210's "no silent NFC coercion for non-participating
    relations" was meant to reach is a maintainer's call. Raised as a follow-up.

    **Engine agreement, and exactly how far it reaches (#342).**
    ``common.dedup_engine_atoms`` keys on ``common.engine_atom_key``, which
    applies the same NFC as ``_fold`` to the subject and the object. It is not
    the same *fold* on the subject, and the difference is the whole of what
    survives: ``engine_atom_key`` folds each value **inside its own triple**,
    while ``_group_key`` folds the subject **across rows**, into a
    ``(folded subject, relation)`` bucket that then collects every value written
    for it. Two rows agreeing on the folded subject and differing on the object
    are one group here and two atoms there, by construction.

    So the two axes did not both close:

    * **subject axis — checker still stricter, present tense.** Measured:
      ``NFC(김철수) 소속 A사`` and ``NFD(김철수) 소속 B사`` give **2** engine atoms
      with 2 subject spellings, while this module reports **1** conflict on
      ``('김철수', '소속')``. The gate still closes on a KB the engine would have
      accepted — nothing slips through, so the direction is the safe one, but the
      divergence is live and #342 did not touch it. Behaviour here is identical
      to before #342.
    * **object axis — checker more permissive, and this one closed.** Two objects
      the raw grouping called a contradiction now agree, so the gate *opens*
      where it used to close, ``finalize`` compiles — and both spellings then
      reached ``accepted.dl`` as separate atoms, the inflated duplicate count
      ``dedup_engine_atoms`` exists to prevent, arrived at through the normal
      path. That is what #342 closed: one atom, so the engine now reproduces the
      merge the checker made.

    The permissive direction is semantically right: ``common._canonical_value``
    (#213) already fixed NFC as value equality. What is not acceptable is doing
    it silently, so the object channel below survives for a pair folding
    resolved, and ``_report_resolved_merges`` discloses it at exit 0 — the merge
    is still a merge the author did not ask for, even now that the engine
    reproduces it.

    **The relation axis is NOT untouched — do not read #342 as leaving it
    clean.** Grouping here is verbatim and ``engine_atom_key`` leaves the
    relation raw, so *those two* split two spellings of one relation the same
    way. That agreement is only between this module and ``relation/3``. Two
    places downstream already fold the relation, and they did before #342:

    * ``common.canonical_atoms`` NFC-folds the relation before the alias lookup,
      so an aliased pair emits **two** ``relation/3`` atoms and **one**
      ``canonical/3`` atom into the same ``accepted.dl`` — measured.
    * ``common._canonical_value`` (#213) folds the relation argument of a query,
      so one spelling typed by the user matches both atoms.

    Whoever judges #210/#345 needs that: the axis is already folded at the query
    layer and in the canonical block, and the choice is not "start folding" but
    "make the rest agree with what those two already do". Folding it in
    ``engine_atom_key`` remains deferred, and deliberately so — it changes which
    rows collide — but the premise "both sides raw, nothing diverges" is false.

    **The typed projection still does not fold (#325 follow-up).** ``_group_key``
    folds *before* ``literal_types.normalize``; the engine's counterpart,
    ``common._project_typed_relations``, hands ``normalize`` the object of the
    atom as written (and looks its spec up under the raw relation name). So an
    NFD-authored typed literal can still parse here and nowhere else, and this
    module can declare two values equal on grounds the engine cannot reproduce.

    #342 narrows this without closing it. Two *canonically equivalent* spellings
    are now one atom, written in the composed spelling wherever the KB authored
    one, so they no longer land on opposite sides of the typed table — the case
    that used to insert the composed literal typed and degrade the decomposed one
    with a warning. What survives is everything the atom fold does not reach: a
    uniformly decomposed KB keeps its decomposed spelling (there is no composed
    member to prefer), so ``normalize`` still fails on it, and with the relation
    name decomposed the spec lookup still misses every row and none load typed at
    all. A *parse* merge is untouched by construction — ``NFD('제3호')`` and
    ``'3위'`` are not canonically equivalent, so they are two atoms here and two
    atoms in the engine, and only this module ever calls them one value.

    Aligning the typed projection too is the root fix and a follow-up of its
    own: it changes which rows enter the typed side-relations, hence what
    ``factlog ask`` answers, and it needs the deferred #210 relation-axis call
    decided first. What belongs *here* is that the checker never merges on that
    basis in silence — see ``_parse_merge`` and ``_report_resolved_merges``.
    """
    typed = typed or {}
    aliases = aliases or {}
    # Precompute the set of canonical single-valued relation names so the
    # per-row membership test is O(1). Folded on both sides: this test is the
    # only thing standing between a row and the grouping loop, and
    # ``common._relation_names_from`` does not normalize the names it parses out
    # of policy/single-valued.md, so comparing raw would make it a byte
    # comparison between two hand-written files. Membership only — the folded
    # name is never used as a grouping key (see the loop below). Shared with the
    # other membership consumers (``factlog status``/``vocab``, ``corroboration``)
    # through ``common``, so "which predicate decides membership" has one answer.
    sv = folded_relation_names(_canonicalize(r, aliases) for r in single_valued)
    # (folded subject, canonical_relation) -> group key -> set of raw objects.
    by_key: dict[tuple[str, str], dict[tuple, set[str]]] = {}
    # Same pair -> raw object -> the key that object would have had unfolded.
    # Per raw object rather than per group: the disclosure has to know *which*
    # rows the fold pulled together, not just that it pulled (_parse_merge).
    unfolded: dict[tuple[str, str], dict[str, tuple]] = {}
    # Same pair -> set of raw subject spellings folded into it.
    raw_subjects: dict[tuple[str, str], set[str]] = {}
    # (folded subject, folded relation) -> raw relation spellings seen, and the
    # raw subjects under them. Grouping does not fold the relation axis, so these
    # rows sit in separate pairs and never meet; the disclosure is what says so.
    raw_relations: dict[tuple[str, str], set[str]] = {}
    relation_subjects: dict[tuple[str, str], set[str]] = {}
    for row in engine_facts(facts):
        relation = row["relation"]
        canon = _canonicalize(relation, aliases)
        # `fold_relation_name`, not the local `_fold`: this is the membership
        # test, and the rule stated in `_fold`'s docstring is that every one of
        # those goes through the shared helper. Same operation today — the point
        # is that the two folds stay one decision if either ever moves.
        if fold_relation_name(canon) not in sv:
            continue
        obj = row["object"]
        # Typed-spec lookup (#210), NOT a fold of the relation axis: the spec dict
        # is keyed by NFC names, so the lookup normalizes to find it. The relation
        # used for grouping stays `canon`, verbatim.
        spec = typed.get(canon) or typed.get(_fold(relation))
        key = _group_key(obj, spec)
        pair = (_fold(row["subject"]), canon)
        by_key.setdefault(pair, {}).setdefault(key, set()).add(obj)
        unfolded.setdefault(pair, {})[obj] = _group_key_unfolded(obj, spec)
        raw_subjects.setdefault(pair, set()).add(row["subject"])
        split = (pair[0], _fold(canon))
        raw_relations.setdefault(split, set()).add(canon)
        relation_subjects.setdefault(split, set()).add(row["subject"])
    conflicts: dict[tuple[str, str], list[str]] = {}
    subject_variants: dict[tuple[str, str], list[str]] = {}
    object_variants: dict[tuple[str, str], dict[str, list[str]]] = {}
    parse_merges: dict[tuple[str, str], dict[str, list[str]]] = {}
    for pair, groups in by_key.items():
        subjects = raw_subjects[pair]
        if len(groups) <= 1:
            # No contradiction to report — but if folding is what collapsed the
            # objects, it *resolved* one the raw spellings would have fired, and
            # dropping the pair here would make that the only silent outcome in
            # the module. Keep the object channel so ``main`` can disclose it.
            # Gated on the fold, not on len(raws): a #116 scalar merge is not a
            # Unicode merge and must not be announced as one. Both merge
            # mechanisms open this gate — canonical equivalence (_fold_classes)
            # and a typed parse the fold enabled (_parse_merge) — because either
            # one lets finalize compile a KB the raw spellings would have blocked,
            # and this advisory is the only signal on that path.
            merged = {
                _representative(raws): names
                for raws in groups.values()
                if (names := _parse_merge(raws, unfolded[pair]))
            }
            if merged or any(_fold_classes(sorted(raws)) for raws in groups.values()):
                reported = (_representative(subjects), pair[1])
                object_variants[reported] = _variant_map(groups)
                if merged:
                    parse_merges[reported] = merged
            continue
        # Representative restoration on both axes: report strings as written.
        # Distinct folded subjects cannot share a representative (the choice is a
        # function of the fold), so reported keys stay unique.
        reported = (_representative(subjects), pair[1])
        objects = _variant_map(groups)
        conflicts[reported] = sorted(objects)
        subject_variants[reported] = sorted(subjects)
        object_variants[reported] = objects
        # A pair that still contradicts can ALSO hold a fold-enabled parse merge:
        # three values collapse to two, one of them merged only because folding
        # let a typed literal parse. The CONFLICT line then names the survivors
        # and the merged notation appears nowhere in the output — a row the
        # previous release listed by name simply vanishes, and the reader
        # supersedes what is on screen without knowing a third row is behind it.
        # Canonical-equivalence merges are disclosed on this path already (main's
        # per-object `_fold_classes` loop), so leaving this one to the exit-0
        # branch made the parse merge the single merge class with a hole.
        merged = {
            _representative(raws): names
            for raws in groups.values()
            if (names := _parse_merge(raws, unfolded[pair]))
        }
        if merged:
            parse_merges[reported] = merged
    relation_variants = {
        (_representative(relation_subjects[split]), split[1]): sorted(names)
        for split, names in raw_relations.items()
        if len(names) > 1
    }
    return ConflictScan(
        conflicts, subject_variants, object_variants, parse_merges, relation_variants
    )


def detect_conflicts(
    facts: list[dict[str, str]],
    single_valued: set[str],
    typed: dict[str, TypedRelSpec] | None = None,
    aliases: dict[str, str] | None = None,
) -> dict[tuple[str, str], list[str]]:
    """Map (subject, canonical_relation) -> sorted distinct *display* objects,
    for single-valued relations that hold more than one *distinct value* (a
    contradiction).

    Distinctness is judged on the canonical grouping key (typed scalar when
    available, else the raw string — see ``_group_key``), so equivalent typed
    notations do not false-positive. The reported values, however, preserve the
    original object strings (provenance): each distinct key contributes one
    deterministic representative (an object seen for it, chosen by
    ``_representative``). Deterministic; never raises.

    Two grouping subtleties documented on ``_group_key``: ordinal collapses
    cross-unit notations onto the shared rank (rank-only contract, #218/#224 A),
    and a scalar wider than int64 groups here even though the engine skips its
    insertion (harmless grouping-only divergence, #224 C).

    **Alias canonicalization (#227):** when *aliases* is provided (non-empty),
    each row's relation is canonicalized via ``_canonicalize`` before the
    single-valued membership test and before grouping.  This causes surface
    variants that map to the same canonical name (e.g. ``게재연도`` and ``발행년도``
    both aliased to ``published_year``) to collide under one key, so a cross-
    variant contradiction is detected as a single conflict on the canonical
    name.  Relations that do **not** participate in the alias map are passed
    through verbatim (no normalization), preserving byte-identical behaviour for
    those predicates.

    When *aliases* is ``None`` or ``{}`` the function is byte-identical to the
    pre-#227 behaviour: the raw relation string is used throughout, and an NFD-
    authored relation name is reported exactly as written (no silent NFC coercion
    for non-participating relations).

    **Typed-spec lookup (#210):** the ``typed`` dict is keyed by NFC-normalized
    names (``typed_relations`` normalizes at ``common._parse_typed_relations``).
    The lookup first tries the canonical relation name (already NFC when it came
    from the alias map), then falls back to the NFC form of the raw relation
    string.  This ensures that an NFD-authored relation that also participates in
    the alias map still reaches its typed spec, so equivalent notations (억↔조)
    collapse correctly.

    **Unicode folding (#325):** the subject and untyped-object axes are folded to
    NFC, and the reported strings are restored to raw spellings seen. Callers that
    also need to know *which* raw spellings were merged should use
    ``collect_conflicts``, which documents the grouping in full and returns those
    maps alongside this dict."""
    return collect_conflicts(facts, single_valued, typed, aliases).conflicts


def _report_resolved_merges(scan: ConflictScan) -> None:
    """Disclose value groups whose Unicode fold *resolved* a contradiction.

    ``collect_conflicts`` keeps an object channel for a pair that ended up with a
    single group when folding is what collapsed it. Such a pair is absent from
    *conflicts* by construction, so nothing else in the report names it — and
    this is exactly the case where disclosure is the only signal there is: the
    checker exits 0 and ``finalize`` goes on to compile a KB the raw spellings
    would have blocked.

    **The two message classes below have different scopes, deliberately.** The
    equivalence message covers only the conflict-free pairs, because ``main``
    already prints ``value … spellings:`` under every CONFLICT it reports, and
    repeating it here would say each merge twice. The parse message covers
    **every** pair with a merge, conflict-free or not: a pair can merge two
    notations under the fold and still contradict on a third value, and nothing
    on the exit-1 path names the merged notation — the CONFLICT line lists the
    survivors, and the spelling lines under it come from ``_fold_classes``, which
    is silent about a merge that is not canonical equivalence. Restricting this
    loop to the conflict-free pairs made a row the previous release listed by
    name vanish from the output in any form.

    Scope is deliberately the **object** axis, the one where folding *resolves* a
    contradiction. Mixed spellings used to cost duplicate atoms downstream as
    well — ``common.dedup_engine_atoms`` keyed on the raw triple, so both
    spellings entered ``accepted.dl`` — and #342 closed that separately, on every
    axis it folds and not only under a resolved conflict. What is left for this
    advisory is the merge itself: the author wrote two strings and the gate
    treated them as one, which is worth saying whether or not anything
    downstream still doubles.

    **Two message classes, because folding merges values two ways.** Canonical
    equivalence is one; making a typed literal parse is the other, and calling
    the second "canonically equivalent" would be a false statement about the
    strings (``NFD('제3호')`` and ``'3위'`` are not equivalent — the fold merely let
    the first one parse as ordinal rank 3). It also carries a warning the first
    does not: the engine folds nowhere, so it cannot reproduce that merge — it
    loads the decomposed literal untyped, and every one of them when the relation
    name is decomposed as well (see ``collect_conflicts``). Keying the disclosure on
    ``_fold_classes`` left that path completely silent, which is how a KB that
    reported a CONFLICT on the previous release became "no contradictions".

    Printed on **stdout**, not stderr: this is an advisory rather than the
    failure report, and ``finalize`` forwards our stdout unconditionally (it
    writes ``conflicts.stdout`` before testing the return code), so it survives
    the exit-0 path where stderr would be dropped.
    """
    resolved = sorted(k for k in scan.object_variants if k not in scan.conflicts)
    lines = []
    for key in resolved:
        subject, relation = key
        for obj, raws in sorted(scan.object_variants[key].items()):
            for members in _fold_classes(raws):
                lines.append(
                    f"    '{relation}' on '{subject}' value {obj!r} spellings: "
                    f"{_spellings(members)}"
                )
    if lines:
        print(
            f"check_conflicts: {len(lines)} spelling group(s) written in several Unicode "
            "normalization forms and merged into one value, so no contradiction is "
            "reported for them:"
        )
        for line in lines:
            print(line)
        print(
            "  These spellings are canonically equivalent, so they collapse into a single "
            "facts/accepted.dl atom, written in the composed spelling where the group has "
            "one. They still differ "
            "byte-wise in sources/ and facts/candidates.csv, where each one is its own row. "
            "Unify them at the source and re-collect."
        )
    # Every pair with a parse merge, not just the ones that ended up conflict-free.
    # A pair can merge two notations under the fold and still contradict on a
    # third value; ``resolved`` skips it, and then nothing anywhere names the
    # merged notation — main's CONFLICT block reports the surviving values and
    # the object-spelling lines under it come from ``_fold_classes``, which is
    # silent about a merge that is not canonical equivalence. So the reader was
    # shown two values where the previous release showed three, with the missing
    # one absent from the output in any form.
    parsed = []
    for key in sorted(scan.parse_merges):
        subject, relation = key
        for obj, notations in sorted(scan.parse_merges[key].items()):
            parsed.append(
                f"    '{relation}' on '{subject}' value {obj!r} notations: "
                f"{_spellings(notations)}"
            )
    if not parsed:
        return
    print(
        f"check_conflicts: {len(parsed)} value(s) merged only because a Unicode fold made "
        "a typed literal parse, so the notations below are counted as one value "
        "wherever this run reports a count:"
    )
    for line in parsed:
        print(line)
    print(
        "  These notations are NOT canonically equivalent: a decomposed literal does not "
        "parse as its declared type, and folding is what let it reach the scalar its "
        "counterpart already had. So they stay two separate atoms, and the engine's typed "
        "projection does not fold either — it hands the object as written to "
        "literal_types.normalize — so it loads the decomposed literal untyped (and, when "
        "the relation name is decomposed too, every one of them), and the notations never "
        "meet there. Unify the spelling in sources/ and re-collect, then re-run to see "
        "whether a contradiction remains."
    )


def _report_split_relations(scan: ConflictScan) -> None:
    """Disclose subjects whose single-valued relation is written several ways.

    Membership folds and grouping does not, so those rows *pass* the membership
    test, enter the grouping loop, and then split on the relation axis into pairs
    that never meet. When each pair holds one value the module reports "0
    conflicts" about rows that, read together, contradict each other — and the
    likeliest mixed KB is exactly this one, because a normalization form is a
    property of the source document and the filesystem it came from, so a whole
    row flips at once rather than one field.

    Deferring the *grouping* decision is the #210 maintainer call this change
    leaves open (see ``collect_conflicts``). Deferring the *disclosure* is not the
    same thing: at exit 0 there is no CONFLICT line to hang the
    "(relation written in N mixed Unicode normalization forms)" suffix on, so
    without this the run says nothing at all.

    Pairs already covered by a CONFLICT line are skipped — that line carries the
    suffix and the spelling list on stderr, and repeating it here would double-
    report one fact in two streams.
    """
    conflicting = {(_fold(s), _fold(r)) for s, r in scan.conflicts}
    lines = []
    for key in sorted(scan.relation_variants):
        subject, relation = key
        if (_fold(subject), relation) in conflicting:
            continue
        lines.append(f"    on '{subject}' spellings: {_spellings(scan.relation_variants[key])}")
    if not lines:
        return
    # One line per (subject, relation) pair, so the header names that unit and no
    # other. Naming either axis alone miscounts in one direction or the other:
    # "N subject(s)" reads as N people when one subject has two split relations,
    # and "N relation(s) … for one subject" reads as one person when N subjects
    # each have one. There is no single axis to name — the pair is the count.
    print(
        f"check_conflicts: {len(lines)} (subject, relation) pair(s) whose single-valued relation "
        "is written in several Unicode normalization forms, so their rows were compared "
        "separately:"
    )
    for line in lines:
        print(line)
    print(
        "  Relation-name membership is folded but grouping is not, so rows spelled one way "
        "were never compared against rows spelled the other, and a contradiction between "
        "them would not be reported above. Unify the spelling in sources/ and re-collect, "
        "then re-run."
    )


def non_ascii_digit_note(objects: list[str], spec: TypedRelSpec | None) -> list[str] | None:
    """Extra guidance lines for a **typed** relation's conflict group when one of
    its values carries non-ASCII digits; ``None`` otherwise.

    The generic advice printed by ``main`` ("mark the outdated row superseded")
    assumes one of the values is out of date. Under a typed relation a value
    carrying non-ASCII digits does not parse as the declared type at all
    (``_group_key`` degrades it to ``("raw", obj)``), so superseding the OTHER row
    clears this gate while leaving the KB holding the value the engine cannot
    read.

    **The ``spec is None`` gate is load-bearing, not defensive.** Under an untyped
    single-valued relation ``_group_key`` returns a raw key because there is no
    spec, *not* because of digit width — ``GPT-４`` and ``GPT-5`` key identically,
    ``GPT-４`` is a perfectly usable ``relation/3`` fact, and superseding the
    outdated row is exactly the right fix. Every clause of this note would be
    false there, and it would steer the user away from the one action that works.

    **Carrying non-ASCII digits is not the same as failing to parse**, so the
    normalizer decides, not the digit predicate alone. The unit group is outside
    the digit policy on purpose and ``_parse_amount_units`` does not *validate* a
    unit NAME, so a declared unit may carry them: ``amount(100,"억１")`` under a
    declared ``억１`` unit normalizes to a scalar and ``_group_key`` keys it
    ``("scalar", …)``. Both leading clauses of the note would be false there.
    Asking ``normalize`` converges this note and ``_group_key`` on one predicate.

    ``_parse_amount_units`` does now **NFC-fold** a unit name (#325), which is a
    normalization and not a validation, so the example above is unaffected. That
    fold runs at policy load, *before* any value meets the ``[0-9]`` numeric
    group — it is upstream of the digit gate, not downstream of it — so its
    harmlessness cannot rest on ordering. It rests on the fold itself: NFC does
    not change a single non-ASCII digit, anywhere in Unicode. Of the 750
    non-ASCII ``Nd`` code points, 80 carry a decomposition mapping and **none of
    those mappings is canonical** — every one is compatibility-tagged, which is
    precisely what NFKC would apply and NFC will not. So no ``Nd`` code point is
    touched by NFC or NFD, ``has_non_ascii_digits`` is invariant under canonical
    equivalence as a general fact rather than as a claim about fullwidth, and the
    fold can neither make a value the digit policy rejects start parsing nor the
    reverse. The same sweep also finds **0 canonical decompositions whose base
    character is an ``Nd`` digit**, which extends the invariant from single code
    points to arbitrary strings: no ``digit + combining mark`` sequence composes
    into something else either. (All measured on unicodedata 16.0.0.)

    Two things the wording deliberately does NOT claim:

    * that supersession cannot resolve the conflict — superseding the offending
      row itself resolves it correctly, so the note says supersession *can* leave
      the bad value behind, never that it is useless;
    * that re-collection *replaces* supersession — for genuinely different values
      (``100억`` vs ``２００억``) correcting the source yields ``100억`` vs
      ``200억``, still a conflict that supersession must settle.

    Pure; never raises."""
    if spec is None:
        return None
    offenders = [
        o
        for o in objects
        if literal_types.has_non_ascii_digits(o)
        and literal_types.normalize(spec.type, o, spec.units) is None
    ]
    if not offenders:
        return None
    shown = ", ".join(f"'{literal_types.mark_non_ascii_digits(o)}'" for o in offenders)
    return [
        f"    note: {shown} carries non-ASCII digits, so it does not parse as this",
        f"          relation's declared type ({spec.type}) and is compared here as a raw",
        "          string. Superseding a row clears this gate but can leave that",
        "          unreadable value in the KB. Correct the source to ASCII digits and",
        "          re-collect; if the values still differ afterwards, supersede the",
        "          outdated one (docs/reference/typed-relations.md).",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect single-valued-relation contradictions.")
    parser.add_argument("--wiki", default=os.environ.get("FACTLOG_ROOT", "."), help="KB root")
    parser.parse_args(argv)

    ensure_dirs()
    single_valued = single_valued_relations()
    if not single_valued:
        print("check_conflicts: no single-valued relations declared (policy/single-valued.md); nothing to check")
        return 0

    typed = typed_relations()
    scan = collect_conflicts(load_facts(), single_valued, typed, relation_aliases())
    conflicts, subject_variants, object_variants = (
        scan.conflicts,
        scan.subject_variants,
        scan.object_variants,
    )
    if not conflicts:
        print(f"check_conflicts: 0 conflicts across {len(single_valued)} single-valued relation(s)")
        _report_resolved_merges(scan)
        _report_split_relations(scan)
        return 0

    print(f"check_conflicts: {len(conflicts)} conflict(s) found", file=sys.stderr)
    _report_resolved_merges(scan)
    _report_split_relations(scan)
    aliases = relation_aliases()
    # Whether folding merged spellings anywhere. This is an *extra* disclosure,
    # never a replacement for the supersede guidance: a contradiction that a mixed
    # spelling merely joined is still a contradiction, and unifying the spelling
    # does not resolve it.
    any_mixed = False
    # Membership is folded but grouping is not (#210 is a maintainer call, see
    # collect_conflicts), so one contradiction written with the relation spelled
    # two ways surfaces as two CONFLICT lines that are byte-different and look
    # identical. Collapsing them means folding the grouping key, which is exactly
    # the deferred decision — so disclose instead, as the subject axis does.
    # ``relation_variants`` counts spellings over every pair examined, not only
    # the conflicting ones: a row hiding under the other spelling is invisible to
    # this conflict whether or not it happens to conflict by itself, and that is
    # the fact worth naming. Re-keyed on the fold so the reported subject (a raw
    # spelling) finds it. The count comes from the rows, not from
    # policy/single-valued.md: sv holds folded names, so several policy spellings
    # collapse to one element there.
    relation_spellings = {(_fold(s), r): names for (s, r), names in scan.relation_variants.items()}
    for key, objects in sorted(conflicts.items()):
        subject, relation = key
        suffix = " (canonical; incl. surface variants)" if aliases and relation in set(aliases.values()) else ""
        subjects = subject_variants[key]
        if len(subjects) > 1:
            suffix += f" (subject written in {len(subjects)} mixed Unicode normalization forms)"
        relations = relation_spellings.get((_fold(subject), _fold(relation)), [relation])
        if len(relations) > 1:
            suffix += f" (relation written in {len(relations)} mixed Unicode normalization forms)"
        print(
            f"  CONFLICT: single-valued '{relation}'{suffix} on '{subject}' has "
            f"{len(objects)} values: {', '.join(objects)}",
            file=sys.stderr,
        )
        # Print the spellings themselves, escaped: the whole difficulty of this
        # class of conflict is that the strings render identically, so naming a
        # count without the code points leaves the reader unable to act.
        if len(subjects) > 1:
            any_mixed = True
            print(f"    subject spellings: {_spellings(subjects)}", file=sys.stderr)
        if len(relations) > 1:
            any_mixed = True
            print(f"    relation spellings: {_spellings(relations)}", file=sys.stderr)
        for obj in objects:
            # Evidence of a *Unicode* merge is that folding collapses spellings,
            # not that the group holds several strings: a typed relation groups on
            # the parsed scalar, so #116 cross-notation equivalents (amount(5400,
            # "억") and amount(0.54,"조") -> 5.4e11) share a group while being
            # plain NFC and rendering nothing alike. _fold_classes answers "did it
            # merge" and "what did it merge" at once, so the gate and the strings
            # printed under it cannot disagree — one line per class, since
            # "canonically equivalent" holds within a class and not across them.
            for members in _fold_classes(object_variants[key][obj]):
                any_mixed = True
                print(
                    f"    value {obj!r} spellings: {_spellings(members)}",
                    file=sys.stderr,
                )
        # The conflict key is the canonical relation, which is already NFC when it
        # came from the alias map; fall back to the NFC form for an NFD-authored
        # name. NOT identical to detect_conflicts' lookup: there the second probe
        # uses the NFC of the ROW's relation, which can differ from the canonical
        # one when an NFD surface form participates in an alias (#210).
        spec = typed.get(relation) or typed.get(unicodedata.normalize("NFC", relation))
        for line in non_ascii_digit_note(objects, spec) or ():
            print(line, file=sys.stderr)
    print(
        "  Resolve by marking the outdated row(s) status='superseded' in "
        "facts/candidates.csv, then re-run.",
        file=sys.stderr,
    )
    if any_mixed:
        print(
            "  Some string(s) above are written in more than one Unicode normalization form: "
            "they are canonically equivalent but differ byte-wise, so the reported spelling "
            "does not grep to every row behind it (the spellings are listed with their form "
            "under each conflict). Do NOT repair this by editing facts/candidates.csv: "
            "merge_candidates rebuilds those rows from runs/*.json and matches everything it "
            "carries back — statuses and superseded rows alike — on the raw "
            "(subject, relation, object, source-without-anchor) key, so a hand-edited spelling "
            "is discarded on the next merge and stops matching the key that preserves its 'superseded' "
            "mark. Unify the spelling in sources/ and re-collect. "
            "That is a separate repair from superseding, and neither substitutes for the other.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
