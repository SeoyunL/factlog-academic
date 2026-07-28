#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Deterministic search router for `/factlog ask`.

Given an LLM-drafted candidate Datalog query, decide — by deterministic code,
never by LLM judgment — whether the question is answered by the facts/rule
ENGINE or routed to WIKI exploration, and (for the engine path) evaluate it.

Routing is keyed on the *reason class* returned by
``common.validate_candidate_query`` (NOT a raw boolean):

    ok=True,  predicate != review_required  -> route=engine (positive/negative)
    ok=True,  predicate == review_required  -> route=wiki
    ok=False, reason is fact-absence        -> route=engine, negative=True
                                               (vocabulary accepted, fact absent)
    ok=False, reason is shape/vocabulary    -> route=wiki

A *verified negative* (engine ran, no matching fact/path) is an engine result —
it is NEVER demoted to unverified wiki prose. Conflating "engine says no" with
"cannot express" is the most damaging routing error this module guards against.

The validator is always called with ``load_accepted_facts()`` (engine input
only), never ``load_facts()`` (candidates), so candidate vocabulary cannot leak
into the engine path.

This module is READ-ONLY with respect to engine inputs: it never writes
``facts/query.dl`` or ``facts/accepted.dl``.

Usage:
    python3 ask_router.py validate "<draft>" [--target <kb>]
    python3 ask_router.py evaluate "<draft>" [--target <kb>]
    python3 ask_router.py render   "<draft>" [--all] [--target <kb>]
    python3 ask_router.py search   "<question>" [--all] [--target <kb>]
    python3 ask_router.py wiki     "<question>" [--all] [--target <kb>]

Each subcommand prints JSON (validate/evaluate) or the rendered answer (render)
to stdout. --target ("--wiki" is an accepted alias) overrides $FACTLOG_ROOT, which
overrides the active-KB config, which overrides cwd.
"""

from __future__ import annotations

import argparse
import importlib
import json
import math
import os
import re
import sys
import unicodedata
from pathlib import Path

# Ensure tools/ is importable when run directly, and resolve the KB root BEFORE
# importing common (whose module-level ROOT captures FACTLOG_ROOT at import).
_TOOLS_DIR = Path(__file__).parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# Resolve the KB root and export it before importing common, which binds
# its module-level paths from FACTLOG_ROOT at import time.
import factlog_config  # noqa: E402

# --target is the canonical spelling across the toolchain; --wiki stays accepted as
# an alias because the sibling engine scripts and SKILL.md spell it that way (#533).
# ONE tuple feeds BOTH the import-time pre-pass below and every subparser in
# build_parser(): a spelling only one of the two knew would be either
# read-but-unadvertised or accepted-but-ignored, and an ignored KB flag silently
# routes the ask at whatever the config/cwd tier resolved to.
_ROOT_FLAGS = ("--target", "--wiki")

# The one help string every subcommand's root flag uses, so six declarations cannot
# drift into six descriptions of one rule. It states the resolution the pre-pass
# actually performs; the old "overrides FACTLOG_ROOT" stopped at the second tier and
# never mentioned the active-KB config (#531).
_ROOT_FLAG_HELP = (
    "KB root (--wiki is an alias). Overrides $FACTLOG_ROOT and the active-KB "
    "config; without it the root is resolved as $FACTLOG_ROOT > active-KB config > cwd."
)


def _peek_root_flag(argv: list[str] | None = None) -> str | None:
    """The KB root given on the command line, or None.

    ``parse_known_args`` because this runs at import time, before build_parser()'s
    real parser exists: the peek must not reject an argument it is not responsible
    for — here that includes the subcommand and its positional draft/question.
    Rejecting a typo is main()'s job, once, through the strict parse.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument(*_ROOT_FLAGS, dest="target", default=None)
    known, _ = pre.parse_known_args(sys.argv[1:] if argv is None else argv)
    return known.target


os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root(_peek_root_flag())[0]

from common import (  # noqa: E402
    ACCEPTED_DL,
    CANDIDATES_CSV,
    ENGINE_STATUSES,
    LOGIC_POLICY_DL,
    QUERY_ENTITY_NOT_ACCEPTED,
    QUERY_FACT_ABSENT,
    QUERY_OK,
    QUERY_RELATION_NOT_ACCEPTED,
    FactlogError,
    KbContext,
    arg_value,
    canonical_value,
    canonical_variants_of,
    is_quoted_string,
    query_args,
    classify_query,
    path_query_rows,
    entity_set,
    fact_signals,
    load_accepted_facts,
    load_facts,
    load_logic_policy,
    logic_policy_md_has_rules,
    relation_row_matches,
    policy_row_matches,
    nearby_vocabulary,
    policy_predicates,
    relation_aliases,
    value_hierarchy,
    run_wirelog,
    is_sync_ignored,
    sync_ignore_patterns,
)
from factlog import ingest, literal_types  # noqa: E402
from factlog.front_matter_scan import front_matter_body, front_matter_end_line  # noqa: E402

# The bridge (#576) resolves a provenance anchor with the SAME function that
# validates one. Imported as a module, not by symbol, so the call sites read as
# "validate's answer" rather than as a local helper that happens to agree today.
# Measured import cost 19 ms; no cycle — validate imports common/factlog, not this.
import validate  # noqa: E402

# Keep the default answer short enough to scan while retaining an explicit,
# deterministic escape hatch for audit work.  This cap is deliberately applied
# by renderers, not by an LLM deciding which facts matter.
DEFAULT_RENDER_ROW_LIMIT = 20


def _policy_program_optional() -> str:
    """Return the fully assembled policy text — the generated `logic-policy.dl`
    PLUS the optional hand-authored `logic-policy.extra.dl` — or '' if no usable
    policy can be assembled yet.

    `/factlog ask` is interactive and must work before `/factlog check` compiles
    `policy/logic-policy.dl`. Reading the *assembled* program via the SAME loader
    `/factlog check` uses — `load_logic_policy()` → `common._load_logic_policy_from`,
    which merges `logic-policy.extra.dl` onto the compiled base — is the single
    source of truth, so ask and check never drift on what the policy program IS.
    That loader already merges `logic-policy.extra.dl` even when the compiled
    `logic-policy.dl` is ABSENT (#190), so a hand-authored comparison predicate
    that lives ONLY in extra.dl (no compiled .dl, no rules in logic-policy.md) is
    now seen and evaluated here, matching check (#198 — closes the ask≠check gap
    where extra.dl was silently ignored when the .dl was absent, #152/#120).
    Both the classify/route path and the evaluate/render path read this, so one
    source of truth fixes both.

    NON-RAISING by contract (#193): `_load_logic_policy_from` fails loud in a few
    cases (`logic-policy.dl` absent WHILE `logic-policy.md` defines uncompiled
    rules; a `canonical/3` head in the policy text) — the right behavior for the
    `check` verification gate, but ask is exploratory and must never hard-fail.
    We reuse the whole loader and catch `FactlogError` here rather than forking
    just its extra.dl-merge tail (which would duplicate logic and invite drift):
    on a LOAD-STAGE failure this returns '' (no policy applied). The uncompiled-
    but-authored `logic-policy.md` case is still surfaced separately as a warning
    by `_policy_uncompiled` (not silently dropped), so #193's behavior is intact —
    an empty return here + that warning, exactly as before.

    Scope note: this guards only the LOAD stage. The ENGINE-EVALUATION stage
    (`evaluate` -> `common.run_wirelog`) re-loads the policy AND runs pyrewire, so
    a present-but-broken `logic-policy.extra.dl` (an unscaled `number` threshold,
    or a syntax error the loader does not parse) can still fail there — including
    with a NON-`FactlogError` pyrewire exception. That stage is guarded separately
    at the `run_wirelog()` call in `evaluate` (degrading to a `policy_unevaluable`
    signal the render/evaluate commands surface as POLICY_UNEVALUABLE_WARNING),
    because it is a distinct failure surface this loader helper never reaches.
    """
    try:
        return load_logic_policy()
    except FactlogError:
        return ""


# Greppable one-line hint shown when the author wrote policy rules but never
# compiled them. Mirrors the remediation `/factlog check` prints on the same
# condition (run the generator, or /factlog add), but as a warning — ask is
# exploratory, not a verification gate.
POLICY_UNCOMPILED_WARNING = (
    "WARNING: policy is uncompiled — policy/logic-policy.md defines rules but "
    "policy/logic-policy.dl is absent, so policy is being IGNORED in this answer. "
    "Run tools/generate_logic_policy.py (or /factlog add) to compile it."
)

# Greppable one-line hint shown when a hand-authored logic-policy.extra.dl is
# PRESENT but the engine cannot evaluate it (a type-violating threshold, broken
# .dl syntax, etc.). Distinct from POLICY_UNCOMPILED_WARNING (uncompiled
# logic-policy.md rules) — the file and the failure mode differ. ask is graceful
# (#193): rather than crash or fake a verified negative, it answers WITHOUT the
# broken policy and says so; `{reason}` carries the engine/loader message so the
# author can fix the file.
POLICY_UNEVALUABLE_WARNING = (
    "WARNING: policy is unevaluable — policy/logic-policy.extra.dl could not be "
    "evaluated by the engine, so this answer was produced WITHOUT policy. Fix "
    "policy/logic-policy.extra.dl. Reason: {reason}"
)


def _policy_uncompiled() -> bool:
    """True iff the author wrote policy rules but never compiled them:
    ``logic-policy.dl`` is absent while ``logic-policy.md`` defines >=1 compilable
    rule.

    Mirrors ``/factlog check``'s detection (``common._load_logic_policy_from``)
    using the SAME shared helper (``logic_policy_md_has_rules``, #190), so ask and
    check never disagree about what "has rules" means — a single source of truth.
    Unlike check, ask stays graceful: it surfaces a warning, not a hard failure,
    because ask must work before check compiles the policy. This closes the
    asymmetry (#193) where ask silently ignored an uncompiled policy that check
    caught. The benign no-policy case (empty/prose ``logic-policy.md``) yields
    False here exactly as it does for check, so ask's legitimate no-policy
    tolerance is unchanged — only "rules written but not compiled" warns.
    """
    if LOGIC_POLICY_DL.is_file():
        return False
    return logic_policy_md_has_rules(LOGIC_POLICY_DL.with_name("logic-policy.md"))


def _predicate_of(draft: str) -> str:
    """Parse the predicate name the way the validator does (regex), so the router
    and the validator never disagree about what predicate a draft calls."""
    match = re.match(r"^([A-Za-z_]\w*)\(", draft.strip())
    return match.group(1) if match else ""


def classify(draft: str, facts: list[dict[str, str]]) -> dict[str, object]:
    """Route a draft to engine vs wiki by the validator's reason class.

    Returns {ok, reason, route, negative, predicate}. Pure: no I/O beyond the
    validator, which only reads the accepted facts already loaded by the caller.
    """
    ok, code, reason = classify_query(draft, facts, policy_program=_policy_program_optional())
    predicate = _predicate_of(draft)

    # Route on the stable classification CODE, never on the reason text — so an
    # entity/relation constant can never masquerade as a routing signal.
    if code == QUERY_OK:
        route, negative = "engine", False
    elif code == QUERY_FACT_ABSENT:
        # Accepted vocabulary, fact/path absent: a verified negative — an engine
        # answer, never demoted to wiki.
        route, negative = "engine", True
    else:
        # review_required or any shape/vocabulary failure: cannot be expressed
        # over accepted facts.
        route, negative = "wiki", False

    return {
        "ok": ok,
        "code": code,
        "reason": reason,
        "route": route,
        "negative": negative,
        "predicate": predicate,
        # An uncompiled-but-authored policy is silently ignored by the engine
        # path (policy program is ''); flag it so callers surface a warning
        # instead of presenting a policy-free answer as fully policy-checked (#193).
        "policy_uncompiled": _policy_uncompiled(),
    }


def evaluate_relation(draft: str, facts: list[dict[str, str]]) -> list[list[str]]:
    """Evaluate a single ``relation(...)`` query against accepted facts.

    Delegates to `common.relation_row_matches` — the ONE matching predicate shared
    with the logic report and the query gate. Three near-copies of this logic used
    to exist and they drifted, so the same question got different answers from
    `/factlog check` and `/factlog ask` (#213).

    Quoted constants must match; variables bind freely. A canonical relation name
    also matches its declared surface variants; the object honours
    policy/value-hierarchy.md. Does not touch facts/query.dl.
    """
    args = query_args(draft)
    if len(args) != 3:
        return []
    hierarchy = value_hierarchy()
    aliases = relation_aliases()
    return [
        [row["subject"], row["relation"], row["object"]]
        for row in facts
        if relation_row_matches(args, row, aliases, hierarchy)
    ]


def coverage_hint(
    draft: str,
    facts: list[dict[str, str]],
    max_relations: int = 6,
) -> str | None:
    """Informational coverage hint for a verified-negative relation query (#189).

    When ``relation("S", "R", O)?`` is a VERIFIED NEGATIVE (0 rows) yet the subject
    ``S`` is an accepted entity that carries fact(s) under OTHER relations, return a
    single informational line naming those relations — so a user can tell a
    *predicate mismatch* ("I asked the wrong relation") apart from an *honest
    absence* ("there really is no such fact"). Deterministic and in-memory: reads
    only the accepted facts already loaded by the caller; writes nothing; never
    changes the verdict, routing, storage, or provenance — it is an ADDED line.

    Returns None (no hint) in every case that could produce a false positive:
      - the query is NOT a VERIFIED NEGATIVE (``classify`` route != engine OR
        negative == False) — e.g. an accepted subject with an UNACCEPTED object
        routes to wiki, and a wiki/positive answer must never carry this hint.
        This is the SAME gate ``cmd_render`` applies (``decision["negative"]``),
        reused via ``classify`` so render and evaluate never drift on scope;
      - predicate is not ``relation`` (path/count/policy carry no predicate
        mismatch), or the query is not a 3-arg relation;
      - the subject or relation argument is a variable (no concrete predicate to
        point at — a predicate-mismatch hint would be meaningless);
      - the subject is NOT an accepted entity (an unknown subject is already
        wiki-routed; never fabricate a hint for it);
      - the subject DOES have fact(s) under the queried relation R (then the empty
        result is an OBJECT mismatch, not a predicate mismatch — no hint);
      - the subject has NO fact under any other relation either (a genuine
        verified negative — the honest-absence value we must preserve).

    Relation names are compared canonically (matching ``evaluate_relation``), and
    the queried relation's declared surface variants are treated as the same
    predicate, so an alias never masquerades as an "other" relation. The listed
    relations are sorted deterministically and capped at *max_relations*.
    """
    if _predicate_of(draft) != "relation":
        return None
    args = query_args(draft)
    if len(args) != 3:
        return None
    s_arg, r_arg, _o_arg = args
    # A predicate-mismatch hint only makes sense for a concrete subject AND a
    # concrete queried predicate; a variable in either position has nothing to
    # compare against.
    if not (is_quoted_string(s_arg) and is_quoted_string(r_arg)):
        return None
    # SCOPE GATE (single source of truth with cmd_render): the hint is defined only
    # for a VERIFIED NEGATIVE engine answer. Reuse classify — exactly what render
    # branches on via decision["negative"] — so an accepted subject with an
    # unaccepted object (route=wiki, negative=False) or a positive answer never
    # emits the hint, keeping the machine (evaluate) and human (render) outputs on
    # the same contract with no drift.
    decision = classify(draft, facts)
    if decision["route"] != "engine" or not decision["negative"]:
        return None
    subject = arg_value(s_arg)
    # A verified negative already guarantees the subject is accepted; this canonical
    # membership check is a defensive guard (never fabricate a hint for an unknown
    # subject) aligned with the canonical counting below — so an amount/date
    # compound subject is matched consistently, not by raw string.
    #
    # "Defensive" is measured, not assumed, and the measurement is written down so the
    # next reader can check whether its PREMISE still holds rather than re-run it on
    # faith. The premise is structural: route == "engine" means classify_query returned
    # QUERY_OK or QUERY_FACT_ABSENT, and for a non-variable subject — which the
    # is_quoted_string gate above has already required — both are past
    # QueryVocabulary.accepts_subject, which tests canonical_value(subject) against
    # {canonical_value(e) for e in entity_set(facts)}: the same fold over the same set
    # the next line builds. Confirmed two ways over tests/unit at 279d37f (6237 passed,
    # 1 skipped both times): deleting the guard leaves the suite green, and replacing
    # its `return None` with a raise ALSO leaves it green — so the branch is not merely
    # unasserted, it is never taken. If classify's routing codes, entity_set's default
    # arguments, or canonical_value's identity on either side ever diverge from this
    # line's, that is what to re-check; the guard is cheap and stays either way.
    accepted_entities_c = {canonical_value(e) for e in entity_set(facts)}
    if canonical_value(subject) not in accepted_entities_c:
        return None
    subject_c = canonical_value(subject)
    queried_rels = {canonical_value(arg_value(r_arg))} | {
        canonical_value(v) for v in canonical_variants_of(arg_value(r_arg), relation_aliases())
    }
    other_relations: set[str] = set()
    other_facts = 0
    queried_facts = 0
    for row in facts:
        if canonical_value(row["subject"]) != subject_c:
            continue
        if canonical_value(row["relation"]) in queried_rels:
            queried_facts += 1
        else:
            other_facts += 1
            other_relations.add(row["relation"])
    # The subject HAS the queried relation (just not this object): an object
    # mismatch, not a predicate mismatch — no hint.
    if queried_facts:
        return None
    # Honest verified negative: the subject has no fact under any other relation
    # either. Preserve the "verified absence" value — emit nothing.
    if not other_relations:
        return None
    shown = sorted(other_relations)[:max_relations]
    listing = ", ".join(shown)
    if len(other_relations) > len(shown):
        listing += ", ..."
    return (
        f"note: no verified '{arg_value(r_arg)}' for '{subject}', but '{subject}' has "
        f"{other_facts} fact(s) under other relations (possible predicate mismatch): "
        f"{listing}"
    )


def did_you_mean_hints(draft: str, facts: list[dict[str, str]]) -> list[dict[str, object]]:
    """Return display-only hints for validator-confirmed vocabulary misses (#273).

    Only concrete relation arguments on stable entity/relation-not-accepted
    routes qualify.  This deliberately excludes verified negatives, malformed
    drafts, variables, review queries, candidate data, and source/wiki text.
    """
    decision = classify(draft, facts)
    if decision["code"] not in {QUERY_ENTITY_NOT_ACCEPTED, QUERY_RELATION_NOT_ACCEPTED}:
        return []
    if _predicate_of(draft) != "relation":
        return []
    args = query_args(draft)
    if len(args) != 3:
        return []
    aliases = relation_aliases()
    entities = entity_set(facts)
    relations = {row["relation"] for row in facts if row["relation"]} | set(aliases) | set(aliases.values())
    hints: list[dict[str, object]] = []
    for kind, arg, vocabulary in (
        ("entity", args[0], entities),
        ("relation", args[1], relations),
        ("entity", args[2], entities),
    ):
        if not is_quoted_string(arg):
            continue
        term = arg_value(arg)
        if any(canonical_value(value).casefold() == canonical_value(term).casefold() for value in vocabulary):
            continue
        suggestions = nearby_vocabulary(term, vocabulary)
        if suggestions:
            hints.append({"kind": kind, "term": term, "suggestions": suggestions})
    return hints


# `_reachable_pairs` — upstream's pure-python transitive closure for `path` —
# is deliberately NOT carried here. Answering a path query from a closure over
# accepted facts made ask return a VERIFIED NEGATIVE for a pair the engine had
# proved (an edge rule in logic-policy.extra.dl reaches pairs no closure over
# relation/3 can see), so ask and the report disagreed about one KB (#220). The
# path branch below asks the engine instead, and degrades to a signalled empty
# result rather than a faked negative.


def evaluate(draft: str, facts: list[dict[str, str]]) -> dict[str, object]:
    """Evaluate a validated engine query: relation, path, or a policy predicate.

    - relation: match against accepted facts.
    - path: a fully-quoted query returns the dependency path (or none); a query
      with a variable returns the reachable (start, target) pairs.
    - policy predicate: the inferred (entity, reason) rows from the engine,
      optionally filtered by a quoted entity argument.

    A truly unknown predicate raises NotImplementedError rather than returning 0
    rows, so a caller never mistakes an unsupported predicate for a verified
    negative.
    """
    predicate = _predicate_of(draft)
    args = query_args(draft)
    if predicate == "relation":
        rows = evaluate_relation(draft, facts)
        result: dict[str, object] = {"rows": rows, "count": len(rows)}
        # Optional, additive coverage hint (#189) for a verified-negative relation
        # query — never changes rows/count, only appended when informative.
        if not rows:
            hint = coverage_hint(draft, facts)
            if hint:
                result["coverage_hint"] = hint
        return result
    if predicate == "count":
        # count(subject, relation)? -> number of distinct objects (a verified
        # aggregate; 0 is a real answer). Rendered as a single value row.
        # When the relation arg is a quoted canonical name (surface_variants
        # non-empty), count DISTINCT objects across the canonical AND all its
        # surface variants — symmetry with the relation branch (#227).
        # Guard arity BEFORE unpacking: a count with != 2 args is malformed. Match
        # classify_query (BAD_ARITY) and raise the same NotImplementedError the
        # unknown-predicate fallthrough uses, so cmd_evaluate turns it into a clean
        # error JSON instead of an uncaught IndexError (< 2 args) or a silently
        # accepted, bogus count (> 2 args) (#257).
        if len(args) != 2:
            raise NotImplementedError("count query must have subject and relation arguments")
        # A count is a relation query with a free object, so it goes through the
        # SAME shared predicate as everything else (#213) — the report's count
        # branch does too. Its own copy compared subjects raw and so could drift.
        objects = {
            row["object"]
            for row in facts
            if relation_row_matches([args[0], args[1], "O"], row, relation_aliases(), value_hierarchy())
        }
        return {"rows": [[str(len(objects))]], "count": len(objects)}
    if predicate == "path":
        # Ask the ENGINE, exactly as the policy-predicate branch below does. Answering
        # from the python closure over accepted facts made ask return a VERIFIED
        # NEGATIVE for a pair the engine had proved -- an edge rule in
        # logic-policy.extra.dl reaches pairs no closure over relation/3 can see -- so
        # ask and the report disagreed about the same KB (#220). Degrade the same way
        # too: a signalled empty result, never a faked verified negative.
        try:
            inferred = run_wirelog()
        except Exception as exc:  # noqa: BLE001 — engine/loader raise non-FactlogError too
            return {"rows": [], "count": 0, "policy_unevaluable": str(exc)}
        rows = path_query_rows(args, facts, inferred["path"])
        return {"rows": rows, "count": len(rows)}
    if predicate in policy_predicates(_policy_program_optional()):
        # Engine evaluation of a policy predicate re-loads the policy program AND
        # runs pyrewire (common.run_wirelog): a hand-authored logic-policy.extra.dl
        # can make this fail loud in TWO ways the routing-time loader guard does
        # NOT cover — a FactlogError (e.g. an unscaled `number` threshold,
        # _assert_no_unscaled_number_threshold) or a pyrewire ParseError from
        # broken .dl syntax (NOT a FactlogError, so run_cli would not catch it and
        # ask would crash with a traceback). ask is exploratory and must never
        # hard-fail (#193). Degrade to a signalled empty result the callers surface
        # as a warning instead of a verified answer (rendering [] here would fake a
        # verified negative). Catch broad Exception — never BaseException, so
        # KeyboardInterrupt/SystemExit still propagate — because the engine may
        # raise non-FactlogError types.
        try:
            inferred = run_wirelog()
        except Exception as exc:  # noqa: BLE001 — engine/loader raise non-FactlogError too
            return {"rows": [], "count": 0, "policy_unevaluable": str(exc)}
        rows = [
            list(row)
            for row in sorted(inferred.get(predicate, set()))
            if policy_row_matches(args, row)
        ]
        return {"rows": rows, "count": len(rows)}
    raise NotImplementedError(f"engine evaluation of predicate '{predicate}' is not supported")


def render_engine_answer(
    draft: str,
    rows: list[list[str]],
    signals: dict[tuple[str, str, str], dict[str, object]] | None = None,
    annotate_objects: bool = False,
    limit: int | None = DEFAULT_RENDER_ROW_LIMIT,
    project: bool = True,
) -> str:
    """Render the VERIFIED — engine answer block (positive or negative).

    The literal marker 'VERIFIED — engine' is the greppable verification token.
    The engine verdict is BINARY — a row is verified or it is not; it carries no
    probability. The annotations below describe the *evidentiary basis* of a
    verified row, never the certainty of the verdict:

    - A relation row backed by an extracted candidate is annotated with
      '(sources: N, extraction conf: C)' — the distinct-source count and the
      LLM's source->fact *extraction* confidence (a candidate-stage trust signal,
      NOT a confidence in the engine verification) — plus '[stale: source
      missing]' when a backing source has vanished, with backing source path(s)
      listed beneath ('    ← <source>').
    - A relation row with NO backing extraction (no signal entry) carries no
      extraction confidence, so it is marked '[no extraction backing]' rather
      than left ambiguous. Today accepted.dl is a 1:1 projection of the
      candidates table and no rule derives relation atoms, so this only arises
      when the two are out of sync (recompile via /factlog check); it would also
      cover a future rule-derived relation. Either way the verdict stays binary.

    Non-relation predicates (path/count/policy) pass signals=None and
    annotate_objects=False: their rows are computed by the engine, carry no
    extraction confidence by construction, and are rendered without annotation.
    Both the signals annotation and the humanize annotation are gated to relation
    rows via these flags; a coincidental 3-element shape on a path/policy row
    never triggers either annotation.
    """
    lines = ["VERIFIED — engine", f"query: {draft}", f"rows: {len(rows)}"]
    if rows:
        visible_rows = rows if limit is None else rows[:_render_limit(limit)]
        projection = _single_column_projection(visible_rows) if project else None
        if projection:
            varying_index, fixed_columns = projection
            fixed = ", ".join(f"[{index}] {value}" for index, value in fixed_columns)
            lines.append(f"  - rows differ only at column {varying_index}; fixed: {fixed}")
        for row in visible_rows:
            line = (
                f"    - {row[projection[0]]}"
                if projection else f"  - {', '.join(row)}"
            )
            # Display-only: annotate a compound-term object (amount/date/number)
            # with its human-friendly form. Gated to relation rows via
            # annotate_objects so a coincidental 3-element shape on a path/policy
            # row is never annotated. The stored/canonical string stays in the row
            # verbatim (still copy-paste queryable); the pretty form is appended,
            # never substituted. No-op for plain objects (#188 follow-up).
            if annotate_objects and len(row) == 3:
                pretty = literal_types.humanize(row[2])
                if pretty != row[2]:
                    line += f"  (= {pretty})"
            sig = signals.get((row[0], row[1], row[2])) if signals is not None and len(row) == 3 else None
            if sig:
                line += f" (sources: {sig['sources']}, extraction conf: {sig['confidence']})"
                if sig.get("stale"):
                    line += " [stale: source missing]"
            elif signals is not None and len(row) == 3:
                # A relation answer is expected to have an extraction-backed signal
                # per row. A row without one carries no extraction confidence:
                # today that means candidates.csv/accepted.dl are out of sync
                # (accepted.dl is a 1:1 projection of the candidates table — no
                # rule derives relation atoms yet); it would also cover a future
                # rule-derived relation. Mark the absence; the verdict stays
                # binary (the row IS verified).
                line += " [no extraction backing]"
            lines.append(line)
            if sig:
                for path in sig.get("source_paths", []):
                    lines.append(f"    ← {path}")
        truncation = _truncation_line(len(rows), len(visible_rows))
        if truncation:
            lines.append(truncation)
    else:
        lines.append("no such fact (verified negative)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Path B — wiki exploration (UNVERIFIED)
# ---------------------------------------------------------------------------
# The wiki corpus is the user's source text ONLY: sources/ (originals) and
# runs/sources/ (text conversions of binary originals). pages/ is DELIBERATELY
# EXCLUDED — it is engine-derived from candidates.csv (including needs_review /
# candidate rows), so grepping it would re-surface facts the engine never
# accepted, leaking candidate vocabulary into an answer as if it were knowledge.
WIKI_SOURCE_DIRS = ("sources", "runs/sources")
# decisions/ (human review notes / open questions) is searched as clearly-labeled
# SUPPLEMENTARY context — useful for an unanswered question, but tagged so it is
# never conflated with source ground truth, and ranked below every primary excerpt
# (see _GRADE_* below). pages/ stays excluded entirely.
WIKI_SUPPLEMENTARY_DIRS = ("decisions",)
_EXCERPT_WINDOW = 3

# Stands where an excerpt jumps from a front-matter match to the document's first
# body line (#574) — the ONE place an excerpt is not a contiguous slice, so the
# discontinuity is shown rather than left for the reader to infer from a citation
# line number that no longer describes the whole excerpt.
#
# It carries no word and no number ON PURPOSE. The excerpt string is what
# `_keyword_hits` scores, so any token in this marker is scored as if the source
# had written it: a marker reading '… (line 18)' self-matches a question about
# 'line' — or about '18', since a 2-character ASCII token is a keyword (#583) —
# and every front-matter excerpt in the corpus then gains coverage the source text
# does not have. U+2026 is one character and is neither ASCII nor CJK, so no
# keyword pattern can reach it.
_EXCERPT_ELISION = "…"

# Directory grade — the TOP element of the ranking key (#572). Before this the
# grade lived only in the display label, so a review note could (and on the
# reference KB did) take rank 1 over the source text it was reviewing. Only the
# ORDER of these two values matters; neither is ever displayed.
_GRADE_PRIMARY = 1
_GRADE_SUPPLEMENTARY = 0


def _wiki_corpus() -> list[tuple[str, str, int]]:
    """(relative dir, display label, ranking grade) for the wiki search, primary
    first.

    The grade is derived here, from the same two constants that build the labels,
    so adding a directory to WIKI_SUPPLEMENTARY_DIRS moves its RANKING together
    with its label — there is no second list of directory names to keep in sync.
    """
    corpus = [(rel, rel, _GRADE_PRIMARY) for rel in WIKI_SOURCE_DIRS]
    corpus += [(rel, f"{rel} (supplementary)", _GRADE_SUPPLEMENTARY) for rel in WIKI_SUPPLEMENTARY_DIRS]
    return corpus


def _is_cjk(word: str) -> bool:
    """True if *word* contains a Hangul / CJK / kana character."""
    return any(
        "가" <= ch <= "힣"  # Hangul syllables
        or "一" <= ch <= "鿿"  # CJK unified ideographs
        or "぀" <= ch <= "ヿ"  # Hiragana + Katakana
        for ch in word
    )


# 지시 표현(demonstratives), built as a PRODUCT of two closed classes instead of being
# listed form by form (#586).
#
# Why a product and not more literals. The stop-word list below is a closed enumeration
# of surface 어절, and #571 already said widening it needs a rule. Measured on the list
# as it stood after #581, the enumeration covered '이것'/'여기' but not '이곳'/'그때'/
# '이쪽'/'이만큼' — so '이곳은 무엇인가' kept '이곳은' as its only keyword, matched
# nothing, and was reported as '(no matching source excerpts found)': the reader is told
# the KB is silent on a topic they never named. Four of #586's seven reported 어절 still
# reproduced that on 96ea1ae; the other three ('이것을', '여기에', '저것을') were closed
# by #581's stem guard, because THEIR stems were on the list and these four were not.
#
# So the missing piece is stems, and stems are where a rule exists. 지시 관형사 is a
# closed class of exactly three (이/그/저), and the 의존명사·대용 어간 that FUSE with
# them into one 어절 form a small closed class too. Their product is generated whole
# rather than curated, which is what makes the 계열 symmetry structural: no 계열 can be
# silently missing a row, because no row is written by hand. Rare members ('저때',
# '이만치') are kept for exactly that reason — dropping them would put a curator back in
# the loop, and their measured reach over the searchable corpus is 0 files each.
#
# The product supplies STEMS; #581's _korean_stem supplies the 조사. Composed, the two
# handle "지시 표현 + 임의 조사" as one rule: '이곳에서'→'이곳', '그쪽으로'→'그쪽',
# '저만큼은'→'저만큼' all reduce onto a member and are dropped, without any of those
# surface forms being written down. That is why this closes a productive suffix class
# that no enumeration could.
#
# Matching stays WHOLE-TOKEN (on the 어절, then on its stem), NOT prefix. A prefix rule
# was the other candidate #586 named, and it is what the issue's counterexamples are
# aimed at. Read at the DETERMINER (1 character, which is how '이론'/'이유' become
# counterexamples at all), it is measurably destructive: over the corpus search()
# actually reads (127 files), 10 distinct Korean 어절 begin with 이/그/저 — '이론',
# '이론연구', '이름', '이후', '저널', '저자', '저la', '이니셜', '이를',
# '이형접합_삽입_검출_비율' — and a 1-character prefix rule drops all 10, costing 76
# file-reaches, 66 of them '저널' alone.
#
# Read at the 2-character STEM the damage is NOT visible on the corpora available here,
# and that is worth saying plainly rather than implying otherwise: over the wider
# vocabulary described below, a stem-prefix rule drops 13 어절 that this rule keeps, and
# every one of them is demonstrative-derived ('이것들', '여기까지는', '그쪽이고'), a
# conjunction built on one ('그런데'), or a file name ('여기서-갈리는가.md') — measured
# cost in content nouns, zero. The objection to it is structural instead: the remainder
# after the stem is unconstrained, so any noun that happens to open with one of these
# bigrams is swallowed, '저기압' (低氣壓) being the readiest example. Whole-token
# equality cannot have that failure — the remainder after the determiner must BE a bound
# noun, and '론'/'유'/'기압' are not — so it is preferred on a bound worst case, not on a
# measured difference. '저기압' is pinned as a test for exactly that reason.
#
# Measured cost of the rule as written, over the same 127 files: 0 of their 496 distinct
# Korean 어절 change their result, and the 59 title-control questions (every source's own
# `title:` asked back as a question) reach exactly the files they reached on 96ea1ae.
# Every product member's own prose reach there is 0 files (only '여기' reaches 1, and it
# was already listed). Over a wider Korean vocabulary — the KB's non-searchable trees
# (pages/, facts/, policy/, runs/, decisions/, source-provenance/) plus this repo frozen
# at 96ea1ae, 2,149 files and 9,213 distinct 어절 — the rule newly drops 9 어절, and all
# 9 are demonstratives: 그때, 그때는, 그만큼, 그쪽이, 이곳, 이곳에서, 이때, 이때는, 이쪽은.
#
# KNOWN GAP, measured not assumed: plural '들' is not a listed 조사, so '이것들은' keeps
# its 어절 ('이것들') and stays a keyword. Closing it means either iterative stripping —
# which #581 refused with a measurement — or a '들' entry in the suffix table, which is
# #581's table, not this rule. 요/고/조 계열 ('요것', '고것') are likewise out: they are
# spoken-register forms and the determiner class here is the written one.
_CJK_DEMONSTRATIVE_DETERMINERS = ("이", "그", "저")
_CJK_DEMONSTRATIVE_BOUND_NOUNS = ("것", "거", "곳", "쪽", "때", "런", "렇게", "만큼", "만치")
_CJK_DEMONSTRATIVES = frozenset(
    {
        determiner + noun
        for determiner in _CJK_DEMONSTRATIVE_DETERMINERS
        for noun in _CJK_DEMONSTRATIVE_BOUND_NOUNS
    }
    # 장소 대명사는 곱으로 나오지 않는다: '여기'의 '기'는 의존명사가 아니라 굳어진 형태다.
    | {"여기", "거기", "저기"}
)


# Korean question function words, as whole 어절 (whitespace-token) SURFACE forms.
#
# Why this list exists (#571): every CJK token of length>=2 is promoted to a content
# keyword, so the grammar of the question becomes search terms. A question ending
# '…주장하는 논문은?' then substring-matches '이 논문은 …철회(retracted)되었다' in a
# topically unrelated retraction notice and ranks it as evidence — noise on an
# exploration surface is tolerable, but citing a retracted trial as a candidate
# answer is the worst failure mode available to a verification tool. These forms
# only CONSTRUCT a question; they never name its subject.
#
# Membership rule (#571 기준 4): surface forms only, never a bare stem. '논문' and
# '방법' are legitimate content nouns and stay searchable; only the particle-attached
# 어절 that a question frame produces ('논문은') is dropped. Matching is therefore on
# the WHOLE token — a substring or suffix rule would swallow content words, e.g. a
# query for '반박논문은' ends in '논문은' but names a real subject.
#
# A form is listed only if it is a function word AS A STANDALONE 어절. Copular endings
# that only ever appear ATTACHED ('-인가', '-인지') are therefore NOT listed: alone,
# '인지' is 인지(cognition) and '인가' is 인가(認可 / 전압 인가) — content nouns in the
# very literature a KB like this holds. Their attached forms ('무엇인가', '무엇인지')
# are listed individually instead.
#
# Single-character forms ('왜', '이', '그') are deliberately absent: the len>=2 floor
# below already drops them, so listing them would only imply a guarantee this filter
# does not provide.
#
# The literals below are a CLOSED enumeration, not a morphological rule: unlisted
# question forms exist (요청형 '알려줘', 의존명사 '대한', 동사+의문어미 '제시하는가') and
# still become keywords. Widening THAT needs an analyzer, not more literals — that is
# #581's ground. One class has since been lifted out of the enumeration and into a rule:
# 지시 표현 come from _CJK_DEMONSTRATIVES above, so this list no longer spells '이것',
# '그런', '이렇게' or their 조사 rows at all. The union is what callers see; the split is
# about which half a new form belongs in.
_CJK_QUESTION_STOPWORDS = frozenset(
    {
        # 의문사 + 그 조사/어미 표층형
        "무엇",
        "무엇이",
        "무엇을",
        "무엇인가",
        "무엇인지",
        "무엇인가요",
        "무엇에",
        "뭐가",
        "뭐야",
        "뭔가",
        "언제",
        "언제부터",
        "언제까지",
        "언제인가",
        "어디",
        "어디에",
        "어디서",
        "어디에서",
        "어디까지",
        "어디인가",
        "어떻게",
        "어떤",
        "어떠한",
        "어느",
        "누가",
        "누구",
        "누구인가",
        "누구인가요",
        "얼마나",
        # 의문형 종결부. 독립 어절로 쓰였을 때 술어를 지시하지 않는 것만 넣는다.
        "있나",
        "있는지",
        "있나요",
        "있는가",
        "없나",
        "없나요",
        "없는가",
        "맞나",
        "맞는가",
        # 지시/대용어 중 곱으로도 어간 분리로도 나오지 않는 것만 남는다. '여기서'류의
        # '서' 는 _KOREAN_TRAILING_SUFFIXES 에 없고, 넣을 수도 없다: 위 주석과 같은 넓은
        # 어휘(9,213 어절)에서 측정하면 '서' 항목 하나가 36 어절의 어간을 바꾸고, 그중
        # '보고서'→'보고', '제안서'→'제안', '각문서'→'각문' 처럼 문서 종류를 가리키는
        # 명사가 머리를 잃는다. 그래서 이 세 형태는 표층형으로 적는다.
        "여기서",
        "거기서",
        "저기서",
        # 질문 틀이 만들어 내는 총칭 명사의 조사 표층형. 어간('논문')은 콘텐츠
        # 명사이므로 목록에 없다 — 사용자가 '논문' 을 그대로 치면 키워드로 남는다.
        # 실측(실제 KB, sources/ 의 읽을 수 있는 파일 186개): '논문은' 이 걸리는 파일은
        # 단 1개이고, 그 1개가 질문과 주제 접점이 0인 철회 공지다. 변별력이 없는 게
        # 아니라, 이 표층형이 변별하는 대상이 오답 하나뿐이다.
        "논문은",
        "논문이",
        "논문을",
        "논문인가",
    }
    | _CJK_DEMONSTRATIVES
)


def _cjk_stopword(word: str) -> bool:
    """True if *word* is a Korean question function word (whole-token match).

    The list holds precomposed (NFC) forms, and so does the comparison. A decomposed
    (NFD) question does not reach this branch at all — _is_cjk tests the 가-힣
    syllable block, which NFD jamo are not in — but it is NOT thereby unfiltered:
    the NFD token falls through to the ASCII branch and still becomes a pattern. So
    this filter's contract holds for NFC input only. Harmless today (the corpus and
    the CLI's own input are NFC); closing it means normalizing the question before
    tokenizing, which changes matching for every non-NFC token, not just stop words.
    """
    return word in _CJK_QUESTION_STOPWORDS


# Korean CJK tokens shorter than this never become keywords, and neither does a stem
# stripped down past it (#581 수용 기준 3). ONE constant for both because they are one
# rule: whatever length is too short to be a search term when the user typed it is too
# short when a suffix rule produced it. The floor is what makes the stripper below safe
# at all — '평가'→'평', '국가'→'국', '온도'→'온', '길이'→'길', '종이'→'종' are all
# rejected here, so the 2-character nouns that end in a particle SYLLABLE never lose it.
_CJK_MIN = 2

# Trailing Korean 조사·어미, stripped from a question 어절 to get its stem (#581).
#
# Why this is needed at all: CJK keywords match by SUBSTRING, and the docstring of
# _keywords used to justify that as tolerating attached particles — '근거' matches
# '근거는'. That is true in exactly one direction, question-stem → document-particle,
# and a natural Korean question is almost always the other one: it is the QUESTION
# that carries the 조사. Measured on origin/main, '해석가능성에서' does not match a
# document writing '해석가능성', '주장하는' does not match '주장', '근거를' does not
# match '근거 로그는 최소 5년 보존한다'. Stripping the suffix makes the matcher's
# reach a SUPERSET of the surface form's — 'X' matches every line 'X<조사>' does —
# so this closes the missing direction without giving up the one that worked.
#
# Why these entries, and not others. Three groups, each admitted for a stated reason:
#
#  1. 격조사·보조사 that attach to the noun a question is ABOUT ('근거를', '방법은',
#     '모델에서'). This is the group the issue is named for.
#  2. Stacked forms ('에서는', '으로서', '에게는'). They are listed as whole strings
#     rather than stripped iteratively: one pass removes at most ONE entry, so the
#     worst over-strip this table can cause is bounded by the worst SINGLE entry and
#     can be measured entry by entry. Iterating would compound — '중요도가' would
#     lose '가' and then '도' and land on '중요', a word the question never contained.
#  3. 어미 of the 하-/되- verb frames a question predicate is built from ('주장하는',
#     '제시하는가', '분석한'). These reduce to the noun root ('주장', '제시', '분석'),
#     which is what the document actually spells in any of its inflections — '주장하는'
#     as a literal misses '주장한다' and '주장했다' alike, '주장' hits all three.
#
# What is deliberately NOT here: any rule that is not a literal suffix. Korean 조사·
# 어미 are productive and no enumeration closes them (#571's stop-word list says the
# same about its own membership), so this table is a bounded improvement, not a
# morphological analyzer — which the issue puts out of scope because an analyzer means
# a model or a dictionary, and both break the offline/deterministic CI contract that
# keeps _semantic_rerank opt-in.
#
# KNOWN, MEASURED over-strip. An entry that is also the last syllable of a real noun
# mis-strips it, and the floor above only rescues the 2-character cases:
#   '가': the agentive suffix — '전문가'→'전문', '이론가'→'이론'
#   '이': '어린이'→'어린'
#   '도': the degree suffix — '정확도'→'정확', '신뢰도'→'신뢰', '중요도'→'중요'
# They are kept because the cost is bounded and one-sided: substring matching makes
# the stem's match set a SUPERSET, so a mis-strip can only ADD documents, never hide
# the ones the surface form found. Measured over the reference KB's 494 distinct Korean
# 어절 of length>=_CJK_MIN, against the 127 files search() actually reads: 99 어절 (20%)
# get a different matcher, and the files they reach go up by a median of 0 (84 of them
# add nothing at all), a mean of 1.48, and a maximum of 67.
#
# That corpus DOES contain mis-strips — 17 found by reading all 99 pairs, among them
# '오메가'→'오메', '서베이'→'서베', '몬테카를로'→'몬테카를', '폴딩속도'→'폴딩속', and
# a set of verb inflections that leave a fragment rather than a root ('쓰지만'→'쓰지').
# Their measured added-file cost is 0, all 17 of them: the fragment is a prefix of the
# word it came from, so on this corpus it reaches the same file and no other. The whole
# +1.48 comes from CORRECT strips, and both large ones ('코퍼스에'→'코퍼스' +67,
# '초록이'→'초록' +66) land on one boilerplate comment line the demo corpus repeats in
# 67 files — reach the bare forms always had, so it is a property of that corpus rather
# than something the stripper invented.
#
# Dropping '가'/'이'/'도' would cost the most common question frames in Korean
# ('무엇이', '차이가', 'X도 있는가') to buy back at most 1 file per mis-strip
# ('이론가'→'이론' +1, '정확도'→'정확' +1, '신뢰도'→'신뢰' +1, and 전문가/소설가/
# 정치가/어린이/중요도/민감도 all +0). The mis-strips above are pinned as tests so a
# future widening cannot lose them.
_KOREAN_TRAILING_SUFFIXES = tuple(
    sorted(
        {
            # 1. 격조사 / 보조사
            "이", "가", "을", "를", "은", "는", "의", "에", "도", "만",
            "로", "와", "과", "에서", "에게", "에는", "에도", "으로",
            "부터", "까지", "처럼", "보다", "마다", "조차", "마저",
            # 2. 스택된 표층형
            "에서는", "에서도", "에서의", "으로는", "으로도", "으로서", "으로써",
            "로서", "로써", "로는", "로도", "에게는", "에게서", "와는", "과는",
            "와의", "과의", "라도", "이라도", "라는", "이라는", "라고", "이라고",
            # 3. 하-/되- 용언 프레임의 어미, 그리고 서술격 '이다' 계열
            "하는", "하는가", "하는지", "하나", "하다", "한다", "하고", "하여",
            "해서", "했나", "했는가", "한", "되는", "되는가", "되나", "된다",
            "된", "인가", "인지",
        },
        key=lambda suffix: (-len(suffix), suffix),
    )
)


def _korean_stem(word: str) -> str:
    """*word* with one trailing 조사/어미 removed, or *word* unchanged.

    The LONGEST matching entry decides, and it decides ALONE: if the stem it leaves
    is not usable, the 어절 keeps its surface form rather than falling through to a
    shorter entry. Falling through is what a first draft did, and it manufactured
    keywords out of grammar — '이라는' has no usable stem under '이라는' itself, and
    the fall-through then cut '는' off and produced '이라'; '것으로' produced '것으'.
    Under one deciding entry those tokens are left alone, which is the honest answer:
    the table matched the whole 어절, so the 어절 named no subject.

    Three ways a stem is unusable, each measured rather than assumed:

    - shorter than _CJK_MIN. This is 수용 기준 3 and it is what keeps '평가'→'평',
      '국가'→'국', '온도'→'온' from happening.
    - nothing CJK left. _is_cjk is `any()`, so 'ai가' reaches this branch; stripping
      it would hand the CJK branch the bare token 'ai', and that branch matches by
      SUBSTRING with no word boundaries — 'ai' would match 'available', 'training',
      'chain'. The ASCII branch's lookarounds exist precisely to prevent that (they
      are why 'api' does not match 'therapist'), so an ASCII-only stem is refused.
    - the stem is ITSELF an entry in the table. Then the 어절 is two stacked 조사/
      어미 and no content: '하는가' would otherwise yield '하는' and '으로만' would
      yield '으로', each of which substring-matches a large share of ordinary Korean
      prose and adds a near-constant to every document's coverage. Measured on the
      wiki fixture corpus before this rule was added, '전문가 평가는 어떻게 하는가'
      returned 3 files on '하는' alone against the 1 the question is about.
    """
    for suffix in _KOREAN_TRAILING_SUFFIXES:
        if not word.endswith(suffix):
            continue
        stem = word[: -len(suffix)]
        if len(stem) < _CJK_MIN or not _is_cjk(stem) or stem in _KOREAN_TRAILING_SUFFIXES:
            return word
        return stem
    return word


# ASCII tokens of 2+ characters become keywords, so a two-letter CONTENT initialism
# ('AI', 'ML', 'RL', 'QA', 'NN') is a search term like any other (#583). It is only
# 1-character tokens that are dropped for length.
#
# The floor used to be 3, on the reasoning that at two characters the English function
# words are the highest-reach tokens in an English corpus. Measured, length was never
# what bounded reach. All figures below are over the corpus search() ACTUALLY reads —
# 127 files, being sources/ + runs/sources/ + decisions/ minus the 60 sync-ignored
# files under sources/arxiv_corpus/** — and count files whose PROSE a token's own
# matcher hits. PROSE means the BODY BELOW the front-matter fence, and the word has to
# be read that way or the table does not reproduce: counting whole files instead moves
# 'ai' from 15 to 26 (arXiv front matter carries `cs.AI`) and 'and' from 123 to 126,
# while leaving every other figure here unchanged.
#
#   already admitted at 3 chars:  and 123  the 120  for 110  doi 103  are 67
#   best 2-char NOT on the list:  10 105   11 18    12 18    il 17    ai 15
#
# The three highest-reaching tokens the old floor already let through beat the highest
# 2-character one, so lowering the floor adds no reach class the default path did not
# already carry. Bounding the 2-character class is the function-word list's job, and
# that list keeps working at the lower floor.
#
# The floor is NOT thereby inert, and reading it as inert is how it gets deleted. It is
# the only guard on the 1-character class, and that class outreaches everything the
# list does not name: by the same measurement 'a' reaches 116 files — above the best
# unlisted 2-character token ('10', 105) and above 'for' (110) — and '1'/'2'/'3' reach
# 76/67/70. The list cannot absorb them, because every entry in it is exactly 2
# characters (tests/test_ask_router.sh asserts that as its own row). Measured: with
# _ASCII_MIN at 1, 'what is a benefit of ai in 3 systems' tokenizes to
# [what, a, benefit, ai, 3, systems] instead of [what, benefit, ai, systems]; with the
# list emptied and the floor left at 2 it tokenizes to [what, is, benefit, of, ai, in,
# systems]. The two guards exclude disjoint classes and neither stands in for the other.
#
# The same measurement answers the digit/fragment worry (#583 기준 3) without a further
# guard: after 10 the unlisted 2-character tokens fall off a cliff (18, 18, 17, 15,
# 13 …), and the numerals among them are ordinary prose numbers, not a distinct noise
# source.
#
# Reach must be read as PROSE reach, and against THIS corpus. Ranked by TOTAL reach
# over the raw file tree the top unlisted token looks like `id` at 60 — but its prose
# reach is 0 (all 60 are the one-line sync comment `<!-- factlog sync … | id:… -->`)
# AND all 60 files are sync-ignored, so its reach here is 0 by two independent routes.
# 19 tokens in this corpus are metadata-only (07=59, 49=25, 44=6 …); a total-reach
# ranking would have mistaken every one of them for a recall risk.
_ASCII_MIN = 2

# 2-character English function words, excluded from the keyword set. Same purpose as
# _CJK_QUESTION_STOPWORDS in the other language: the floor above admits a short CONTENT
# initialism ('AI', 'ML', 'QA'), and by length alone those are indistinguishable from
# 'of'/'in'. Measured over the same 127 searchable files, by prose reach: of 124,
# in 123, to 116, is 88, we 86, by 84, on 84, as 79, an 66, be 50, or 45, at 43 —
# against ai 15, the best-reaching content initialism. Promoting one of these makes the
# question's ENGLISH grammar the entire query, which is #571's defect in another
# language: measured, '이것은 무엇인가 of 그것은' returned 451 excerpts on 'of' alone.
#
# EVERY entry is exactly 2 characters. Under the old floor of 3 that made the list
# unreachable from the default path and only a constraint on the recovery stage; since
# #583 lowered the floor to 2 the list is LOAD-BEARING on the default path — it is now
# the only thing keeping 'of'/'in' out of an ordinary question — while the invariant
# still bounds what the list may remove: at exactly 2 characters it cannot silently
# swallow a longer word. Longer English function words ('the', 'and', 'which') are
# keywords today and stay that way; changing that is a recall decision for the English
# side, not part of #571 or #583.
_ASCII_FUNCTION_WORDS = frozenset(
    {
        "am", "an", "as", "at", "be", "by", "do", "he", "if", "in", "is", "it",
        "me", "my", "no", "of", "on", "or", "so", "to", "up", "us", "we",
    }
)


def _tokenize_keywords(question: str) -> list[tuple[str, re.Pattern[str]]]:
    """The single token→(term, matcher) pass behind _keywords.

    Korean question function words are always dropped — there is no mode that keeps
    them (#571 기준 2) — and so are the 2-character English ones. There is exactly
    ONE pass: #571 added a second, relaxed-floor stage to rescue a short initialism
    when the first came back empty, and #583 removed it by letting the default floor
    admit those initialisms outright. A stage that re-ran this function with the same
    floor could not change any result, and leaving it in would advertise a recovery
    path that recovers nothing.

    The surface term is carried ALONGSIDE its compiled matcher, never re-derived
    from `pattern.pattern`: the ASCII branch wraps the token in lookarounds, both
    branches re-escape it, and since #581 the CJK branch may have stripped a 조사
    off it — so unwrapping that string back into what the user typed is a parser
    for this function's own output, and for one of the three it is not even
    invertible. The recall report (#575) prints these terms, and printing
    `(?<!\\w)neurosymbolic(?!\\w)` at a human is not a report.
    """
    # What has already claimed a matcher — the MATCHED string, not the surface token.
    # Since #581 those differ: '근거를' and '근거가' in one question reduce to the same
    # stem '근거', and admitting both would register two keywords for one query concept.
    # _keyword_hits counts distinct terms, so the row that happens to contain '근거'
    # would score coverage 2 against a row that covers two genuinely different words —
    # and frequency would count every occurrence twice on top of that. Keying on the
    # matcher makes ONE stem ONE keyword, which is #581 수용 기준 5 with no change to
    # _excerpt_score's shape. The surface term of the FIRST 어절 is what gets carried,
    # so the recall report (#575) still prints something the user typed.
    # A CJK stem and an ASCII token can never collide here: _korean_stem refuses a
    # strip whose result has no CJK character left.
    seen: set[str] = set()
    keywords: list[tuple[str, re.Pattern[str]]] = []
    # Tokenizer captures programming-term punctuation: internal '.'/'-' (node.js,
    # 도구가) and trailing '+'/'#' (c++, c#, f#), while excluding trailing
    # sentence punctuation. Plain \w runs (incl. CJK) still tokenize as before.
    for word in re.findall(r"\w+(?:[.+#-]+\w+)*[+#]*", question.lower(), flags=re.UNICODE):
        if _is_cjk(word):
            # Stop-word removal runs on the RAW 어절, and a token it drops produces
            # NO pattern at all — not even a derived one. #581's 조사 stripper is
            # therefore BELOW this guard (inside the len>=_CJK_MIN branch): run
            # above it, '논문은' would first become the stem '논문' — 67 of 186
            # measured source files, against 1 for the surface form — and #571 would
            # get wider instead of fixed. No stage of _keyword_patterns bypasses
            # this guard, so there is no second path a stripper could slip through.
            if _cjk_stopword(word):
                continue
            if len(word) < _CJK_MIN:
                continue
            term = _korean_stem(word)
            # The guard again, on the STEM. The list is defined over standalone 어절
            # surface forms, so a stem that IS one of those forms proves the 어절 was
            # that function word carrying a particle: '이것을' → '이것', '여기에' →
            # '여기'. Without this the stripper would take a function word the raw
            # guard happens not to spell and make it WIDER than it was — pattern
            # '이것' reaches every line pattern '이것을' reached and more — which is
            # #571's defect re-entering through the new code path. Content words are
            # not at risk from it: matching stays whole-token, so '반박논문은' → the
            # stem '반박논문', which is not a listed form and stays a keyword.
            #
            # This guard is also the half of #586's fix that lives in code. #586's other
            # half is _CJK_DEMONSTRATIVES, which supplies the STEMS; this line is what
            # turns "지시 어간" into "지시 어간 + 임의 조사" without either side spelling
            # a single inflected form. Neither half works alone: on 96ea1ae the guard
            # was already here and '이곳은' still became a keyword, because '이곳' was
            # not a listed stem.
            if term != word and _cjk_stopword(term):
                continue
            if term in seen:
                continue
            seen.add(term)
            keywords.append((word, re.compile(re.escape(term))))
        elif len(word) >= _ASCII_MIN and word not in _ASCII_FUNCTION_WORDS:
            if word in seen:
                continue
            seen.add(word)
            # Lookaround boundaries (not \b) so punctuation-edged tokens like
            # 'c++' / 'c#' match while 'api' still does not match inside
            # 'therapist'.
            keywords.append((word, re.compile(rf"(?<!\w){re.escape(word)}(?!\w)")))
    return keywords


# Emitted when the question yields no keyword at all. It must NOT read like the
# corpus was searched and came back empty: nothing was searched for, because the
# question named no subject. Conflating "I have no such source" with "you asked me
# nothing searchable" tells the reader the KB is silent on a topic they never
# actually named — the same class of unreported retrieval failure as #575.
#
# The stated cause must cover EVERY way the keyword set empties, or it misdiagnoses
# the user's question for them. There are exactly two such ways, and a question can
# hit them in combination ('이 논문은?' is one of each):
#   - a function word: a listed Korean 어절, or a 2-letter English function word
#   - a single-character token: dropped by the length floor in either script
# Naming only the first told someone who typed '왜?' to stop using function words.
#
# Both causes are now decided on ONE pass (#583 removed the relaxed-floor recovery
# stage), so this note fires exactly when that pass yields nothing — there is no
# second stage that could refill the set after the diagnostic's premise was formed.
# The counterpart wording, '(no matching source excerpts found)', is emitted only
# when there WAS a keyword and the corpus had no line for it; the two remain
# mutually exclusive because the keyword set is either empty or it is not.
NO_QUERY_TERM_NOTE = (
    "(no searchable keyword in the question: question function words and "
    "single-character tokens do not become keywords, and nothing else was left. "
    "This is NOT 'no such source' — the corpus was not consulted for any term. "
    "Rephrase with a content word.)"
)


def _keywords(question: str) -> list[tuple[str, re.Pattern[str]]]:
    """The question's keywords as (surface term, matcher) pairs, bilingual:

    - ASCII words (len>=2): word-boundary match — avoids substring false positives
      (e.g. 'api' in 'therapist'), and lets a two-letter initialism ('AI', 'QA') be
      a keyword on this path rather than only in a recovery stage (#583).
    - CJK words (len>=_CJK_MIN): substring match on the token's STEM — one trailing
      조사/어미 is removed first (#581, _korean_stem). Substring alone only ever
      tolerated the particle on the DOCUMENT side ('근거' matches '근거는'); a real
      Korean question carries the particle on the QUESTION side, and there substring
      fails structurally ('근거를' misses '근거'). Stripping restores the missing
      direction and keeps the other, because the stem's match set contains the
      surface form's. CJK compounding has no word delimiters, so a 2-char query can
      substring-match inside an unrelated compound, and a stem is by construction no
      longer than the 어절 it came from; this recall-over-precision trade-off is
      acceptable for the UNVERIFIED exploration surface, but do NOT reuse this
      matcher on a precision-sensitive path.
    - Korean question function words (_CJK_QUESTION_STOPWORDS) are dropped, so the
      grammar of the question does not become a search term (#571).

    Returns [] for a question made only of function words. That is deliberate
    (#571 기준 2, revised): search() then returns nothing and the caller reports
    NO_QUERY_TERM_NOTE. Restoring the function words instead — the earlier
    behaviour — reproduced the exact defect this filter exists to remove: measured
    on the real KB, '이 논문은?' came back citing a retracted trial as its only
    evidence, because that notice is the one file in 186 containing '논문은'.

    'AI 논문은 어디에 있나' keeps 'ai' — but by the floor, not by a fallback: the
    recovery stage #571 needed for that question is gone (#583), so a question whose
    every token is a function word or a single character now empties the set on the
    only path there is. That is what keeps NO_QUERY_TERM_NOTE's two stated causes
    exhaustive.
    """
    return _tokenize_keywords(question)


def _keyword_patterns(question: str) -> list[re.Pattern[str]]:
    """The matchers of :func:`_keywords`, for callers that only match, never report."""
    return [pattern for _term, pattern in _keywords(question)]


def _body_anchor(text: str, lines: list[str]) -> tuple[int, int] | None:
    """``(closing fence index, first body prose index)``, or None when neither the
    front matter nor the prose below it exists — both 0-based indices into *lines*.

    "Body prose" is the first line under the block that is neither blank nor a
    markdown heading. Headings are excluded because excluding them is the whole
    point: a ``zotero-import`` source puts ``# <title>`` and ``## Abstract``
    between the fence and the first sentence, and an excerpt that stops on either
    of them shows the reader the title they already saw in ``title:`` and nothing
    they can judge the paper by (#574). A list item ('- DOI: …') is prose here —
    the rule is stated over blankness and heading-ness, not over a guess at which
    prose is worth reading.

    Returns None when the file has no closing fence (nothing is known to be front
    matter, so no line needs rescuing) and also when the block is all there is
    (nothing to rescue). Both cases leave every caller on the plain window.

    The fence comes from :func:`factlog.front_matter_scan.front_matter_end_line`,
    not from a local ``startswith('---')`` scan: the fence rule is that module's
    (#419), and the local copy this replaces would have had to re-decide what an
    unclosed block means — the case where a hand-written note that merely opens
    with ``---`` would otherwise have its whole body read as metadata.
    """
    fence = front_matter_end_line(text)
    if fence is None:
        return None
    anchor = next(
        (
            index
            for index in range(fence + 1, len(lines))
            if lines[index].strip() and not lines[index].lstrip().startswith("#")
        ),
        None,
    )
    return None if anchor is None else (fence, anchor)


def _excerpt_span(
    lines: list[str],
    start: int,
    end: int,
    match: int,
    anchor: tuple[int, int] | None,
) -> tuple[list[str], int]:
    """``(lines to display, last file index the excerpt covers)`` for one match.

    Normally the plain window ``lines[start:end]``. When the match sits INSIDE the
    front-matter block and that window reaches no further than the first body
    line, the body line is attached below an elision marker so the excerpt carries
    at least one line of prose (#574).

    **The attachment is display only — the caller scores the plain window.** That
    is deliberate and it was measured, not assumed. Scoring the attached line
    instead is defensible on its face (the reader sees it, so it is part of what
    the row offers) and it does rank better on the reference KB, but on the wiki
    harness's fixture it silently guts the #594 regression guard: the attached
    abstract carries the same terms as the front matter that matched, so the
    front-matter row's frequency rises (kim-2024 goes (2,2) -> (2,4) on that
    fixture's Q_BACK) and it overtakes the bridged row whose promotion PIN9 exists
    to observe. Reproduced: with the attached line scored, deleting #594's credit
    from search() outright leaves PIN9's pinned order byte-identical — the pin
    stops seeing its own axis. It is not recoverable by re-picking the question
    either; every question that reaches the bridge must contain the term the
    bridge joins on, and that term is in both the title and the abstract, so the
    front-matter row can only tie or beat the bridged row (searched: 36 candidate
    questions over that fixture, best case a tie broken by corpus order).

    Display diverging from score is an established shape in this file, not a new
    one: #573 masks KB path citations out of the score while leaving them in the
    excerpt, and the bridge's '← accepted:' lines are rendered and never scored.
    What #574 adds is a line that is shown for orientation and does not claim to
    be a match — the row's citation line still names where the match was.

    Attaching rather than widening is the point. ``_EXCERPT_WINDOW`` is symmetric
    and applies to every excerpt in the corpus, and the distance a front-matter
    match has to cover is not small: measured on the reference KB, all 59 sources
    with front matter put their first prose line exactly 6 lines below the fence,
    whose own line runs 10..22 (median 12), so a window that reached prose from a
    ``title:`` match would have to be 13+ and would make EVERY excerpt 27 lines.
    Extending this one excerpt contiguously instead is 18 lines of which 11 are
    the metadata and headings the reader is already looking at; attaching is 9.

    The returned index is what the caller feeds to its overlap collapse, and for
    the attached case it is the BODY line, not the window's end. That is what
    makes the fix whole rather than cosmetic: the prose line was already matching
    and already being suppressed by the previous excerpt's collapse (the defect's
    second half), so absorbing it into the excerpt that rescued it keeps it from
    being emitted twice — while the metadata-only excerpt that used to sit between
    them, anchored on ``# <title>``, now collapses away instead of spending a slot
    of the render cap on a heading.

    Nothing changes for a match outside the block, for a file with no front matter,
    for one that is nothing but front matter, or for a block whose body starts
    close enough that the plain window already covers it (``anchor < end``): all
    four return the plain window and ``end - 1``, byte for byte as before.
    """
    if anchor is not None:
        fence, body = anchor
        if match <= fence and end <= body:
            gap = [_EXCERPT_ELISION] if body > end else []
            return lines[start:end] + gap + [lines[body]], body
    return lines[start:end], end - 1


def _sanitize(line: str) -> str:
    """Drop non-printable control characters (keep tabs) so a malformed source
    cannot smuggle NUL/ANSI/control bytes into a rendered answer."""
    return "".join(ch for ch in line if ch == "\t" or ch.isprintable())


# The closed set of directories `factlog init` scaffolds — the only roots a
# KB-relative citation can start from. It is NOT narrowed to the searched corpus
# (sources/ + runs/sources/ + decisions/): measured over the corpus of the
# reference KB (187 readable files under those three dirs), the 533 masked
# citations are 378 `sources/…`, 154 `pages/…` and 1 `policy/…` — narrowing would
# leave those 155 non-corpus citations scoring like prose. facts/, templates/ and
# bare runs/ measure zero hits there but stay in the set: it is the scaffold's
# closed list, and the extension whitelist below bounds what they can misfire on.
#
# "Closed set" is looser than it sounds in one direction and tighter in the other, and
# both were checked so the next reader does not have to re-run it. `factlog init`
# scaffolds NINE directories; this tuple names eight. The missing one is
# `policy/prompts`, and it does not need naming, because the only consumer here is
# _PATH_CITATION_RE below, whose body consumes the rest of the path after the root:
# `policy/prompts/extract.md` is already masked through the `policy` entry (measured;
# a root outside the set, `notarealdir/x.md`, is left alone). By the same measurement
# `runs/sources` is redundant FOR MASKING — with that entry deleted, bare `runs`
# still masks `runs/sources/report.pdf.md`. It is in the tuple because the tuple is
# DERIVED from WIKI_SOURCE_DIRS rather than hand-listed, which is the property worth
# keeping. So a nested scaffold dir needs its own entry only if its parent is absent
# from the set — and none currently is.
_CITED_KB_DIRS = (
    *WIKI_SOURCE_DIRS,
    *WIKI_SUPPLEMENTARY_DIRS,
    "pages",
    "facts",
    "policy",
    "templates",
    "runs",
)
# Extensions a KB citation can end in — DERIVED, never hand-listed. Every original
# format `factlog ingest` accepts (INGEST_CONVERTERS) plus the KB's own file types
# taken from the constants that already define them, plus the plain text/data the KB
# is written in. The first cut of this list was written by hand and let .docx/.pdf in
# while .hwp/.hwpx/.pptx/.odt/.dl stayed out — which silently left #573's defect
# standing for a KB of Korean originals, the very corpus this repo supports first
# class. Deriving is also why importing factlog.ingest here is worth its 0.5 ms.
#
# Requiring an extension at all (rather than a bare `\.\w+`) is what keeps prose out:
# several scaffold dirs are ordinary English nouns, so "3 runs/day.The results" and
# "the policy/value.Networks are trained jointly" were eaten as paths and lost their
# keywords — run-on sentences like that are common in PDF→text conversions under
# runs/sources/. Note this NARROWS the misfire surface, it does not remove it: prose
# whose next word STARTS with a listed extension is still eaten ("policy/value.Txt is
# odd", "pages/index.Htmlish"). Every extension added widens that surface, which is
# the price of covering the formats above; measured on the reference KB the residual
# cost is zero (all 533 masked citations are real). The trailing `\w*` keeps an
# attached Korean particle inside the match (`policy/attribute-relations.md에`, which
# occurs in the reference KB); if it ever swallows too much of an unspaced CJK 어절,
# narrow it to `[가-힣]*` rather than dropping it.
_CITATION_EXTS = tuple(
    sorted(
        {"md", "txt", "json", "yml", "yaml"}
        | {path.suffix.lstrip(".") for path in (ACCEPTED_DL, CANDIDATES_CSV, LOGIC_POLICY_DL)}
        | {ext.lstrip(".") for ext in ingest.INGEST_CONVERTERS}
    )
)
# A KB-root-relative file path written inside an excerpt, e.g.
# `sources/faronius-2025-independence-is-not-an-issue-in-neurosymbolic-ai.md` or
# `runs/sources/kim-2024-neurosymbolic-grounding.txt`. Root-relative ONLY: `./sources/x.md`,
# `~/kb/sources/x.md`, an absolute path, a bare filename and a `#anchor` fragment are
# NOT damped (zero such citations in the reference KB; left to a follow-up issue).
# The dir alternation is sorted (-len, name) purely so the compiled pattern string is
# byte-identical across processes — set iteration order varies with PYTHONHASHSEED, and
# this repo treats determinism as a contract. Order does not affect what is matched: the
# body character class includes '/', so a shorter root that matches first still swallows
# the rest of the path. The body stops at whitespace or the punctuation that closes a
# citation, so `(sources/x.md, confidence=0.85)` yields just the path.
_PATH_CITATION_RE = re.compile(
    r"(?<![\w./-])(?:"
    + "|".join(re.escape(rel) for rel in sorted(set(_CITED_KB_DIRS), key=lambda rel: (-len(rel), rel)))
    + r")/[^\s,;()\[\]<>\"'`]*\.(?:"
    + "|".join(_CITATION_EXTS)
    + r")\w*"
)


def _keyword_hits(
    excerpt: str,
    keywords: list[tuple[str, re.Pattern[str]]],
) -> tuple[set[str], int]:
    """The distinct question terms *excerpt* matches, NFC-folded, and the total
    match count — the two halves of :func:`_excerpt_score`, with the coverage half
    kept as the SET of terms rather than its size.

    search() needs the set, not the count: a row's coverage is now the union of the
    terms it matched in the text and the terms the KB vocabulary bridged to its file
    (#594), and a union of counts would double-count a term reached both ways.

    The NFC fold is LOAD-BEARING; removing it changes results today. The other side
    of the union (_bridge_terms) folds its copy of the same keyword list, so the two
    sides differ on any question term that is not already NFC — and such a term does
    reach the bridge. _is_cjk is `any()` over the token: '漢' + NFD('글자') contains a
    CJK ideograph, so it passes the script filter with its Hangul still decomposed,
    and the two channels then hold two spellings of one word. Unfolded, that word is
    counted twice and the row outranks one that really does cover more of the
    question (reproduced end-to-end: the two files swap places).

    An ALL-Hangul NFD term does not get that far — decomposed Hangul is conjoining
    jamo and nothing else in the token passes _is_cjk — which is why the first cut of
    this docstring called the fold defensive. That reasoning generalized from the
    pure case to a filter that is `any()`, and the mixed case is the counter-example.
    Both are pinned in tests/unit/test_ask_kb_backing_rank.py.

    Masking and match rules live here alone; _excerpt_score is a thin count over
    this. Two copies of the path mask (#573) is exactly the "equivalent rule
    duplicated" shape common.py's fact_key warns about.
    """
    low = _PATH_CITATION_RE.sub(" ", excerpt.lower())
    hits = {_nfc(term) for term, pattern in keywords if pattern.search(low)}
    frequency = sum(len(pattern.findall(low)) for _term, pattern in keywords)
    return hits, frequency


def _excerpt_score(excerpt: str, patterns: list[re.Pattern[str]]) -> tuple[int, int]:
    """Relevance of an excerpt to the query: (distinct keyword coverage, total
    match frequency). An excerpt covering more of the query's keywords ranks
    above one that merely repeats a single keyword — so the most relevant excerpt
    surfaces even under a small result cap.

    Cited KB file paths are MASKED OUT before scoring (#573). A filename is a
    locator for where text lives, not a claim about the topic: a note whose body
    is a list of `sources/…-neurosymbolic-….md` links says nothing about
    neurosymbolic anything, yet unmasked it outscored the paper itself. The
    damping is full EXCLUSION (weight 0), not a reduced fractional weight, for
    two reasons: (a) this score is an integer lexicographic sort key whose SCALE
    is itself pinned — a fractional path weight would have to float the key (a
    contract change for every caller that compares it) and the integer workaround,
    scaling prose up instead, changes what a plain prose line scores; (b) zero is
    the honest weight — the keyword in a path is evidence about a *different*
    file, so any nonzero weight would still let enough citations outrank prose.

    Only the path token itself is removed, so prose is scored exactly as before:
    a path mentioned mid-sentence loses its own characters and nothing else, and
    the same keyword spelled out in the surrounding sentence still counts.
    COLLECTION recall is untouched on purpose — search() gates an excerpt on the
    raw, unmasked line, so a path-only note is still collected and ranked rather
    than filtered out. It is NOT retained unconditionally in the rendered answer:
    a (0, 0) excerpt sinks below better-scoring ones and can fall outside
    search()'s `limit` cap (measured: with the default cap of 10, a demoted
    decisions/ excerpt drops out of the top 10). That demotion is the intended
    effect; deciding an excerpt is unciteable is not this issue's scope.

    Pattern-only callers keep this entry point. The positional index stands in for
    the surface term: _keyword_hits keys its set by term, and callers that hold
    patterns alone have no terms to give — indices are unique, which is all the set
    needs to count each pattern once.
    """
    hits, frequency = _keyword_hits(excerpt, [(str(i), pat) for i, pat in enumerate(patterns)])
    return (len(hits), frequency)


def _semantic_rerank(question: str, results: list[dict[str, object]]) -> list[dict[str, object]]:
    """Optional neural re-rank. Bundled retrieval is lexical (relevance-ranked);
    a neural backend is NOT bundled (it would need a model + network, breaking
    deterministic/offline CI). If the env var FACTLOG_EMBED_MODULE names an
    importable module exposing ``rank(question, texts) -> list[float]`` (higher =
    more similar), results are reordered by it. Any absence/failure → unchanged
    (graceful degrade). The backend reorders only the already-capped top lexical
    candidates; it cannot widen recall beyond lexical matches. The module runs
    with full process privileges (it is opt-in by the KB operator).

    The re-rank does NOT preserve the directory grade (#572): a backend may put a
    supplementary decisions/ excerpt above a primary one. Measured with a stub
    backend (`rank` returning ascending scores, i.e. an exact reversal), the top
    result flips from `sources` to `decisions (supplementary)`. That is the chosen
    behaviour, for two reasons. (a) ON THE CAPPED PATH the grade key has already
    done its decisive work by this point: search() sorts by grade FIRST and slices
    to `limit` BEFORE calling here, so the grade decides WHICH excerpts a backend
    ever sees and the backend only orders that set — re-sorting within grade groups
    would buy a guarantee the caller mostly already has, at the cost of overruling
    the one signal the operator opted in to get. This reason does NOT hold under
    `--all` (limit=None): there is no cap, so the backend sees every excerpt and
    the grade constrains nothing. (b) alone decides that case, and it is the one
    that carries the choice. (b) Enabling a backend is an explicit,
    KB-operator-level statement that semantic similarity should decide order;
    silently pinning grade above it would make the backend's effect depend on a
    directory layout it cannot see. The guarantee "supplementary never ranks first
    while a primary excerpt exists" is therefore scoped to the bundled lexical
    path, i.e. FACTLOG_EMBED_MODULE unset — which is what CI and every default
    install run, and what tests/test_ask_wiki_search.sh pins."""
    module_name = os.environ.get("FACTLOG_EMBED_MODULE")
    if not module_name or not results:
        return results
    try:
        backend = importlib.import_module(module_name)
        scores = backend.rank(question, [str(r["excerpt"]) for r in results])
        if not isinstance(scores, list) or len(scores) != len(results):
            return results
        floats = [float(score) for score in scores]
        if not all(math.isfinite(value) for value in floats):
            return results  # reject NaN/inf → keep lexical order
        order = sorted(range(len(results)), key=lambda i: floats[i], reverse=True)
        return [results[i] for i in order]
    except Exception:
        return results  # graceful degrade to lexical ranking


def _fill_recall(
    recall: dict[str, list[str]] | None,
    keywords: list[tuple[str, re.Pattern[str]]],
    pending: set[int],
) -> None:
    """Write the keyword recall tally into the caller's *recall* dict (#575).

    Question order is preserved in both lists so the report reads back as the
    question was typed. *pending* holds the indices still unseen in the corpus.
    """
    if recall is None:
        return
    recall["matched"] = [term for i, (term, _pat) in enumerate(keywords) if i not in pending]
    recall["unmatched"] = [term for i, (term, _pat) in enumerate(keywords) if i in pending]


# ---------------------------------------------------------------------------
# KB-vocabulary bridge (#576) — reaching a source the lexical scan cannot,
# and (#594) crediting the one it already reached
# ---------------------------------------------------------------------------
# The engine's facts are normalized to Korean (relation '이점', object
# '설명가능성_향상'); the sources they were extracted FROM are English abstracts.
# So a Korean question cannot reach its own source lexically — measured on the
# reference KB, the stem '해석가능' occurs in ZERO of the 186 readable source
# files and in 4 accepted facts. The retrieval failure is not "the KB is silent
# on this"; it is "the KB holds the answer in a vocabulary the corpus does not
# spell".
#
# The bridge closes that gap with the vocabulary the engine ALREADY holds, and
# nothing else: question word -> accepted relation/object -> the backing source
# path joined out of candidates.csv -> that file joins the search candidates,
# labeled so it is never read as a lexical hit. Translation and embeddings stay
# out of scope for the reason _semantic_rerank stays opt-in — CI is offline and
# deterministic.
#
# What the bridge does NOT do, deliberately:
#   - It does not promote anything to VERIFIED. A bridged excerpt is source prose
#     that no one checked; reaching it through an accepted fact says the FACT was
#     accepted, not the sentence. Rendering it inside the UNVERIFIED block with an
#     explicit tag is the whole contract (수용 기준 3).
#   - It does not label a file the scan already cited. That file gets the same
#     backing credited to its ranking key (#594, search()), but no tag and no
#     '← accepted:' lines: the tag means "reached this way and NOT lexically", so
#     wearing it would say something false about a row the reader's own keyword
#     produced. #576 left the credit out and called it #572/#573's ground; both
#     closed without taking it, and the omission is what this corpus feels most —
#     a source whose title borrows the English term is found lexically, so the
#     "already cited" rule dropped the KB's evidence about exactly the files that
#     match, and kept it only for the ones no keyword reached. Measured on the
#     reference KB, question 'neurosymbolic 접근이 순수 신경망 대비 해석가능성에서
#     어떤 이점을 갖는가': 22 of the 28 rows — every PRIMARY one — scored (1,1,1),
#     so the KB channel decided nothing and the order was the filename's. (The
#     remaining 6 are supplementary and the grade key had already separated them.)
#     The issue's own evidence file (tilwani-2024) sat at 13 with the same key as
#     every other primary row.
#   - It does not touch the #575 recall tally. A bridged term still occurs nowhere
#     in the corpus text; counting it as matched would make that diagnostic claim
#     the corpus contains wording it does not.

# Shared-prefix floor for a bridge match, in characters.
#
# The comparison is on a common PREFIX rather than substring containment because
# Korean inflects on the right: the question writes '해석가능성에서' (조사 attached)
# where the KB writes '해석가능하며' / '해석가능한' / '해석가능' — one shared stem,
# three tails. A prefix comparison reads that stem off both sides without a
# morphological analyzer, which is exactly what #581 exists to introduce and what
# this issue must not wait for.
#
# Three is not a tuned constant; it is where the measurement splits. Every figure
# below is a TOTAL at that floor (not a delta), on the reference KB's 2055 accepted
# facts, question 'neurosymbolic 접근이 순수 신경망 대비 해석가능성에서 어떤 이점을
# 갖는가':
#   floor 2 -> 44 facts / 27 subjects
#   floor 3 -> 11 facts /  8 subjects
#   floor 4 ->  4 facts /  4 subjects
#   floor 5 ->  1 fact  /  1 subject
#   floor 6 ->  0 facts /  0 subjects
# The 33 facts floor 2 adds over floor 3 are EVERY one a 2-character fragment: the
# relation '이점' alone drags in every 이점 fact in the KB (FRET measurement,
# base-editor screening, TTS prosody — the question's grammar, not its topic), and
# '대비' '접근' '신경' '가능' '해석' match inside unrelated compounds. At floor 3 all
# 11 are about 해석가능 or 신경망. Going higher does not buy precision, it buys
# silence: floor 4 already drops '신경망', the question's most literal content word,
# and by floor 6 the bridge answers nothing at all.
# What the floor does to the function-word class, stated no wider than it holds — the
# first cut of this comment said the class was "structurally unreachable instead of
# filtered", and that is true of less than half of it. Of the 75
# _CJK_QUESTION_STOPWORDS, 34 are 2 characters ('논문', '이점'); for those it IS
# structural, since a 2-character word can never reach a 3-character shared prefix, so
# no question can bridge VIA one. The other 41 are 3+ characters ('무엇인가', '어떻게',
# '논문은') and this floor does not touch them. What excludes THEM is the filter:
# _bridge_terms reuses _keywords instead of re-tokenizing, so #571's stop-word list is
# in force here too (see its docstring). On the reference KB none of the 41 would
# bridge even without that filter — measured, 0 of the 41 shares a 3-character prefix
# with any of the 1656 vocabulary words — but that is a property of one KB's
# vocabulary, not a structure, so it is a measurement and not the guarantee. The
# guarantee for those 41 is the filter, and deleting it is not covered by this floor.
#
# The cost is real and deliberate: a two-syllable CONTENT noun ('근거', '추론')
# cannot bridge either. Buying those back means telling '근거' from '대비', which is
# a morphological judgement, not a smaller constant.
_BRIDGE_PREFIX_MIN = 3

# Marker appended to a bridged excerpt's citation header. Greppable, and it states
# BOTH halves of the contract on one line: how the excerpt was reached, and that
# reaching it that way changed nothing about its standing.
VIA_KB_VOCABULARY_TAG = "[via KB vocabulary — still UNVERIFIED]"


def _nfc(value: str) -> str:
    """NFC-fold for bridge comparison. accepted.dl and candidates.csv are written
    by whatever editor touched them last, so an NFD-authored object would compare
    unequal to an NFC question character by character and silently never bridge."""
    return unicodedata.normalize("NFC", value)


def _shared_prefix_len(left: str, right: str) -> int:
    length = 0
    for left_char, right_char in zip(left, right):
        if left_char != right_char:
            break
        length += 1
    return length


def _vocabulary_words(value: str) -> list[str]:
    """The CJK words inside an accepted relation name or object.

    Accepted objects are underscore-joined normal forms ('설명가능성_향상'), so the
    words a question can actually name are the underscore segments. Splitting here
    is what lets the question's natural form ('설명가능성') meet the normal form
    (수용 기준 4). Non-CJK segments are dropped: see _bridge_terms.
    """
    return [word for word in _nfc(value).split("_") if _is_cjk(word)]


def _bridge_terms(question: str) -> list[str]:
    """The question words eligible to bridge — the CJK keywords _keywords() already
    produced, nothing re-derived.

    Reusing _keywords is what keeps #571's stop-word filter in force here too: a
    bridge that re-tokenized the question would hand '논문은' a second way in, below
    the one guard that drops it.

    ASCII keywords are excluded on purpose. The bridge exists to cross the script
    boundary the lexical matcher cannot; an ASCII term already matches the English
    source text directly, so bridging it adds rows without adding reach.
    """
    return [_nfc(term) for term, _pattern in _keywords(question) if _is_cjk(term)]


def _bridged_facts(
    question: str,
    accepted: list[dict[str, str]],
) -> dict[tuple[str, str, str], list[str]]:
    """{(subject, relation, object): [question word that reached it, ...]}.

    Matching is relation names and objects only. Subjects are NOT matched here:
    an accepted entity named in the question is _entity_mentioned()/
    grounding_facts()' job (수용 기준 6), and it answers a different question —
    grounding shows the VERIFIED facts about that entity, while this bridge only
    widens which files the UNVERIFIED excerpt search is allowed to look at.
    """
    terms = _bridge_terms(question)
    if not terms:
        return {}
    matched: dict[tuple[str, str, str], list[str]] = {}
    for row in accepted:
        hits: set[str] = set()
        for field in ("relation", "object"):
            for word in _vocabulary_words(row[field]):
                for term in terms:
                    if _shared_prefix_len(word.lower(), term) >= _BRIDGE_PREFIX_MIN:
                        hits.add(term)
        if hits:
            # NFC-folded key: this dict is looked up with a triple read out of
            # candidates.csv, a different file written by a different tool, and the
            # two must agree on composition or the join silently finds nothing.
            matched[(_nfc(row["subject"]), _nfc(row["relation"]), _nfc(row["object"]))] = sorted(hits)
    return matched


def _anchor_line(path: Path, line_count: int, fragment: str) -> int:
    """1-based line of the markdown heading *fragment* names, or 1.

    candidates.csv records provenance as ``sources/x.md#abstract`` — a SECTION
    name, not a line number (measured on the reference KB: 761 of 2139 rows carry
    an anchor, all of them '#abstract'). Resolving it matters because the excerpt
    window is 3 lines either side: anchored at the file head, a bibliographic
    source yields seven lines of YAML front matter, which is where the prose an
    excerpt is for does NOT begin. An unresolvable or absent fragment falls back
    to line 1, and since #574 that fallback lands on the front-matter path in
    _excerpt_span, which attaches the document's first body line rather than
    leaving the row prose-free — so the fallback costs the anchor's precision now,
    not the prose. Resolving remains better: the excerpt is then centred on the
    section the fact was extracted from instead of on the metadata.

    The anchor set comes from ``validate.heading_anchors`` — the SAME function
    ``validate_source_ref`` asks "does this anchor exist", asked here for "where".
    The first cut of this resolved anchors with a local regex and a local slug rule,
    and the two answers disagreed on three real shapes: a punctuated heading
    (``## CRISPR/Cas9 results`` -> ``#crisprcas9-results``), GitHub's duplicate
    suffix (``#abstract-1``), and a ``## Abstract`` written INSIDE a code fence,
    which the local scan anchored on and #521 exists to say is not a heading. Every
    one of those makes ``factlog validate`` pass a ref this then silently falls back
    on, i.e. re-creates #574's front-matter window for a ref the KB called valid.
    The slug rules also split on script — the local one kept 가-힣 only, so ``概要``
    slugged to '' here and to ``概要`` there. common.py's ``fact_key`` names this
    class outright: a second, "equivalent" copy of a rule is a latent recurrence.

    *line_count* is the RAW file's line count; anchors are found in the
    front-matter-stripped body (md_lines reads a closing ``---`` as a Setext
    heading otherwise), so the body index is shifted back by the block's height.
    """
    if not fragment:
        return 1
    try:
        body = front_matter_body(path)
    except OSError:
        return 1
    index = validate.heading_anchors(body).get(fragment.lower())
    if index is None:
        return 1
    # front_matter_body returns a SUFFIX of the file, so the line counts differ by
    # exactly the block's height — no second parse of the fence, which is the
    # boundary front_matter_scan owns (#419).
    return line_count - len(body.splitlines()) + index + 1


def kb_vocabulary_bridge(question: str, root: Path) -> dict[str, dict[str, object]]:
    """{source ref: {'fragment': str, 'facts': [ 's, r, o', ... ], 'terms': [...]}}.

    Pure join, no corpus I/O: 'fragment' is the anchor as candidates.csv recorded it
    ('abstract', or '' when the row carries none). Resolving it to a line needs the
    file, which is _bridge_rows' job — keeping the two apart is what lets this
    function be read (and tested) as the provenance question it is.

    The join is mandatory and is the reason this is not a two-line change:
    ``facts/accepted.dl`` carries only (subject, relation, object) — it has NO
    source field — so the backing path exists solely in ``facts/candidates.csv``.

    Only rows whose status is in ENGINE_STATUSES (confirmed/accepted) are joined.
    Promoting a ``superseded`` or ``needs_review`` source would make the UNVERIFIED
    block quote evidence the KB has already retired, which is worse than the
    cross-lingual miss this bridge exists to fix.

    Graceful degrade is the whole failure policy: a KB with no accepted.dl, no
    candidates.csv, an unreadable/malformed one, or facts that join nothing returns
    {} and search() behaves exactly as it did before (수용 기준 5).
    """
    if not _bridge_terms(question):
        return {}  # an all-ASCII question reads neither fact file
    try:
        kb = KbContext.for_root(root)
        if not kb.accepted_dl.is_file() or not kb.candidates_csv.is_file():
            return {}
        matched = _bridged_facts(question, kb.load_accepted_facts())
        if not matched:
            return {}
        candidates = kb.load_facts()
    except (FactlogError, OSError, UnicodeDecodeError, ValueError):
        return {}
    bridged: dict[str, dict[str, object]] = {}
    for row in candidates:
        if row["status"] not in ENGINE_STATUSES:
            continue
        # NFC on BOTH sides of the join. accepted.dl and candidates.csv are separate
        # files written by separate tools; an NFD-authored triple in one and an
        # NFC-authored triple in the other are the same fact and compare unequal
        # character by character, and the whole bridge would then return {} with
        # nothing logged anywhere. _bridged_facts already folds its side.
        terms = matched.get((_nfc(row["subject"]), _nfc(row["relation"]), _nfc(row["object"])))
        if not terms:
            continue
        # Same fold on the path, and for a sharper reason than the join: the ref is
        # compared against the refs the lexical scan collected (see _bridge_rows'
        # `cited`), which come from the filesystem. Two spellings of one path make the
        # same file appear under BOTH banners — a lexical row and a row tagged "not a
        # lexical match" — so the tag would state something false. This mirrors the
        # fold every other reader of candidates' `source` already applies
        # (common.py's cited_source_counts, merge_candidates, source_coverage, cli).
        ref, _sep, fragment = _nfc(row["source"]).partition("#")
        if not ref:
            continue
        entry = bridged.setdefault(ref, {"fragments": set(), "facts": set(), "terms": set()})
        entry["fragments"].add(fragment)
        entry["facts"].add(
            f"{_nfc(row['subject'])}, {_nfc(row['relation'])}, {_nfc(row['object'])}"
        )
        entry["terms"].update(terms)
    # Sorted everywhere below: one file can be the source of several bridged facts
    # with different anchors, and this module treats determinism as a contract — the
    # rendered answer must not depend on CSV row order or set iteration order.
    return {
        ref: {
            "fragment": sorted(entry["fragments"])[0],
            "facts": sorted(entry["facts"]),
            "terms": sorted(entry["terms"]),
        }
        for ref, entry in sorted(bridged.items())
    }


def _bridge_rows(
    bridged: dict[str, dict[str, object]],
    root: Path,
    cited: set[str],
    ignored_patterns: list[str],
) -> list[tuple[tuple[int, int, int], dict[str, object]]]:
    """Scored bridge excerpts for sources the lexical scan did not cite.

    A file the scan already cited is skipped: 수용 기준 2 defines the tag as "reached
    via KB vocabulary and NOT by a lexical match", so tagging a file that WAS matched
    lexically would say something false, and adding a second excerpt from it would
    duplicate the file under two banners. *cited* holds NFC-folded refs and
    kb_vocabulary_bridge folds its side, so one path spelled two ways cannot slip
    past that skip.

    Every guard the lexical scan applies applies here too — corpus directories only,
    sync-ignored sources excluded (a source not synced is not evidence for wiki
    exploration either), no symlink out of the KB, no binary. pages/ can never be
    reached: it is not in WIKI_SOURCE_DIRS, so an engine-derived candidate page still
    cannot enter an answer through this path.

    The excerpt window arithmetic below is a SECOND site: search()'s scan computes
    the same start/end from _EXCERPT_WINDOW. The two still compute their own
    start/end — the scan collapses overlapping windows against `last_end` and a
    single promoted excerpt has no analogue for that — but since #574 both build the
    displayed lines through :func:`_excerpt_span`, so the front-matter rule is not
    duplicated here. The collapse index it returns is discarded on this path (named
    `_covered`) for the same reason: there is no second excerpt of this file to
    collapse against.

    *bridged* is :func:`kb_vocabulary_bridge`'s map, taken as an argument rather than
    computed here: since #594 the SAME map also credits the rows the scan did cite,
    and two calls could disagree only by reading the KB twice mid-question.
    """
    if not bridged:
        return []
    rows: list[tuple[tuple[int, int, int], dict[str, object]]] = []
    # Longest first so 'runs/sources' is tested before 'sources' if the two ever
    # become prefixes of one another.
    corpus_dirs = sorted(WIKI_SOURCE_DIRS, key=lambda rel: (-len(rel), rel))
    for ref, entry in bridged.items():
        if ref in cited or is_sync_ignored(ref, ignored_patterns):
            continue
        rel = next((rel for rel in corpus_dirs if ref.startswith(f"{rel}/")), None)
        if rel is None:
            continue
        base = root / rel
        path = root / ref
        try:
            if not path.is_file() or not path.resolve().is_relative_to(base.resolve()):
                continue
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if "\x00" in text:
            continue
        lines = text.splitlines()
        if not lines:
            continue
        anchor = min(_anchor_line(path, len(lines), str(entry["fragment"])), len(lines))
        start = max(0, anchor - 1 - _EXCERPT_WINDOW)
        end = min(len(lines), anchor + _EXCERPT_WINDOW)
        # Same body attachment as the scan (#574). It is reached here on the
        # fallback path _anchor_line documents: a bridged row whose provenance
        # carries no anchor, or one this KB's headings do not resolve, anchors on
        # line 1 — inside the front matter — and produced the seven metadata lines
        # and zero prose that #574 is about.
        span, _covered = _excerpt_span(lines, start, end, anchor - 1, _body_anchor(text, lines))
        result = {
            "file": ref,
            "line": anchor,
            "excerpt": "\n".join(_sanitize(line) for line in span),
            "dir": rel,
            "via": {"facts": entry["facts"], "terms": entry["terms"]},
        }
        # Scored in the SAME (coverage, frequency) space as a lexical excerpt, by
        # the same reading of it — how much of the question this row answers, and
        # how much evidence says so — measured on the other channel: distinct
        # question words that bridged here, and how many accepted facts did the
        # bridging. Scoring these 0 instead would be a quieter defect than the one
        # this issue fixes: on a KB whose lexical matches alone fill the render cap,
        # the bridged source would be collected, ranked last, and never shown.
        rows.append(((_GRADE_PRIMARY, len(entry["terms"]), len(entry["facts"])), result))
    return rows


def search(
    question: str,
    root: Path,
    *,
    limit: int | None = 10,
    recall: dict[str, list[str]] | None = None,
) -> list[dict[str, object]]:
    """Relevance-ranked search over the wiki corpus (sources/ + runs/sources/).

    Collects keyword-matched excerpts, ranks them by (directory grade, keyword
    coverage, match frequency) — primary source text ahead of supplementary
    decisions/ notes at every relevance level (#572) — optionally re-ranks via a
    neural backend (graceful degrade when absent), and returns the top *limit*
    cited excerpts: {file, line, excerpt, dir}. Binary files (e.g. an un-converted
    .docx) are skipped.

    Sources the question reached through the engine's accepted Korean vocabulary
    rather than through the corpus text are added by the bridge (#576) and carry an
    extra 'via' key naming the accepted facts that reached them. They are ranked
    alongside the lexical excerpts and stay UNVERIFIED; no other row gains a key, so
    a KB with no bridge returns byte-identical output.

    A file the scan DID cite is credited with that same vocabulary instead of being
    tagged with it (#594): its coverage becomes the union of the terms it matched in
    the text and the terms bridged to it, and its frequency gains the backing facts.
    The credit is ordering only — no key, no tag, no row added or removed, and it
    ranks below the directory grade — so the rows returned are the same rows in a
    different order, and a KB whose fact files are absent or join nothing ranks
    exactly as before.

    When *recall* is given it is filled with {'matched': [...], 'unmatched': [...]}
    — which of the question's keywords the corpus contains (#575). It is a pure
    side report: nothing below reads it, so it cannot influence scoring, ordering
    or the returned rows.

    The tally is taken HERE, per scanned line, because every vantage point outside
    this loop undercounts and would report a keyword the corpus HAS as absent:
      - after the `limit` cap: the cap is a RENDER budget; a keyword whose only
        excerpt ranks 11th disappears from a top-10 answer,
      - from the returned rows even uncapped: the `last_end` window collapse below
        drops an excerpt whose window overlaps the previous one, so a keyword
        confined to those lines is in no row at all.
    A recall report that understates recall is not a safety net; it is a second,
    quieter version of the failure #575 exists to expose.

    Being taken in the scan is also what makes the tally independent of the grade
    key (#572): the grade decides the ORDER of the excerpts and, with the cap,
    WHICH of them are returned, but a keyword is recorded when a line contains it,
    before any of that. A keyword whose only occurrence is in a supplementary
    excerpt pushed below the cap is still reported matched.
    """
    keywords = _keywords(question)
    patterns = [pattern for _term, pattern in keywords]
    # Indices of keywords not yet seen anywhere in the corpus; the scan discharges
    # them, and whatever remains at the end is the unmatched set.
    pending = set(range(len(keywords)))
    if not patterns:
        _fill_recall(recall, keywords, pending)
        return []
    scored: list[tuple[tuple[int, int, int], dict[str, object]]] = []
    cited: set[str] = set()  # files the lexical scan cited — the bridge below skips them
    ignored_patterns = sync_ignore_patterns(root)
    # Read ONCE, before the scan, and used twice: to credit the rows the scan cites
    # (#594, below) and to build rows for the files it never cites (#576). Reading it
    # here also means the KB join happens once per search() instead of once per call
    # site.
    bridged = kb_vocabulary_bridge(question, root)
    for rel, label, grade in _wiki_corpus():
        base = root / rel
        if not base.is_dir():
            continue
        base_resolved = base.resolve()
        for path in sorted(p for p in base.rglob("*") if p.is_file()):
            # Stay within the corpus root: never follow a symlink out of the KB.
            if not path.resolve().is_relative_to(base_resolved):
                continue
            ref = path.relative_to(root).as_posix()
            # Sync-ignore means this primary source is not evidence for wiki
            # exploration either. Supplementary decisions remain searchable:
            # they are explicitly labeled and are not source files.
            if rel in WIKI_SOURCE_DIRS and is_sync_ignored(ref, ignored_patterns):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue  # unreadable — skip
            if "\x00" in text:
                continue  # binary (valid-UTF-8-with-NUL) — skip
            lines = text.splitlines()
            # Once per file, not per match: every excerpt of a file shares one
            # front-matter boundary, and the scan below can emit several.
            body = _body_anchor(text, lines)
            last_end = -1  # collapse overlapping windows within this file
            for i, line in enumerate(lines):
                low = line.lower()
                if pending:
                    # Only the still-unseen keywords are tested, and only until the
                    # last one is discharged — so the common case (every keyword
                    # found early) costs nothing, and the worst case is the same
                    # O(len(patterns)) per line the non-matching branch below
                    # already pays.
                    pending.difference_update(
                        idx for idx in tuple(pending) if keywords[idx][1].search(low)
                    )
                if not any(pat.search(low) for pat in patterns):
                    continue
                start = max(0, i - _EXCERPT_WINDOW)
                if start <= last_end:
                    continue  # window overlaps the previously emitted excerpt
                end = min(len(lines), i + _EXCERPT_WINDOW + 1)
                span, last_end = _excerpt_span(lines, start, end, i, body)
                excerpt = "\n".join(_sanitize(line_text) for line_text in span)
                # The WINDOW, not the excerpt: an attached body line (#574) is shown
                # for orientation and is not a match, so it does not score. The two
                # are the same string for every excerpt that was not attached to,
                # which is why this is written as a second join rather than as a
                # flag — the divergence is visible here or it is invisible.
                scored_text = "\n".join(_sanitize(line_text) for line_text in lines[start:end])
                result = {
                    "file": ref,
                    "line": i + 1,
                    "excerpt": excerpt,
                    "dir": label,
                }
                hits, frequency = _keyword_hits(scored_text, keywords)
                # KB-vocabulary backing (#594): the engine's accepted facts reach this
                # file through question words the text does not spell. #576 left this
                # credit out and named it #572/#573's ground; both closed without
                # taking it, and the omission is what made the bridge weakest exactly
                # where the KB is richest — a source whose title borrows the English
                # term is found lexically, so the rule "do not promote a cited file"
                # dropped the KB's own evidence about the best-matching files and kept
                # it only for the ones no keyword reached.
                #
                # Credited into the SAME two components, in the same reading: coverage
                # is how much of the question this row answers, frequency is how much
                # evidence says so. A bridged term is a question word answered on the
                # other channel, an accepted fact is evidence for it. The union is what
                # keeps a Korean source (whose text spells the term AND whose facts
                # bridge it) from being counted twice — in COVERAGE. Frequency is a
                # sum and is not deduplicated: a term found both ways adds on both
                # sides there, which is the same reading (two independent pieces of
                # evidence), and it only ever breaks a coverage tie.
                #
                # KNOWN COST, measured, not fixed here: the backing is a per-FILE
                # constant added to EVERY excerpt of that file, so a file with many
                # excerpts rises as a block and can take most of the render cap. On
                # the reference KB, '오메가-3 보충이 COPD 환자에게 효과있음을 보인
                # 연구는?': distinct papers in the default top 10 fall from 9 to 3
                # (5 + 4 + 1 excerpts of three files). Over a 26-question sample the
                # total distinct-file count over each top 10 falls 197 -> 190, and an
                # independent 30-question sample measured 175 -> 169 — i.e. the whole
                # loss is that one question shape, "which studies …?" over a corpus
                # where one paper carries many matching excerpts. Damping it (crediting
                # only a file's best excerpt, or a diversity term in the sort key) is a
                # change to the ranking key's shape and is left to a follow-up; the
                # numbers above are here so that follow-up can reproduce the baseline.
                backing = bridged.get(_nfc(ref))
                if backing:
                    hits = hits | set(backing["terms"])
                    frequency += len(backing["facts"])
                scored.append(((grade, len(hits), frequency), result))
                # NFC-folded: this set is compared against refs read out of
                # candidates.csv, and the two spellings of one Korean filename are
                # equal to every renderer and unequal to `in`. Unfolded, the same
                # file came back as a lexical row AND as a row tagged "not a lexical
                # match" (reproduced: sources/해석연구.md at lines 9 and 7).
                cited.add(_nfc(ref))
    # KB-vocabulary bridge (#576): sources the question reached through the engine's
    # own Korean vocabulary rather than through the corpus text. Appended AFTER the
    # scan and BEFORE the sort, so bridged rows compete on the same ranking key
    # instead of being pinned to either end of the list. Their rows carry 'via'; no
    # other row does, so a caller that never looks at it sees exactly what it saw
    # before on a KB with no bridge.
    scored.extend(_bridge_rows(bridged, root, cited, ignored_patterns))
    # Taken before the sort and the cap below, so neither the grade key (#572) nor
    # the render budget can make recall look worse than the corpus is (#575).
    _fill_recall(recall, keywords, pending)
    # Rank by (directory grade, keyword coverage, match frequency), desc; ties keep
    # corpus/line order (stable sort over the already-ordered collection). Then take
    # the cap, then optional neural rerank.
    #
    # Grade leads (#572). SKILL.md defines the wiki block as cited sources/ and
    # runs/sources/ excerpts with decisions/ as supplementary, so a supplementary
    # excerpt must never displace a primary one no matter how many times it repeats
    # a keyword. The consequence is deliberate: because grade is the top key and the
    # cap is applied to the SORTED list, a KB whose primary excerpts alone fill the
    # cap shows no supplementary excerpt at all. Measured on the reference KB with
    # the query '오메가-3 보충이 COPD 환자에게 효과있음을 보인 연구는?', which returns
    # 134 excerpts — 124 primary, 10 supplementary: before this change the top 10
    # held 4 supplementary excerpts INCLUDING rank 1; after it the 10 supplementary
    # excerpts occupy ranks 125-134, none inside the default cap. That is the
    # intended reading of "supplementary": context for a question the sources do
    # not answer, not a competitor to them.
    # Supplementary excerpts are never FILTERED — they are collected and ranked as
    # before and reappear as soon as primary matches run short (or under `--all`,
    # which passes limit=None).
    scored.sort(key=lambda item: item[0], reverse=True)
    ranked = [result for _score, result in scored]
    if limit is not None:
        ranked = ranked[:limit]
    return _semantic_rerank(question, ranked)


def _render_limit(value: int | None) -> int | None:
    """Translate the public ``--all`` mode to an internal row cap."""
    return None if value is None else max(0, value)


def _truncation_line(total: int, shown: int) -> str | None:
    """Return an explicit audit escape-hatch notice when rows were omitted."""
    omitted = total - shown
    if omitted <= 0:
        return None
    return f"… {omitted} more rows (full output: --all)"


def _single_column_projection(rows: list[list[str]]) -> tuple[int, list[tuple[int, str]]] | None:
    """Describe a lossless projection when exactly one column varies.

    The returned column index and fixed indexed values retain enough structure to
    reconstruct every displayed row.  Provenance stays attached to each varying
    value in :func:`render_engine_answer`; this is display compaction, never an
    LLM-authored summary.
    """
    if len(rows) < 2 or not rows or not rows[0]:
        return None
    width = len(rows[0])
    if any(len(row) != width for row in rows):
        return None
    varying = [index for index in range(width) if any(row[index] != rows[0][index] for row in rows[1:])]
    if len(varying) != 1:
        return None
    varying_index = varying[0]
    return varying_index, [(index, rows[0][index]) for index in range(width) if index != varying_index]


def _entity_mentioned(entity: str, question_low: str) -> bool:
    """Whether an accepted entity name appears in the question (bilingual,
    matching the keyword matcher's contract): CJK substring (length >= 2);
    ASCII lookaround boundaries so punctuation-edged names like 'C++'/'.NET'
    match while short names don't match inside unrelated words."""
    name = entity.lower()
    if _is_cjk(entity):
        return len(entity) >= 2 and name in question_low
    return re.search(rf"(?<!\w){re.escape(name)}(?!\w)", question_low) is not None


def grounding_facts(question: str, accepted: list[dict[str, str]]) -> list[dict[str, str]]:
    """Engine-verified accepted facts about the accepted entities the question
    mentions — verified anchors to show alongside an unverified wiki answer.
    Pure: only reads the accepted facts passed in."""
    question_low = question.lower()
    mentioned = {ent for ent in entity_set(accepted) if _entity_mentioned(ent, question_low)}
    if not mentioned:
        return []
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, str]] = []
    for row in accepted:
        if row["subject"] in mentioned or row["object"] in mentioned:
            key = (row["subject"], row["relation"], row["object"])
            if key not in seen:
                seen.add(key)
                out.append(row)
    return out


# ---------------------------------------------------------------------------
# Decomposition proposals (#577) — the single queries a combined question hides
# ---------------------------------------------------------------------------
# A question that binds two conditions ("A 이면서 B 인 것은?") has no shape in this
# query language: classify_query validates ONE predicate line, so the combined form
# leaves through review_required and the user gets UNVERIFIED excerpts although each
# half, asked alone, is an engine-verifiable query. Measured on the reference KB, the
# issue's own question decomposes into 11 such queries, and every one of them routes
# to the engine.
#
# The generator is the #576 bridge's matcher, not a second one: _bridged_facts already
# answers "which accepted relation/object did this question's words reach", which is
# exactly the input a proposal needs. What is added here is the (relation, object)
# grouping, the verification, and the cap.
#
# Deliberate limits, so the block stays a proposal and not a second answer:
#   - Nothing is executed as an answer. The row COUNT is pre-computed (수용 기준 5:
#     a 0-row proposal is worth knowing about before you ask it) but no rows are
#     rendered, so what becomes a verified answer stays the reader's decision.
#   - Only proposals that pass classify_query are shown (수용 기준 2), verified one at
#     a time through the SAME classify() every other caller routes on.
#   - candidates.csv is not read. The engine answers out of accepted.dl, so a fact's
#     candidate status decides which SOURCE the bridge may quote (kb_vocabulary_bridge)
#     but not whether the engine can answer a query — asking the two files the same
#     question would report a proposal as unanswerable that the engine does answer.
#
# What this does NOT reach, measured on the issue's own example. #577 names two queries
# its author found by hand; this generator produces one of them and can never produce
# the other:
#   relation(X, "비교_대상", "신경망_기반_모델")?   generated — 5th of 11, inside the cap
#   relation(X, "이점", "설명가능성_향상")?         never a candidate
# The second is not lost to the cap. The fact is in the reference KB (1 accepted fact),
# but the question writes '해석가능성에서' where the KB writes '설명가능성' — synonyms
# whose shared prefix is ZERO characters ('해' differs from '설' at the first one). So
# no value of _BRIDGE_PREFIX_MIN reaches it: lowering the floor is not the missing
# piece, a synonym relation is, and the author found that pair by knowing the two words
# mean the same thing. What the bridge can reach is #576's design and widening it is
# not this issue's scope (#577 is the proposal mechanism), so this is RECORDED, not
# worked around — half of the issue's evidence is out of reach of the fix it motivated,
# and a reader who does not know that will read the missing proposal as a bug here.
# The pair is not unreachable in general: a question that types the KB's own word
# proposes exactly that query today (measured on '지식그래프를 쓰면서 설명가능성도
# 확보하는 연구는?').

# How many proposals one answer may carry.
#
# Measured on the reference KB (2055 accepted facts) over 300 synthetic two-condition
# questions — '<A> 이면서 <B> 인 것은?', A/B sampled (seed 577) from the 1128
# vocabulary words long enough to bridge:
#   proposals per question: median 3, p75 4, p90 8, p95 14, max 41
#   a SINGLE question word contributes up to 25 of them (p90 7)
#   questions cut, by cap:  4 -> 74   5 -> 56   6 -> 40   7 -> 34   8 -> 27   10 -> 18
#
# That curve is SMOOTH. Six is a judgement on it, not a threshold discovered in it:
# nothing in the data distinguishes 6 from 5 or 7, and this constant should not be read
# as if something did. What the choice buys is stated as a cost, both directions
# measured: at 6, 260 of 300 questions are shown whole, and the 40 that are cut lose
# proposals that were never validated (the block says so). Tightening to 4 doubles the
# cut questions to 74; loosening to 10 leaves 18, at the price of a list of ten long
# query lines to choose between, which is the complaint this cap exists to answer.
#
# The claim NOT made: that a cut question keeps three proposals per condition. Measured
# over the 40 cut at cap 6, the minimum surviving per condition is 1 and the median 2 —
# a condition with two proposals keeps two and the budget goes to the other, which is
# the intended behaviour and not a symmetric split.
#
# What the round-robin does guarantee, and why the cut is not a flat head of one
# ranking: no matched question word is ever silenced while the cap has room. One word
# of two can generate 25 proposals, so a flat cut is free to spend the whole budget on
# ONE condition and drop the other entirely — deleting the decomposition from a
# decomposition proposal. Measured across all 40 cut questions, the minimum per
# condition is 1, never 0.
DECOMPOSITION_CANDIDATE_CAP = 6

# Greppable header. It has to carry the whole contract, because this block is the one
# place in an UNVERIFIED answer that shows engine-answerable queries: a reader who
# takes these for results has been handed verified-looking text that nobody ran.
DECOMPOSITION_HEADER = (
    "SUGGESTION — decomposable single queries. PROPOSALS, not an answer: the row "
    "count below was counted, but no rows were fetched and nothing was answered for "
    "you. Ask one to get a VERIFIED answer:"
)


def decomposition_query(relation: str, obj: str) -> str:
    """The single-predicate query line proposing (relation, object).

    Constants are spelled with json.dumps because that is literally how the parser
    reads them back — common.py's is_quoted_string json-decodes the argument. Naive
    f-string interpolation produces a broken line for any value holding a quote or a
    backslash, and the reference KB has one (`amount(44000,"시간")`, a compound
    literal): the proposal would be shown, be unparseable when pasted, and take the
    reader's trust in the whole block with it.
    """
    return f"relation(X, {json.dumps(relation, ensure_ascii=False)}, {json.dumps(obj, ensure_ascii=False)})?"


def _proposal_order(question: str, accepted: list[dict[str, str]]) -> list[dict[str, object]]:
    """Every (relation, object) the question's words reached, in the order proposed.

    Two facts sharing a (relation, object) are ONE proposal — the proposed query has a
    variable subject, so it would return both of them; listing it twice would present
    the same query as two suggestions.

    Ordering, outermost key first:
      1. round — the proposal's rank inside its own question word's group. Taking
         round 0 of every word before round 1 of any is what makes the cap spend the
         budget across the conditions instead of inside one (see the cap's comment).
      2. the question word's position in the question, so the first condition asked is
         the first condition proposed.
      3. how many of the question's words the pair reached (descending): a pair both
         conditions reached is the closest thing in the KB to the combined question the
         language cannot express, so it leads its group.
      4. how many accepted facts back it, then the pair itself — total and
         deterministic, so a proposal list never depends on dict iteration order.
    """
    terms = _bridge_terms(question)
    pool: dict[tuple[str, str], dict[str, object]] = {}
    for (_subject, relation, obj), hits in _bridged_facts(question, accepted).items():
        entry = pool.setdefault(
            (relation, obj), {"relation": relation, "object": obj, "terms": set(), "backing": 0}
        )
        entry["terms"].update(hits)
        entry["backing"] = int(entry["backing"]) + 1
    groups: dict[int, list[dict[str, object]]] = {}
    for entry in pool.values():
        # The word that OPENS this proposal's group: the earliest one in the question
        # that reached it. Grouping on every matching word instead would list a
        # two-condition pair once per condition.
        primary = min(terms.index(term) for term in entry["terms"])
        entry["primary"] = primary
        groups.setdefault(primary, []).append(entry)
    ordered: list[dict[str, object]] = []
    for group in groups.values():
        group.sort(key=lambda e: (-len(e["terms"]), -int(e["backing"]), e["relation"], e["object"]))
        for round_index, entry in enumerate(group):
            entry["round"] = round_index
            ordered.append(entry)
    ordered.sort(key=lambda e: (e["round"], e["primary"], -len(e["terms"]), e["relation"], e["object"]))
    return ordered


def decomposition_candidates(
    question: str,
    accepted: list[dict[str, str]],
    cap: int = DECOMPOSITION_CANDIDATE_CAP,
) -> dict[str, object]:
    """Engine-answerable single queries the combined *question* decomposes into.

    Returns {'candidates', 'generated', 'not_shown', 'rejected', 'unrenderable'} —
    every count the rendered block needs to state what it left out. Pure: reads only
    the accepted facts passed in (plus the optional policy program classify() already
    consults), writes nothing, and never touches facts/query.dl.

    GATE (수용 기준 1): at least two DISTINCT accepted relation names among the
    generated pairs. One relation is not a decomposition — the question named one
    condition and the wiki answer is the honest response to it. The gate is read off
    the generated pairs, not the surviving ones, because that is the question the
    criterion asks ("how many accepted relation names does this question match").

    VERIFICATION is lazy, in proposal order, and stops once *cap* have been kept: a
    question can generate 41 pairs on the reference KB and each classify() costs ~11 ms
    over 2055 facts, so verifying all of them would spend half a second to throw most
    of the answers away. The consequence is reported rather than hidden — the pairs
    past the cap are counted in 'not_shown' and the rendered line says they were
    neither verified nor shown, which is a weaker claim than "there are 35 more valid
    queries" and the only one this function is entitled to make.
    """
    pool = _proposal_order(question, accepted)
    empty = {"candidates": [], "generated": len(pool), "not_shown": 0, "rejected": 0, "unrenderable": 0}
    if len({entry["relation"] for entry in pool}) < 2:
        return empty
    kept: list[dict[str, object]] = []
    examined = rejected = unrenderable = 0
    for entry in pool:
        if len(kept) >= cap:
            break
        examined += 1
        relation, obj = str(entry["relation"]), str(entry["object"])
        # A value that does not survive _sanitize cannot be proposed at all, and this
        # is the one block where dropping it beats escaping it. Everything else in a
        # wiki answer is quoted evidence, where a sanitized rendering still informs;
        # a query line is meant to be COPIED, so a sanitized one is a lie the reader
        # cannot see. It is also the #576 forging class (an accepted object may hold
        # U+2028 — common.py's reader keeps it deliberately — and str.splitlines()
        # then turns the tail into a top-level line, forging a 'VERIFIED — engine'
        # header inside the UNVERIFIED block). Measured on the reference KB: 0 of 2055
        # accepted facts carry such a character, so this costs nothing there and is
        # asserted against a fixture that does.
        if _sanitize(relation) != relation or _sanitize(obj) != obj:
            unrenderable += 1
            continue
        draft = decomposition_query(relation, obj)
        try:
            decision = classify(draft, accepted)
            # The route check is what enforces 수용 기준 2, and which of these two
            # conditions does that depends on the SHAPE of *accepted* — so both are
            # stated with the measurement that decides them, not with a claim:
            #
            #   route == "engine" — load-bearing TODAY, on a fact list carrying a
            #     status column. classify() reads status (entity_set keeps only
            #     ENGINE_STATUSES rows), evaluate_relation does not, so a
            #     candidate/needs_review/superseded/'' row routes wiki and STILL
            #     answers with its row: measured route=wiki, rows=1 for all four
            #     values. Without this condition that row is proposed as a "verified
            #     row" of a fact the KB has not accepted. cmd_wiki does not reach it
            #     because load_accepted_facts() returns subject/relation/object only
            #     (measured: 2055 of 2055 rows on the reference KB carry no status) —
            #     but load_facts(), used two functions away in fact_signals(), returns
            #     exactly the shape that does. tests/unit/test_ask_decomposition.py's
            #     TestTheValidatorGate pins it, and removing this condition fails those
            #     four cases.
            #   not decision["negative"] — mirrors the scope gate cmd_render branches
            #     on, and changes NO result here: a draft built from a fact that is
            #     present cannot classify as fact_absent, and if it somehow did, the
            #     0-row guard below would drop it anyway. Measured at 279d37f, with the
            #     suites named so the claim can be re-run rather than believed: removing
            #     this half alone moves nothing — tests/unit 6237 passed / 1 skipped,
            #     tests/test_ask_router.sh 258 passed, tests/test_ask_wiki_search.sh 76
            #     passed, all identical to baseline. Removing the ROUTE half instead
            #     fails the four TestTheValidatorGate cases named above — measured on
            #     tests/unit/test_ask_decomposition.py, 4 failed / 24 passed; whether
            #     that mutant also moves anything outside that file was not measured.
            #     The two halves are not interchangeable. This one is kept so the two
            #     call sites read as one contract, not because it decides anything.
            rows = (
                evaluate_relation(draft, accepted)
                if decision["route"] == "engine" and not decision["negative"]
                else []
            )
        except (FactlogError, ValueError):
            # A hand-edited fact file must not turn a wiki answer into a crash; the
            # excerpts above the block are still worth returning.
            rows = []
        if not rows:
            rejected += 1
            continue
        kept.append({"query": draft, "rows": len(rows), "terms": sorted(entry["terms"])})
    return {
        "candidates": kept,
        "generated": len(pool),
        "not_shown": len(pool) - examined,
        "rejected": rejected,
        "unrenderable": unrenderable,
    }


def _decomposition_lines(decomposition: dict[str, object] | None) -> list[str]:
    """The proposal block (#577), or nothing at all.

    Nothing at all is the common case and it is exact: no proposal survived, so the
    answer is byte-identical to the one rendered before this feature existed
    (수용 기준 4). Pure formatting — it decides nothing and re-verifies nothing.
    """
    if not decomposition:
        return []
    candidates = decomposition.get("candidates") or []
    if not candidates:
        return []
    lines = ["", DECOMPOSITION_HEADER]
    for candidate in candidates:
        rows = int(candidate["rows"])
        # The question words are named, not counted: they are why THIS pair is being
        # proposed for THIS question, and a reader who disagrees with the match can
        # only see that from the word.
        reached = ", ".join(str(term) for term in candidate["terms"])
        lines.append(
            f"  {candidate['query']}  — {rows} verified row{'' if rows == 1 else 's'} ← {reached}"
        )
    # Silent truncation is the failure this block is most exposed to: it already shows
    # a short list of long strings, so a reader has no way to notice that a whole
    # condition was dropped. Each cut says its own count and its own reason.
    not_shown = int(decomposition.get("not_shown") or 0)
    if not_shown:
        lines.append(
            f"  … {not_shown} further candidate(s) generated and NOT shown (cap "
            f"{DECOMPOSITION_CANDIDATE_CAP}, filled one per matched question word in "
            "turn). They were not verified, so they may or may not be answerable."
        )
    rejected = int(decomposition.get("rejected") or 0)
    if rejected:
        lines.append(f"  … {rejected} candidate(s) dropped: the query validator did not accept them.")
    unrenderable = int(decomposition.get("unrenderable") or 0)
    if unrenderable:
        lines.append(
            f"  … {unrenderable} candidate(s) dropped: the accepted vocabulary they use "
            "cannot be spelled on a query line."
        )
    lines.append("")
    return lines


# Emitted when most of the question's keywords occur nowhere in the corpus (#575).
# It is a RETRIEVAL report, not an evidence claim: the excerpts above it are real
# matches, but they were selected by a minority of what the user asked. Read without
# this line, that answer is indistinguishable from "the KB has nothing on this" —
# and for a Korean question over English sources the two are opposite conclusions,
# one of which sends the reader away from a KB that does hold the answer.
#
# Distinct from the two neighbouring notices, and never a substitute for either:
#   - 'WARNING: unverified candidates' qualifies the STANDING of what was found;
#     this qualifies the COVERAGE of the search that found it. Both can be true.
#   - NO_QUERY_TERM_NOTE (#571) covers the disjoint case of zero keywords, where
#     there is no ratio to report; _recall_lines returns nothing there.
LOW_RECALL_NOTE = (
    "NOTE: low keyword recall — most of the question's keywords occur nowhere in "
    "the corpus, so any excerpt above was matched by a minority of what you asked. "
    "This is NOT 'no such source': the search may simply have missed wording the "
    "sources use (e.g. a Korean question over English sources). Retry with terms "
    "the sources would use."
)


def _recall_lines(recall: dict[str, list[str]] | None) -> list[str]:
    """The keyword match record for the rendered block (#575).

    Pure formatting over the tally :func:`search` collected; it reads nothing from
    the corpus and decides nothing about the rows shown above it (수용 기준 6).

    THRESHOLD — warn when fewer than half the keywords reached the corpus.
    Measured on the reference KB (187 source files) to bound the choice from both
    sides rather than tune a constant:
      - control, every source's own title asked as the question (59 titles with
        front-matter `title:`): 59/59 at ratio 1.00, zero unmatched keywords. That
        control is near-tautological — the words are literally in the file — so it
        only RULES OUT thresholds, it cannot pick one.
      - a good question can still carry a term the search cannot reach: 'what
        evaluation benchmarks are used for retrieval augmented generation'
        measures 8/9. Warning on any unmatched keyword would fire here, so 'any'
        is out. ('augmented' is not missing from the KB — it is in 5 files, all
        under a sync-ignored path. The denominator is the SEARCHABLE corpus:
        search() skips those files, so no excerpt can ever cite them, and calling
        the term matched would promise evidence that cannot be produced.)
      - the failures #575 exists to expose measure far lower: the issue's own
        question is 2/8 (0.25) and '신경기호 추론의 근거를 어떻게 제시하는가' is
        1/4 (0.25) — both cross-lingual, both returning excerpts that look like an
        answer.
    Half is the only point in the measured gap [0.25, 0.89] that states something
    instead of fitting something: the corpus lacks more of your terms than it has,
    so the minority is driving the answer. Integer arithmetic, no float rounding at
    the boundary — exactly half (1/2, 2/4) does NOT warn.
    """
    if not recall:
        return []
    matched = recall.get("matched") or []
    unmatched = recall.get("unmatched") or []
    total = len(matched) + len(unmatched)
    if total == 0:
        # No keywords at all: NO_QUERY_TERM_NOTE (#571) is the diagnostic for that
        # case and says something this one cannot. Reporting '0/0' beside it would
        # imply a search happened.
        return []
    lines = [f"keywords matched: {len(matched)}/{total}" + (f" — {', '.join(matched)}" if matched else "")]
    if unmatched:
        lines.append(f"keywords unmatched: {', '.join(unmatched)}")
    if len(matched) * 2 < total:
        lines.append(LOW_RECALL_NOTE)
    return lines


def render_wiki_answer(
    question: str,
    reason: str,
    results: list[dict[str, object]],
    grounding: list[dict[str, str]] | None = None,
    did_you_mean: list[dict[str, object]] | None = None,
    limit: int | None = DEFAULT_RENDER_ROW_LIMIT,
    total_results: int | None = None,
    recall: dict[str, list[str]] | None = None,
    decomposition: dict[str, object] | None = None,
) -> str:
    """Render the UNVERIFIED — wiki exploration answer block.

    The literal marker 'UNVERIFIED — wiki exploration' is the greppable token.
    Excerpt citations point only at source text (sources/ , runs/sources/). When
    *grounding* is given, the answer additionally shows a clearly-separated
    'VERIFIED — engine' block of accepted facts about the entities the question
    mentions, so verified anchors sit beside the unverified prose.

    *recall* is the keyword tally from :func:`search`; when given, the block also
    reports which of the question's keywords the corpus contains (#575). Omitting
    it renders exactly the block this function rendered before — the match record
    is additive, so a caller that has no tally never prints a wrong one.

    *decomposition* is :func:`decomposition_candidates`' proposal set (#577), on the
    same additive terms.
    """
    lines = [
        "UNVERIFIED — wiki exploration",
        f"question: {question}",
        f"reason: {reason}",
        "WARNING: unverified candidates — do not treat as confirmed facts.",
    ]
    total_grounding = len(grounding or [])
    visible_grounding = (grounding or []) if limit is None else (grounding or [])[:_render_limit(limit)]
    if grounding:
        lines.append("")
        lines.append("VERIFIED — engine (grounding: accepted facts about mentioned entities):")
        lines.append(f"grounding facts: {total_grounding}")
        lines.extend(f"  - {row['subject']}, {row['relation']}, {row['object']}" for row in visible_grounding)
        truncation = _truncation_line(total_grounding, len(visible_grounding))
        if truncation:
            lines.append(truncation)
        lines.append("")
    lines.append(f"sources searched: {', '.join(label for _rel, label, _grade in _wiki_corpus())}")
    result_total = len(results) if total_results is None else total_results
    lines.append(f"source excerpts: {result_total}")
    # Placed with 'sources searched' / 'source excerpts' — the three lines together
    # say where the search looked, what of the question it looked for, and what it
    # got — and BEFORE the excerpts, so the coverage caveat is read before the
    # excerpts it qualifies, not after the reader has already drawn a conclusion.
    lines.extend(_recall_lines(recall))
    # The proposals sit between the recall report and the excerpts, and that position
    # is the argument: #575's line says the corpus does not spell most of what you
    # asked and to retry with wording it does use; these are that wording, taken from
    # the engine's own vocabulary. Read as one thought they are a diagnosis and its
    # remedy. Appended after the excerpts instead, the remedy lands below up to 20
    # citations — off-screen in the case it exists for. The excerpts still follow
    # their coverage caveat, which is #575's own placement contract.
    lines.extend(_decomposition_lines(decomposition))
    visible_results = results if limit is None else results[:_render_limit(limit)]
    if visible_results:
        for r in visible_results:
            header = f"[{r['file']}:{r['line']}] ({r['dir']})"
            # A bridged excerpt (#576) says where it came from ON the citation header,
            # before the prose it qualifies — the reader must not first read English
            # source text that no keyword of theirs matched and work out afterwards why
            # it is here. `.get` because render_wiki_answer is called with rows built
            # by other callers (and by tests) that carry only the four base keys.
            via = r.get("via") if isinstance(r, dict) else None
            if via:
                header += f" {VIA_KB_VOCABULARY_TAG}"
            lines.append(header)
            if via:
                # The accepted facts are named individually, not summarized: they are
                # the entire justification for this file being in the answer, and a
                # count would leave the reader unable to check the bridge's judgement.
                #
                # _sanitize is load-bearing, not hygiene. An accepted OBJECT may hold
                # U+2028/U+2029/U+0085 — common.py's reader keeps them deliberately
                # ("routine in text copied from PDFs/web"), and the compiler does not
                # reject them the way it rejects the C0 controls. Interpolated raw,
                # str.splitlines() breaks this string on one, and the tail becomes its
                # own top-level line: an object ending '… VERIFIED — engine (grounding:
                # …)' then renders a forged VERIFIED header inside the UNVERIFIED
                # block, which is the one contract this whole feature is built around.
                # Every other line in this block is either a literal or already went
                # through _sanitize in search(); this was the only raw KB string.
                for fact in via.get("facts", []):
                    lines.append(f"    ← accepted: {_sanitize(str(fact))}")
            for excerpt_line in str(r["excerpt"]).splitlines():
                lines.append(f"    {excerpt_line}")
    elif not _keyword_patterns(question):
        # Empty for two different reasons; say which one (#571).
        lines.append(NO_QUERY_TERM_NOTE)
    else:
        lines.append("(no matching source excerpts found)")
    truncation = _truncation_line(result_total, len(visible_results))
    if truncation:
        lines.append(truncation)
    for hint in did_you_mean or []:
        suggestions = ", ".join(str(value) for value in hint["suggestions"])
        lines.append(
            f"note: no accepted {hint['kind']} '{hint['term']}'. did you mean: {suggestions}?"
        )
    return "\n".join(lines)


def record_open_question(question: str, root: Path) -> Path:
    """Append an unanswered question to a NON-engine-input sink for later review.

    Writes to decisions/ask-open-questions.md (not guarded by the PreToolUse
    gate, never engine input), so interactive ask never touches facts/query.dl.
    Idempotent: a question already present is not duplicated.
    """
    question = " ".join(question.split())  # collapse newlines/runs so one bullet
    sink = root / "decisions" / "ask-open-questions.md"
    if not question:
        return sink  # nothing to record
    sink.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# Ask — open questions\n\n"
        "Unanswered `/factlog ask` questions, kept for later review. This file is\n"
        "NOT engine input; promote items into policy/questions.md deliberately.\n"
    )
    text = sink.read_text(encoding="utf-8") if sink.is_file() else header
    bullet = f"- {question}\n"
    if bullet not in text:
        if not text.endswith("\n"):
            text += "\n"
        text += bullet
    sink.write_text(text, encoding="utf-8")
    return sink


def cmd_validate(args: argparse.Namespace) -> int:
    facts = load_accepted_facts()
    print(json.dumps(classify(args.draft, facts), ensure_ascii=False))
    return 0


def cmd_evaluate(args: argparse.Namespace) -> int:
    facts = load_accepted_facts()
    try:
        result = evaluate(args.draft, facts)
    except NotImplementedError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False))
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    """Validate + (engine) evaluate + render. Wiki rendering is out of scope for
    this module; for route=wiki this prints a machine-readable directive so the
    caller (the skill) can run wiki exploration."""
    facts = load_accepted_facts()
    decision = classify(args.draft, facts)
    if decision["route"] == "engine":
        # A verified negative is proven by the validator regardless of predicate,
        # so it is always renderable as an engine answer — never demoted.
        if decision["negative"]:
            print(render_engine_answer(args.draft, []))
            # Additive coverage hint (#189): if this verified-negative relation
            # query has an accepted subject that carries fact(s) under OTHER
            # relations, surface a predicate-mismatch note. The verdict block above
            # is untouched — this is an extra line, not a change to the answer.
            hint = coverage_hint(args.draft, facts)
            if hint:
                print(hint)
        else:
            # Positive engine answer: relation, path, and policy predicates are all
            # evaluated by the engine and rendered (0 rows -> a verified-empty
            # result, never a wiki fallback).
            result = evaluate(args.draft, facts)
            if result.get("policy_unevaluable"):
                # A policy predicate needs the engine, but the hand-authored policy
                # could not be evaluated (broken logic-policy.extra.dl). Do NOT
                # render an empty engine answer — that would fake a verified
                # negative. Degrade to a wiki directive + a warning, rc 0: ask never
                # crashes or hard-fails on a human extra.dl mistake (#193).
                print(json.dumps(
                    {
                        "route": "wiki",
                        "reason": "policy unevaluable — logic-policy.extra.dl could not be evaluated",
                        "policy_uncompiled": decision["policy_uncompiled"],
                    },
                    ensure_ascii=False,
                ))
                print(POLICY_UNEVALUABLE_WARNING.format(reason=result["policy_unevaluable"]))
                return 0
            # Answer-quality signals (sources/extraction-conf/staleness) annotate
            # relation rows only (the (s,r,o) key is a relation triple); gate on the
            # predicate so path/policy rows are never annotated by a coincidental
            # 3-element shape.
            is_relation = decision["predicate"] == "relation"
            signals = (
                fact_signals(load_facts(), Path(os.environ["FACTLOG_ROOT"]))
                if is_relation and CANDIDATES_CSV.is_file()
                else None
            )
            print(render_engine_answer(
                args.draft,
                result["rows"],
                signals,
                annotate_objects=is_relation,
                limit=None if args.all else DEFAULT_RENDER_ROW_LIMIT,
                project=not args.all,
            ))
        # The engine answer is real, but if the author wrote policy rules and
        # never compiled them, the engine had no policy to apply — say so, so a
        # policy-free answer is not mistaken for a policy-checked one (#193).
        if decision["policy_uncompiled"]:
            print(POLICY_UNCOMPILED_WARNING)
        return 0
    # route == wiki: emit a machine-readable directive so the caller runs wiki
    # exploration. Always carry policy_uncompiled (same schema as `validate`), so
    # the caller can surface the same warning the wiki answer appends.
    print(json.dumps(
        {
            "route": "wiki",
            "reason": decision["reason"],
            "policy_uncompiled": decision["policy_uncompiled"],
            "did_you_mean": did_you_mean_hints(args.draft, facts),
        },
        ensure_ascii=False,
    ))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    root = Path(os.environ["FACTLOG_ROOT"])
    if args.all:
        results = search(args.text, root, limit=None)
        total = len(results)
    else:
        # Keep the existing top-10 retrieval/reranking behaviour for callers of
        # the stable ``results`` array.  The additive fields make the cap visible.
        results = search(args.text, root)
        total = len(search(args.text, root, limit=None))
    # 'diagnostic' is null unless the empty result needs an explanation the row
    # count cannot give: zero rows because zero keywords, not because zero matches
    # (#571). Additive, like total/truncated — the results array is unchanged.
    print(json.dumps(
        {
            "results": results,
            "total": total,
            "truncated": len(results) < total,
            "diagnostic": None if _keyword_patterns(args.text) else NO_QUERY_TERM_NOTE,
        },
        ensure_ascii=False,
    ))
    return 0


def cmd_wiki(args: argparse.Namespace) -> int:
    root = Path(os.environ["FACTLOG_ROOT"])
    # The tally is corpus-wide whichever call fills it: search() records a keyword
    # the moment a scanned line contains it, and `limit` is applied after the scan
    # ends. So the capped call below reports the same recall the uncapped one does —
    # the render cap cannot make recall look worse than it is (#575), which would
    # have turned this diagnostic into the false alarm it exists to prevent.
    recall: dict[str, list[str]] = {}
    if args.all:
        results = search(args.text, root, limit=None, recall=recall)
        total_results = len(results)
    else:
        results = search(args.text, root, recall=recall)
        total_results = len(search(args.text, root, limit=None))
    # Grounding: accepted facts about mentioned entities (empty if not compiled yet).
    accepted = load_accepted_facts() if ACCEPTED_DL.is_file() else []
    grounding = grounding_facts(args.text, accepted)
    hints = did_you_mean_hints(args.draft, accepted) if args.draft else []
    # --all is deliberately NOT wired to the proposal cap. The other caps hide ROWS of
    # one answer, so lifting them is lossless; this one bounds how many queries a
    # reader is asked to choose between, and 41 of them is the defect the cap exists
    # for, not a shorter view of it. The suppressed count is printed instead.
    print(render_wiki_answer(
        args.text,
        args.reason,
        results,
        grounding,
        hints,
        limit=None if args.all else DEFAULT_RENDER_ROW_LIMIT,
        total_results=total_results,
        recall=recall,
        decomposition=decomposition_candidates(args.text, accepted),
    ))
    # A wiki answer is already UNVERIFIED, but an uncompiled-but-authored policy
    # is a separate, actionable defect the author should fix — surface it (#193).
    if _policy_uncompiled():
        print(POLICY_UNCOMPILED_WARNING)
    return 0


def cmd_note(args: argparse.Namespace) -> int:
    root = Path(os.environ["FACTLOG_ROOT"])
    sink = record_open_question(args.text, root)
    print(json.dumps({"recorded": args.text, "sink": sink.relative_to(root).as_posix()}, ensure_ascii=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ask_router", description="Deterministic /factlog ask router")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func, helptext in (
        ("validate", cmd_validate, "classify a draft query to engine vs wiki (JSON)"),
        ("evaluate", cmd_evaluate, "evaluate a relation query against accepted facts (JSON)"),
        ("render", cmd_render, "validate+evaluate+render the engine answer, or emit a wiki directive"),
    ):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("draft", help="the candidate Datalog query line")
        p.add_argument("--all", action="store_true", help="show every answer row (no renderer cap)")
        p.add_argument(*_ROOT_FLAGS, dest="target", default=None, metavar="PATH", help=_ROOT_FLAG_HELP)
        p.set_defaults(func=func)

    # Path B (wiki) subcommands take the natural-language question, not a draft.
    search_p = sub.add_parser("search", help="search the wiki corpus (sources/ + runs/sources/) (JSON)")
    search_p.add_argument("text", help="the natural-language question")
    search_p.add_argument("--all", action="store_true", help="return every matching excerpt")
    search_p.add_argument(*_ROOT_FLAGS, dest="target", default=None, metavar="PATH", help=_ROOT_FLAG_HELP)
    search_p.set_defaults(func=cmd_search)

    wiki_p = sub.add_parser("wiki", help="render the UNVERIFIED — wiki exploration answer")
    wiki_p.add_argument("text", help="the natural-language question")
    wiki_p.add_argument("--reason", default="not expressible over accepted facts", help="why the engine path did not apply")
    wiki_p.add_argument("--draft", default=None, help="validated draft query; append display-only spelling hints when eligible")
    wiki_p.add_argument("--all", action="store_true", help="show every excerpt and grounding row")
    wiki_p.add_argument(*_ROOT_FLAGS, dest="target", default=None, metavar="PATH", help=_ROOT_FLAG_HELP)
    wiki_p.set_defaults(func=cmd_wiki)

    note_p = sub.add_parser("note", help="record an unanswered question to the non-engine-input sink")
    note_p.add_argument("text", help="the natural-language question")
    note_p.add_argument(*_ROOT_FLAGS, dest="target", default=None, metavar="PATH", help=_ROOT_FLAG_HELP)
    note_p.set_defaults(func=cmd_note)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # An empty flag value is refused rather than dropped to the next tier (#546):
    # `--target "$FACTLOG_ROOT"` in a shell that never exported the variable is
    # exactly this shape, and falling through would route the ask at the configured
    # KB while the caller believes they named one.
    if args.target is not None and not args.target.strip():
        print(
            "ask_router: the KB-root flag (--target/--wiki) was empty; pass a KB path, "
            "or pass no flag at all to use the active KB.",
            file=sys.stderr,
        )
        return 1
    return args.func(args)


if __name__ == "__main__":
    from common import run_cli

    raise SystemExit(run_cli(main))
