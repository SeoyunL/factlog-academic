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
engine input, and the conflict clears).

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
    engine_facts,
    ensure_dirs,
    load_facts,
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
    """
    return min(raws, key=lambda s: (s != _fold(s), s))


def _form_label(value: str) -> str:
    """Name the Unicode normalization form *value* is written in.

    Only NFC and NFD are named: they are the two forms this module folds between,
    and telling them apart is what a reader needs in order to know which row to
    edit. A string can be neither — composed and decomposed syllables mixed
    inside one string — which is reported as ``"mixed"`` rather than guessed at.
    A pure-ASCII string is identical under both and is reported ``"NFC"``.
    """
    if value == unicodedata.normalize("NFC", value):
        return "NFC"
    if value == unicodedata.normalize("NFD", value):
        return "NFD"
    return "mixed"


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
    byte-identical."""
    folded = _fold(obj)
    if spec is not None:
        scalar = literal_types.normalize(spec.type, folded, spec.units)
        if scalar is not None:
            return ("scalar", scalar)
    return ("raw", folded)


def collect_conflicts(
    facts: list[dict[str, str]],
    single_valued: set[str],
    typed: dict[str, TypedRelSpec] | None = None,
    aliases: dict[str, str] | None = None,
) -> tuple[
    dict[tuple[str, str], list[str]],
    dict[tuple[str, str], list[str]],
    dict[tuple[str, str], dict[str, list[str]]],
]:
    """Return ``(conflicts, subject_variants, object_variants)``.

    *conflicts* is exactly what ``detect_conflicts`` returns (see there).

    The other two are symmetric spelling channels — folding merges strings on the
    subject axis and on the object axis alike, and a reported representative
    stands in for every spelling it absorbed on **both**:

    * *subject_variants* maps each reported key to the sorted raw subject strings
      merged under it;
    * *object_variants* maps each reported key to ``{reported object: sorted raw
      objects}`` for that key's value groups. Its key set is a **superset** of
      *conflicts*: a pair whose objects folding collapsed to a single value is
      not a conflict, yet is retained here so the caller can disclose that the
      fold is what resolved it (see ``_report_resolved_merges``). *conflicts* and
      *subject_variants* carry conflicting pairs only.

    Either list holds one entry when nothing was merged. More than one means the
    KB writes that string in several Unicode normalization forms, so the reported
    representative does not grep to every row behind it — which is exactly what
    the caller has to disclose.

    These channels exist so ``main`` can report a merge without duplicating the
    grouping logic, while ``detect_conflicts`` keeps its established return shape
    (a large body of pinned tests asserts that dict directly).

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

    **Engine divergence:** ``common.dedup_engine_atoms`` dedups on the **raw**
    ``(subject, relation, object)`` triple, so the engine writes an NFC-spelled
    and an NFD-spelled string into ``accepted.dl`` as two distinct entities. The
    two folded axes diverge from that in *opposite* directions, and only one of
    them is safe:

    * **subject axis — checker stricter.** It reports a contradiction the engine
      cannot see from ``accepted.dl`` alone. The gate closes on a KB the engine
      would have accepted, so nothing slips through.
    * **object axis — checker more permissive.** Two objects the raw grouping
      called a contradiction now agree, so the gate *opens* where it used to
      close, ``finalize`` compiles, and both spellings reach ``accepted.dl`` as
      separate atoms — the inflated duplicate count ``dedup_engine_atoms`` exists
      to prevent, arrived at through the normal path.

    The permissive direction is semantically right: ``common._canonical_value``
    (#213) already fixed NFC as value equality. What is not acceptable is doing
    it silently, so the object channel below survives for a pair folding
    resolved, and ``_report_resolved_merges`` discloses it at exit 0. The
    engine-side raw-triple dedup itself is a follow-up.
    """
    typed = typed or {}
    aliases = aliases or {}
    # Precompute the set of canonical single-valued relation names so the
    # per-row membership test is O(1). Folded on both sides: this test is the
    # only thing standing between a row and the grouping loop, and
    # ``common._relation_names_from`` does not normalize the names it parses out
    # of policy/single-valued.md, so comparing raw would make it a byte
    # comparison between two hand-written files. Membership only — the folded
    # name is never used as a grouping key (see the loop below).
    sv = {_fold(_canonicalize(r, aliases)) for r in single_valued}
    # (folded subject, canonical_relation) -> group key -> set of raw objects.
    by_key: dict[tuple[str, str], dict[tuple, set[str]]] = {}
    # Same pair -> set of raw subject spellings folded into it.
    raw_subjects: dict[tuple[str, str], set[str]] = {}
    for row in engine_facts(facts):
        relation = row["relation"]
        canon = _canonicalize(relation, aliases)
        if _fold(canon) not in sv:
            continue
        obj = row["object"]
        # Typed-spec lookup (#210), NOT a fold of the relation axis: the spec dict
        # is keyed by NFC names, so the lookup normalizes to find it. The relation
        # used for grouping stays `canon`, verbatim.
        spec = typed.get(canon) or typed.get(_fold(relation))
        key = _group_key(obj, spec)
        pair = (_fold(row["subject"]), canon)
        by_key.setdefault(pair, {}).setdefault(key, set()).add(obj)
        raw_subjects.setdefault(pair, set()).add(row["subject"])
    conflicts: dict[tuple[str, str], list[str]] = {}
    subject_variants: dict[tuple[str, str], list[str]] = {}
    object_variants: dict[tuple[str, str], dict[str, list[str]]] = {}
    for pair, groups in by_key.items():
        subjects = raw_subjects[pair]
        if len(groups) <= 1:
            # No contradiction to report — but if folding is what collapsed the
            # objects, it *resolved* one the raw spellings would have fired, and
            # dropping the pair here would make that the only silent outcome in
            # the module. Keep the object channel so ``main`` can disclose it.
            # Gated on the fold, not on len(raws): a #116 scalar merge is not a
            # Unicode merge and must not be announced as one.
            if any(len({_fold(r) for r in raws}) < len(raws) for raws in groups.values()):
                reported = (_representative(subjects), pair[1])
                object_variants[reported] = _variant_map(groups)
            continue
        # Representative restoration on both axes: report strings as written.
        # Distinct folded subjects cannot share a representative (the choice is a
        # function of the fold), so reported keys stay unique.
        reported = (_representative(subjects), pair[1])
        objects = _variant_map(groups)
        conflicts[reported] = sorted(objects)
        subject_variants[reported] = sorted(subjects)
        object_variants[reported] = objects
    return conflicts, subject_variants, object_variants


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
    return collect_conflicts(facts, single_valued, typed, aliases)[0]


def _report_resolved_merges(
    conflicts: dict[tuple[str, str], list[str]],
    object_variants: dict[tuple[str, str], dict[str, list[str]]],
) -> None:
    """Disclose value groups whose Unicode fold *resolved* a contradiction.

    ``collect_conflicts`` keeps an object channel for a pair that ended up with a
    single group when folding is what collapsed it. Such a pair is absent from
    *conflicts* by construction, so nothing else in the report names it — and
    this is exactly the case where disclosure is the only signal there is. The
    checker exits 0, ``finalize`` goes on to compile, and
    ``common.dedup_engine_atoms`` dedups on the raw ``(subject, relation,
    object)`` triple, so both spellings enter ``accepted.dl`` as two atoms of one
    visible fact — the inflated duplicate count that function exists to prevent.

    Printed on **stdout**, not stderr: this is an advisory rather than the
    failure report, and ``finalize`` forwards our stdout unconditionally (it
    writes ``conflicts.stdout`` before testing the return code), so it survives
    the exit-0 path where stderr would be dropped.
    """
    lines = []
    for key in sorted(k for k in object_variants if k not in conflicts):
        subject, relation = key
        for obj, raws in sorted(object_variants[key].items()):
            if len({_fold(r) for r in raws}) < len(raws):
                lines.append(
                    f"    '{relation}' on '{subject}' value {obj!r} spellings: "
                    f"{_spellings(raws)}"
                )
    if not lines:
        return
    print(
        f"check_conflicts: {len(lines)} value group(s) written in several Unicode "
        "normalization forms and merged into one value, so no contradiction is "
        "reported for them:"
    )
    for line in lines:
        print(line)
    print(
        "  These spellings are canonically equivalent, but they differ byte-wise and "
        "engine atoms dedup on the raw triple, so each one still enters "
        "facts/accepted.dl as a separate atom. Unify them at the source and re-collect."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Detect single-valued-relation contradictions.")
    parser.add_argument("--wiki", default=os.environ.get("FACTLOG_ROOT", "."), help="KB root")
    parser.parse_args(argv)

    ensure_dirs()
    single_valued = single_valued_relations()
    if not single_valued:
        print("check_conflicts: no single-valued relations declared (policy/single-valued.md); nothing to check")
        return 0

    conflicts, subject_variants, object_variants = collect_conflicts(
        load_facts(), single_valued, typed_relations(), relation_aliases()
    )
    if not conflicts:
        print(f"check_conflicts: 0 conflicts across {len(single_valued)} single-valued relation(s)")
        _report_resolved_merges(conflicts, object_variants)
        return 0

    print(f"check_conflicts: {len(conflicts)} conflict(s) found", file=sys.stderr)
    _report_resolved_merges(conflicts, object_variants)
    aliases = relation_aliases()
    # Whether folding merged spellings anywhere. This is an *extra* disclosure,
    # never a replacement for the supersede guidance: a contradiction that a mixed
    # spelling merely joined is still a contradiction, and unifying the spelling
    # does not resolve it.
    any_mixed = False
    for key, objects in sorted(conflicts.items()):
        subject, relation = key
        suffix = " (canonical; incl. surface variants)" if aliases and relation in set(aliases.values()) else ""
        subjects = subject_variants[key]
        if len(subjects) > 1:
            suffix += f" (subject written in {len(subjects)} mixed Unicode normalization forms)"
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
        for obj in objects:
            raws = object_variants[key][obj]
            # Evidence of a *Unicode* merge is that folding collapses the group,
            # not that the group holds several strings: a typed relation groups on
            # the parsed scalar, so #116 cross-notation equivalents (amount(5400,
            # "억") and amount(0.54,"조") -> 5.4e11) share a group while being
            # plain NFC and rendering nothing alike. Keying the disclosure on
            # len(raws) > 1 fired this whole paragraph — "they render identically",
            # "merged across normalization forms", "unify the spelling" — at a KB
            # with zero decomposed code points, none of which was true of it.
            if len({_fold(r) for r in raws}) < len(raws):
                any_mixed = True
                print(
                    f"    value {obj!r} spellings: {_spellings(raws)}",
                    file=sys.stderr,
                )
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
            "merge_candidates rebuilds those rows from runs/*.json and carries back only "
            "status, keyed on the raw (subject, relation, object) triple — so a hand-edited "
            "spelling is discarded on the next merge, and stops matching the key that "
            "preserves its 'superseded' mark. Unify the spelling in sources/ and re-collect. "
            "That is a separate repair from superseding, and neither substitutes for the other.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    from common import run_cli

    sys.exit(run_cli(main))
