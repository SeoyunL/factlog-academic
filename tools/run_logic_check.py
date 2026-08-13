#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run deterministic logic checks over facts and query drafts.

Usage:
    python3 run_logic_check.py [--wiki <kb>]
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import tempfile
from pathlib import Path

_TOOLS_DIR = Path(__file__).resolve().parent
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


# Resolve the KB root and export it before importing common, which binds
# its module-level paths from FACTLOG_ROOT at import time.
import factlog_config  # noqa: E402

os.environ["FACTLOG_ROOT"] = factlog_config.resolve_root_from_argv("--wiki")

from common import (  # noqa: E402
    FACTS_DIR,
    KNOWN_STATUSES,
    QUERY_PREDICATES,
    allowed_relations,
    attribute_relations,
    dependency_path,
    entity_set,
    value_set,
    ensure_dirs,
    kb_query_spellings,
    resolve_query_spellings,
    load_accepted_facts,
    load_facts,
    load_logic_policy,
    policy_predicates,
    review_facts,
    LOGIC_POLICY_DL,
    run_wirelog,
    arg_value,
    is_quoted_string,
    query_arity_error,
    query_args,
    query_shape_error,
    quoted_constants,
)


def query_lines() -> list[str]:
    query_file = FACTS_DIR / "query.dl"
    if not query_file.exists():
        return []
    return [
        line.strip()
        for line in query_file.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("//")
    ]


def query_constants(line: str) -> list[str]:
    """Every quoted-literal ARGUMENT of *line*, decoded — the quote-aware twin of
    ``common.quoted_constants``.

    Use this, not ``quoted_constants``, wherever a line is read BEFORE and AFTER
    ``resolve_query_spellings``. That function re-quotes through ``json.dumps``,
    so resolving onto a value that itself carries a ``"`` puts a ``\\"`` into the
    query string; ``quoted_constants``' raw ``"([^"]+)"`` splits on the escape and
    returns a different NUMBER of constants for the same line. Measured on
    ``relation("예산안", R, "amount(1000,億)")?`` over a KB holding
    ``amount(1000,"億")``: ``['amount(1000,億)']`` written against
    ``['amount(1000,\\\\', ')']`` resolved. Every canonical ``amount`` value carries
    a quote — it is the form merge stores — so this is the ordinary case for any
    query naming an amount, not an exotic one.

    Going through ``query_args``/``is_quoted_string``/``arg_value`` decodes with
    ``json.loads`` instead of scanning for quote characters, so the two readings
    match by construction: ``resolve_query_spellings`` rebuilds the line from the
    same argument list, substituting in place.

    Decoding is also what the vocabulary tests want: *entities* is ``value_set``,
    which holds values as the KB decodes them, so a query constant must be
    compared decoded rather than as the raw text between quotes.

    NOT a drop-in replacement for ``quoted_constants`` everywhere. It sees
    arguments, so a line that does not parse as an atom, or whose argument does
    not parse as one quoted string, yields nothing where the regex still returned
    text. Every caller here reads a line ``query_error`` has already accepted.
    """
    return [arg_value(arg) for arg in query_args(line) if is_quoted_string(arg)]


def path_endpoints(line: str) -> list[str]:
    """The quoted endpoints of a path query, INCLUDING an empty literal.

    `quoted_constants` matches `"([^"]+)"`, so it drops `""` entirely: with it,
    `path("갑봇", "")?` yielded one constant, failed the `len(constants) >= 2`
    gate, and vanished from the report — no result line, no warning — while
    `ask`'s router answers the same query with a reason. #329 is what made `""`
    a graph node that is NOT an accepted entity, so the report has to say so."""
    return query_constants(line)


def display_value(value: str) -> str:
    """A value as it should read in the report. The empty string is rendered
    `""` so `path 갑봇 -> "" ` does not print as a dangling arrow.

    ``one_line`` because this is the rendering helper for values DECODED out of
    a query: ``arg_value`` is ``json.loads``, so ``"a\\nstatus: ..."`` in
    facts/query.dl — one physical line, the escape written as two ordinary
    characters — decodes to a real newline. Every caller that renders a decoded
    query value goes through here or is wrapped at its own call site."""
    return one_line(value) if value else '""'


# Query parsing is delegated to common's string-aware parsers
# (_query_args / _arg_value / _quoted_constants, imported above) so this engine
# and the ask router agree on every query — notably commas inside quoted literals
# like relation("A", "born_in", "Paris, France")?, which a naive split(",") would
# mis-count as 4 args and report as "0 rows".


def relation_results(line: str, facts: list[dict[str, str]]) -> list[tuple[str, str, str]]:
    args = query_args(line)
    if len(args) != 3:
        return []
    fields = ["subject", "relation", "object"]
    rows: list[tuple[str, str, str]] = []
    for row in facts:
        matched = True
        for arg, field in zip(args, fields, strict=True):
            if is_quoted_string(arg) and arg_value(arg) != row[field]:
                matched = False
                break
        if matched:
            rows.append((row["subject"], row["relation"], row["object"]))
    return rows


def query_error(label: str, line: str) -> str | None:
    """The report's single verdict on *line*'s SIGNATURE, or None when answerable.

    Neither rule is restated here: ``common.query_arity_error`` and
    ``common.query_shape_error`` are the same two functions ``classify_query``
    applies, in the same order, so the gate and the report cannot disagree about
    which lines are answerable nor word the verdict differently. They used to, on
    every predicate:

    - ``count`` was checked on ARITY ONLY, so ``count("S", 'r')?`` reached
      ``evaluate_queries``, where a non-double-quoted argument is treated as a
      WILDCARD rather than a filter. The count then ranged over every relation of
      that subject and was printed as an engine-verified aggregate (#328). An
      aggregate is the output a reader is least able to check by eye, which is
      why answering it wrongly is worse than not answering it.
    - a policy query was checked on arity only, so ``stale_entity(Alice, stale)?``
      had both bare tokens taken for variables and rendered the predicate's WHOLE
      extent with invented bindings (``Alice=Bob``).
    - ``relation`` and ``path`` were checked on NEITHER rule — they fell through
      to the generic warning loop. Same mechanism, wider blast radius:
      ``relation("Marie Curie", 'born_in', O)?`` reported rows spanning every
      relation of the subject, each carrying a nonsense binding
      (``'born_in'=worked_at``), and an arity violation was answered as a
      confident NEGATIVE about a fact that is in the KB:

          relation("Marie Curie", "born_in", "Warsaw", X)?
            gate   -> bad_arity
            report -> relation results: 0 rows
          path("Marie Curie", "Warsaw", "Poland")?
            gate   -> bad_arity
            report -> path Marie Curie -> Warsaw: Marie Curie -> Warsaw

      A dropped extra argument is a plausible typo, and both answers read as
      engine-verified.

    ARITY IS CHECKED FIRST, which is not interchangeable with the other order:
    it decides the DIAGNOSIS a line that breaks both rules receives. Shape-first
    told the author of ``relation()?`` that "arguments must be variables or
    quoted strings" — advice about the quoting of arguments that are not there.

    One verdict serves both the Errors section (``validate_query``) and the
    answer renderers (``evaluate_queries``, ``policy_result_line``), so the
    report cannot call a line an error and answer it in the same run.

    Message wording is the gate's, with the offending line appended — the
    convention every other error in this module follows, ``one_line`` included:
    the appended text is the RAW query line, not a decoded value, and the raw
    line is its own carrier (see ENGINE_FAILED_STATUS_LINE).
    """
    args = query_args(line)
    message = query_arity_error(label, args) or query_shape_error(label, args)
    return f"{message}: {one_line(line)}" if message else None


def validate_query(
    line: str,
    entities: set[str],
    policy_query_predicates: set[str],
    path_nodes: set[str] | None = None,
    spelling: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Validate one query line against the KB vocabulary.

    *entities* is value_set — entities AND literal values — because a relation
    query's object may legitimately be a literal. *path_nodes* is the narrower
    entity_set: a path node must be an entity, which is what classify_query
    enforces for `ask`. ``None`` means "do not distinguish the two" and keeps the
    pre-#329 behaviour for the callers that pass three arguments.

    *spelling* is ``kb_query_spellings``. Every VOCABULARY test below reads the
    resolved line, so a constant naming a value accepted.dl holds under its other
    canonically equivalent spelling is not warned about as absent — the same
    resolution the gate and the engine apply, so all three agree about which
    constants the KB carries. ``None`` means "do not resolve", which is what a
    caller passing four arguments gets.

    Every MESSAGE keeps the line and the constant as the author wrote them: the
    report is an artifact a reader compares to their own query file.

    Signature errors come first, from ``query_error``: a line the gate refuses on
    arity or shape is reported as an error and never also warned about or
    answered (#328).
    """
    errors: list[str] = []
    warnings: list[str] = []
    resolved = line if spelling is None else resolve_query_spellings(line, spelling)
    predicate = line.split("(", 1)[0]
    if predicate not in QUERY_PREDICATES and predicate not in policy_query_predicates:
        errors.append(f"query unknown predicate: {one_line(line)}")
        return errors, warnings
    if not line.endswith("?"):
        errors.append(f"query must end with ?: {one_line(line)}")
    if predicate == "review_required":
        constants = quoted_constants(line)
        if len(constants) != 1:
            errors.append(
                f"review_required must include the original question string: {one_line(line)}"
            )
        return errors, warnings
    if predicate in policy_query_predicates:
        policy_error = query_error("policy query", line)
        if policy_error:
            errors.append(policy_error)
            return errors, warnings
        args = query_args(line)
        # The membership test reads the RESOLVED constant and the message names
        # the WRITTEN one, deliberately and in that order: 4428952 and fc98675
        # are what made the gate and `ask` judge vocabulary on the spelling
        # accepted.dl actually holds, and this is the report's copy of that
        # decision. NO TEST GUARDS IT — mutating `query_args(resolved)[0]` back to
        # `args[0]` passes the whole suite (measured: 1868 passed), silently
        # undoing both commits and returning the report to warning that the KB has
        # never heard of a value it holds under its other spelling.
        #
        # Nor is the safety of reading *resolved* here enforceable at this level.
        # It rests on a CALLER-level invariant: validate_query takes *entities*
        # and *spelling* as independent parameters and nothing inside it checks
        # that the map's values are a subset of *entities*. build_report_text
        # derives both from the same rows, which is what makes the subset hold —
        # a caller that derives them from different rows breaks it silently, here
        # and at the vocabulary loop below.
        if is_quoted_string(args[0]) and arg_value(query_args(resolved)[0]) not in entities:
            warnings.append(
                f"query references non-engine entity: {one_line(arg_value(args[0]))}"
            )
        return errors, warnings
    if predicate == "count":
        # count(subject, relation)? — engine-verified aggregate (see evaluate_queries).
        count_error = query_error("count", line)
        if count_error:
            errors.append(count_error)
            return errors, warnings
        # A well-formed count falls through to the shared warning loop below, so
        # a subject or relation the engine does not carry gets the same
        # "non-engine entity or relation" warning relation/path queries get. It
        # used to return here, which left the report's most misreadable answer —
        # `0 (distinct objects)`, indistinguishable from a verified zero — as the
        # only signal that the query named something the KB has never heard of.
    elif predicate in {"relation", "path"}:
        signature_error = query_error(predicate, line)
        if signature_error:
            errors.append(signature_error)
            return errors, warnings
        # SIGNATURE first, then vocabulary: a path query the gate refuses on arity
        # or shape has already returned, so the endpoint check below never sees a
        # half-parsed argument and the same line is never both an error and a
        # warning (#328 + #329).
        if predicate == "path" and path_nodes is not None:
            # A path node must be an ENTITY. The object of a declared attribute
            # relation is a literal value: it is in the KB (so the generic check
            # below stays silent) but cannot sit on a path. classify_query refuses
            # the same query outright — say why here too, rather than letting the
            # result line answer "(not found)", which reads as "the facts do not
            # connect them" (#329).
            for constant, tested in _paired_constants(
                path_endpoints(line), path_endpoints(resolved)
            ):
                # `not constant` covers the empty string, which value_set drops, so
                # the generic unknown-constant check below is silent on it as well —
                # without it that endpoint drew no diagnostic anywhere.
                if tested not in path_nodes and (tested in entities or not tested):
                    warnings.append(
                        f"query path argument is not an accepted entity: {display_value(constant)}"
                    )
    # DEFENSIVE, unlike its sibling above — this pairing cannot currently change
    # any message, and that is structural rather than accidental.
    # ``build_report_text`` derives *entities* (``value_set(facts)``) and
    # *spelling* (``kb_query_spellings(facts)``) from the SAME rows, so the map's
    # values are a subset of *entities*: a constant that resolved is necessarily
    # in *entities* and this warning cannot fire for it. ``constant`` and
    # ``tested`` therefore only ever differ on constants this branch stays silent
    # about. Mutating ``{constant}`` to ``{tested}`` here survives the suite for
    # that reason (re-measured: 1868 passed); do not go looking for the pin that
    # would catch it. It is written this way so the rule "a message names what the
    # author wrote" holds by construction at every site, and so a future caller
    # passing a map derived from other rows cannot reintroduce the mismatch. The
    # path-endpoint pairing above IS load-bearing — entity_set is narrower than
    # value_set, so a resolved constant can still be warned about there — and is
    # pinned.
    #
    # ``query_constants``, NOT ``quoted_constants``: both sides of a pairing must
    # be read by the SAME parser. Reading them with the raw regex made this
    # pairing DESYNC on every query that resolved onto a canonical ``amount``, at
    # which point the paragraph above was true and irrelevant — the fallback
    # discarded resolution before the subset argument could apply. See
    # ``_paired_constants``.
    #
    # Two cheaper guards look right and are not, and both were measured before
    # this was settled. Neither is worth trying again.
    #
    #   - "keep the regex, but fall back to it only when the parser finds FEWER
    #     constants". The regex OVER-counts the resolved line — 3 against the
    #     parser's 2 on ``count("amount(1000,億)", "규모")?``, because it splits
    #     the ``\"`` that ``json.dumps`` wrote — so the floor picks the wrong list
    #     and the bug comes straight back. The regex is not merely incomplete on
    #     escaped text; it is wrong in BOTH directions, which is why it cannot
    #     serve as a floor, a ceiling or a cross-check anywhere.
    #   - "regex on the written side, parser on the resolved side", which does fix
    #     the amount case. It desyncs on an EMPTY literal instead: the regex drops
    #     ``""`` and the parser keeps it, so ``relation("a", "r", "")?`` reads 2
    #     against 3 and loses resolution on a line with nothing wrong with it.
    for constant, tested in _paired_constants(
        query_constants(line), query_constants(resolved)
    ):
        if constant and tested not in entities and tested not in {"S", "R", "O", "X", "Q"}:
            warnings.append(
                f"query references non-engine entity or relation: {one_line(constant)}"
            )
    return errors, warnings


def _paired_constants(written: list[str], resolved: list[str]) -> list[tuple[str, str]]:
    """Zip a line's constants with the same line's after spelling resolution:
    ``(what the author wrote, what to test against the KB vocabulary)``.

    When the lists disagree in length a diagnostic must not be attributed to the
    wrong constant, so this ABANDONS RESOLUTION for the line and pairs each
    constant with itself — the unresolved reading, which is the pre-existing
    behaviour, so the fallback can only warn where the old code warned.

    **This branch used to be documented as unreachable, and it fired.** The
    argument given was "resolution substitutes in place and never adds or drops
    an argument", which is true — and did not apply, because the callers were not
    counting arguments. Two of them scanned the line with ``quoted_constants``'
    raw ``"([^"]+)"``, which cannot see JSON escaping, while
    ``resolve_query_spellings`` re-quotes through ``json.dumps``. Resolving onto
    any value that itself carries a ``"`` therefore changed the COUNT of what the
    regex found, and every canonical ``amount`` value carries one. The reader who
    trusted the sentence would have looked for a mistake in the substitution and
    found none.

    Every caller now reads both sides with ``query_constants``, and the reason
    the branch does not fire is the one the old sentence claimed: the resolved
    line is REBUILT from ``query_args(line)`` with per-position substitutions that
    preserve "this argument is a quoted string", so re-parsing it yields the same
    number of constants by construction. Measured over 300k generated lines
    against a map that actually moves constants: of the 23002 that were rewritten,
    the regex reading disagreed on 14720 and the parser reading on 0.

    So the branch is kept, and the invariant it now guards is narrower and
    stated where it can be checked: **both sides must be read by the same
    parser.** That is the thing that broke, not arity. A caller that reaches for
    ``quoted_constants`` here again, or hands in two lists produced by different
    readers, lands in this fallback — silently, since it degrades rather than
    raises. It is also a module-level helper with no guard on its arguments, so
    "unreachable" was never a property of the function, only of its callers.

    ``common.classify_query``'s ``_shown`` guard degrades the same way for the
    same reason. The two were written pointing in opposite directions — one
    reverting the display, the other the evaluation — which would have made a
    desync produce a gate and a report that disagree about the same line.

    Three sites read the resolved line by direct index instead —
    ``validate_query``'s policy branch, ``policy_result_line``'s ``filter_args``,
    and the ``count`` branch of ``evaluate_queries``. They would RAISE on a
    desync rather than degrade, which is the opposite of the rule above. Left as
    they are on purpose: each unpacks a fixed arity that ``query_error`` has
    already accepted, so a guard there would be dead code shaped like a policy,
    and none of them attributes a message to a constant — the failure this
    function exists to prevent. If ``resolve_query_spellings`` ever stops
    preserving arity, those three are where it surfaces."""
    if len(written) != len(resolved):
        return [(constant, constant) for constant in written]
    return list(zip(written, resolved, strict=True))


# The two validate_query warnings that fire on a constant absent from the KB
# vocabulary. A relation NAME is legitimate vocabulary, so a warning about one is
# dropped by the caller.
_UNKNOWN_CONSTANT_PREFIXES = (
    "query references non-engine entity or relation: ",
    "query references non-engine entity: ",
)


def names_a_relation(warning: str, relations: set[str]) -> bool:
    """True when *warning* is an unknown-constant warning about a relation NAME.

    Matching the prefix matters: filtering on `rsplit(": ", 1)[-1]` tested the
    tail of EVERY warning, so an unrelated warning whose value happened to equal
    a relation name — `query path argument is not an accepted entity: <name>` —
    was silently dropped. Stripping the known prefix instead also keeps a value
    that itself contains ": " intact."""
    for prefix in _UNKNOWN_CONSTANT_PREFIXES:
        if warning.startswith(prefix):
            return warning[len(prefix):] in relations
    return False


def policy_row_matches(args: list[str], row: tuple[str, ...] | list[str]) -> bool:
    """True when *row* satisfies every quoted constant *args* pins, by position.

    A quoted constant is a FILTER, at whatever position it appears — not merely a
    binding the display omits. Filtering only args[0] would still let
    ``pred(E, "stale")?`` report the other reasons' rows, and would do so while
    the first-argument form answers correctly, which is worse than filtering
    nothing: the reader loses the one signal that the second line is untrustworthy.

    A row shorter than the pinned position cannot satisfy the constant, so the
    0-arity row an engine may emit is dropped from a constant-pinned query (it
    still shows up for an all-variable query, as before).

    Comparison is RAW (``arg_value`` only), deliberately not
    ``common.canonical_value``: it mirrors ask_router's policy branch exactly so
    the report and ``ask`` cannot diverge, which is the property
    tests/unit/test_policy_query_filter.py pins. This means the policy path does
    NOT go through the "#213 single query-value comparison chokepoint"
    (common.py `_canonical_value`), contrary to what that docstring claims for
    every query-match path. Measured consequence: an NFD-stored entity queried
    with an NFC-typed constant now yields `0 rows`, which reads as a verified
    negative. Folding both sides belongs in one place for BOTH paths and is out
    of scope here; see #213.

    The matching rule is kept identical to ask_router's `policy_row_matches`
    (same body, module-specific docstring). The natural home is common.py
    alongside the other query-parsing helpers, but hoisting it there is a wider
    change than this fix needs; the report/router parity test fails if the two
    copies ever drift. Two of its cases carry that load — the 0-arity row and the
    NFD-stored/NFC-queried entity. Every other case is a 2-column ASCII row,
    which a copy that lost the short-row guard, or that folded to NFC on its own,
    still gets right; do not delete those two.
    """
    for index, arg in enumerate(args):
        if not is_quoted_string(arg):
            continue
        if index >= len(row) or arg_value(arg) != row[index]:
            return False
    return True


def policy_result_line(
    predicate: str,
    line: str,
    inferred: dict[str, set[tuple[str, ...]]],
    resolved: str | None = None,
) -> str | None:
    """Render one policy query's result, or None when the query is malformed.

    The test is `query_error`, the SAME verdict validate_query puts in the
    Errors section, so a line the report is rejecting never also receives an
    answer here. Four shapes reach this function malformed, and each used to be
    answered:

    - unparseable (no trailing '?'): `query_args` returns [], no constant is
      pinned, so the filter passes everything -> the whole extent;
    - `pred()?` -> one empty arg, likewise unfiltered -> the whole extent;
    - `pred("Alice")?` / `pred("Alice", R, "zzz")?` -> wrong arity, filtered by
      whatever constants happen to line up -> a plausible but meaningless count;
    - `pred(Alice, stale)?` -> right arity, but neither bare token is a variable
      or a quoted string, so `policy_row_matches` pins nothing and every row is
      rendered against them: the whole extent again, this time with `Alice=Bob`
      bindings that name an entity the row is not about (#328).

    "No usable args" is not "no constants to honour" — it means the query was
    never understood, so answering it invents an answer for a line the report is
    simultaneously calling an error. Emitting nothing leaves the Errors section
    to speak. ask_router.evaluate raises NotImplementedError on the same shapes,
    so neither path answers a malformed policy query.

    The query is echoed ONLY when a quoted constant is pinned. Such a line and
    the "Policy evaluation:" extent line ("<pred>: N rows", the count over ALL
    entities) sit a few lines apart and now legitimately disagree — 3 rows there,
    0 rows here — so naming the query that produced the 0 is what makes the pair
    readable as scope rather than contradiction, and it tells two queries on the
    same predicate apart. A variable-only query cannot produce that mismatch (it
    reports the extent, which is what the extent line says), so it keeps its
    original text byte for byte — the query-shape whose output this fix promised
    not to change. The extent line itself is left untouched: it is pinned by
    tests/golden/logic_report.txt, and its section header already says it is the
    policy evaluation rather than the answer to any one query.
    """
    if query_error("policy query", line) is not None:
        return None
    args = query_args(line)
    # policy_row_matches compares RAW at EVERY position (deliberately, so the
    # report and ask_router filter identically), so the constants it sees must
    # already carry the KB's spelling. *resolved* is *line* with its value
    # constants moved onto the spellings accepted.dl holds; positions past the
    # first hold engine-derived reason codes, which are absent from the value map
    # and pass through untouched. None keeps the unresolved reading for the
    # three-argument callers.
    filter_args = args if resolved is None else query_args(resolved)
    rows = [row for row in sorted(inferred[predicate]) if policy_row_matches(filter_args, row)]
    values: list[str] = []
    for row in rows:
        bindings = []
        for arg, value in zip(args, row, strict=False):
            # With the shape guard above, an arg is a variable or a quoted
            # string, so is_quoted_string is exactly "not a variable" — the
            # predicate policy_row_matches already uses, said the same way.
            if not is_quoted_string(arg):
                bindings.append(f"{arg}={one_line(value)}")
        values.append(", ".join(bindings) if bindings else ", ".join(one_line(v) for v in row))
    suffix = "; " + "; ".join(values) if values else ""
    echo = f" (query: {one_line(line)})" if any(is_quoted_string(arg) for arg in args) else ""
    return f"{predicate} results{echo}: {len(rows)} rows{suffix}"


def evaluate_queries(
    facts: list[dict[str, str]],
    inferred: dict[str, set[tuple[str, ...]]],
    policy_query_predicates: set[str],
    path_nodes: set[str] | None = None,
    spelling: dict[str, str] | None = None,
) -> list[str]:
    """Render one result line per query in facts/query.dl.

    *path_nodes* is entity_set — the values that may be path endpoints. ``None``
    means "do not distinguish", the pre-#329 behaviour kept for three-argument
    callers; ``build_report_text`` always passes it.

    *spelling* is ``kb_query_spellings``, shared with ``validate_query`` so a run
    derives it once; ``None`` derives it here, which is what the callers passing
    four arguments or fewer get. Unlike ``validate_query``'s parameter, ``None``
    does NOT mean "do not resolve" — this function has *facts* and must always
    evaluate against the spellings the file holds, or the report would answer a
    query the Errors section simultaneously accepts.

    Every branch EVALUATES the resolved line and ECHOES the written one. The
    engine and the raw comparisons here join on bytes, so a query naming a value
    under its other canonically equivalent spelling has to be moved onto the
    spelling accepted.dl holds before anything is matched; but the report is read
    beside facts/query.dl, so what it prints back must be what the author typed.
    The two differ only on a KB that stores both forms of some value.
    """
    results: list[str] = []
    # Read policy/attribute-relations.md at most once for the whole run, not once
    # per path query — dependency_path falls back to reading it when the argument
    # is None. classify_query hoists it the same way. Stays lazy so a KB with no
    # path query does not touch the file at all.
    attribute_rels: set[str] | None = None
    if spelling is None:
        spelling = kb_query_spellings(facts)
    for line in query_lines():
        resolved = resolve_query_spellings(line, spelling)
        predicate = line.split("(", 1)[0]
        if predicate in policy_query_predicates:
            result_line = policy_result_line(predicate, line, inferred, resolved)
            if result_line is not None:
                results.append(result_line)
        elif predicate == "path":
            # Same verdict validate_query put in the Errors section, so a line
            # reported as malformed is never also answered here (#328). It runs
            # before the endpoints are read, so a refused line never reaches
            # path_endpoints at all.
            if query_error("path", line) is not None:
                continue
            constants = path_endpoints(line)
            # Display from the WRITTEN line, evaluation from the RESOLVED one:
            # the head echoes the endpoints the author typed, while membership,
            # reachability and the trace all have to use the spelling the engine
            # joined on.
            evaluated = _paired_constants(constants, path_endpoints(resolved))
            if len(constants) >= 2:
                # An endpoint that is a literal (object of a declared attribute
                # relation) is not a path node at all. Name the reason instead of
                # reporting "(not found)", which claims the facts were searched
                # and do not connect the two — and which `ask` does not claim,
                # because classify_query rejects the query as entity_not_accepted
                # (#329).
                not_nodes = (
                    [written for written, tested in evaluated[:2] if tested not in path_nodes]
                    if path_nodes is not None else []
                )
                head = (
                    f"path {display_value(constants[0])} -> {display_value(constants[1])}"
                )
                if not_nodes:
                    reason = ", ".join(display_value(node) for node in not_nodes)
                    results.append(
                        f"{head}: (not evaluated — not an accepted entity: {reason})"
                    )
                    continue
                start, target = evaluated[0][1], evaluated[1][1]
                is_reachable = (start, target) in inferred["path"]
                if is_reachable:
                    if attribute_rels is None:
                        attribute_rels = attribute_relations()
                    trace = dependency_path(facts, start, target, attribute_rels)
                else:
                    trace = []
                # one_line here because the trace nodes come from
                # dependency_path over the facts and are rendered directly. `head`
                # needs no wrapping at THIS site because it is built from
                # display_value, which applies one_line itself — not because a
                # query-derived value is safe. It is not: path_endpoints goes
                # through arg_value, which is json.loads, so an escape in
                # facts/query.dl decodes to a real newline inside a single
                # physical line. That is what put the escaping into display_value
                # in the first place.
                value = " -> ".join(one_line(node) for node in trace) if trace else "(not found)"
                results.append(f"{head}: {value}")
        elif predicate == "relation":
            if query_error("relation", line) is not None:
                continue
            # relation_results compares RAW, so it must see the resolved line;
            # the bindings below are named from the written one, which resolution
            # cannot change (a variable is never a value position).
            rows = relation_results(resolved, facts)
            args = query_args(line)
            result_values: list[str] = []
            for subject, relation, object_ in rows:
                bindings = []
                for arg, value in zip(args, [subject, relation, object_], strict=True):
                    if not is_quoted_string(arg):
                        bindings.append(f"{arg}={one_line(value)}")
                result_values.append(
                    ", ".join(bindings) if bindings
                    else f"{one_line(subject)}, {one_line(relation)}, {one_line(object_)}"
                )
            suffix = "; " + "; ".join(result_values) if result_values else ""
            results.append(f"relation results: {len(rows)} rows{suffix}")
        elif predicate == "count":
            # count(subject, relation)? -> number of DISTINCT objects for that
            # (subject, relation) over engine facts (0 is a verified answer).
            # NOT the same number as ask_router.evaluate's count branch: #227 gave
            # the router's count surface-variant expansion (a quoted canonical
            # relation also counts objects stored under its declared variants) and
            # this branch never got it, so on a KB with relation aliases the two
            # disagree — the gate passes the query, the router answers 2 and the
            # report answers 0. Which side is right is #227's question, not this
            # guard's; what is fixed here is that both refuse the SAME malformed
            # lines.
            if query_error("count", line) is not None:
                continue
            subj_q, rel_q = query_args(resolved)
            subj, rel = arg_value(subj_q), arg_value(rel_q)
            objects = {
                f["object"]
                for f in facts
                if (not is_quoted_string(subj_q) or f["subject"] == subj)
                and (not is_quoted_string(rel_q) or f["relation"] == rel)
            }
            # Echo the query, as policy_result_line does (17cf7d3): a malformed
            # line now drops out of this list, so 3 query lines can leave 2 result
            # lines and positional correspondence silently breaks. Unlike the
            # policy echo this is unconditional — count has no "Policy evaluation:"
            # extent line for a variable-only query to be read against, and a
            # dropped line misaligns every shape equally.
            #
            # The echo is the WRITTEN line, never the resolved one. It is what
            # tests/golden/logic_report.txt pins, and more to the point the reader
            # matches this line against facts/query.dl by eye: printing back a
            # spelling they did not type would be a difference they cannot see and
            # cannot search for.
            #
            # The ``one_line`` here FIRES ON REAL INPUT, and is pinned. It was
            # nearly documented as defence over a structurally unreachable path,
            # on this argument: the echo is `line`, this branch is reached only
            # after ``query_error("count", line)`` returned None, and that requires
            # every argument to be a strict ``[A-Z_][A-Za-z0-9_]*`` variable or a
            # string ``json.loads`` accepts — and JSON forbids raw control
            # characters below 0x20 inside a string literal.
            #
            # The argument is sound and covers too little. ``_FORBIDDEN_IN_LINE``
            # is wider than C0: it also holds DEL (0x7f) and the whole C1 block
            # (0x80-0x9f), and JSON permits every one of those inside a string.
            # Measured — ``count("a\x7fb", "r")?`` with the byte raw in the file is
            # ONE physical line, ``query_error`` returns None, and it arrives here
            # with the byte intact; same for 0x80 and 0x9f.
            #
            # What IS structurally excluded is exactly the C0 range the old
            # argument named, and doubly: ``json.loads`` rejects those raw, and an
            # author who escapes them instead writes a six-character
            # ``\uXXXX`` escape, ordinary characters in the physical line that
            # ``one_line`` has nothing to do with. 0x85 is excluded too, by
            # ``query_lines``' ``splitlines``. So the reachable set is DEL and C1
            # minus 0x85, and that is what the pin uses.
            results.append(
                f"count results (query: {one_line(line)}): {len(objects)} (distinct objects)"
            )
        elif predicate == "review_required":
            constants = quoted_constants(line)
            question = one_line(constants[0]) if constants else "(missing question)"
            results.append(f"review_required: {question}")
    return results


# The line that tells a reader — and hooks/gate_check.sh — that this report
# describes a run in which THE ENGINE NEVER RAN. It is the whole discriminator
# between "engine ran and found nothing" and "there is nothing to find out from
# here", so it is matched as a whole line, byte for byte, on both sides. Change
# it in one place only together with the other two: `_records_engine_failure` in
# hooks/gate_check.sh and the same comparison in factlog/cli.py.
#
# The marker is NEGATIVE — a successful report carries no status line at all —
# for one reason: the success report's text is a published contract
# (tests/golden/logic_report.txt, examples/sample-kb/facts/logic_report.txt, and
# every report already sitting in a user's KB), and a positive `status: ok`
# marker would make every one of those read as unrecognised.
#
# A negative marker only works while "carries the marker" and "written by the
# failure path" are the same set, and that is NOT free. This report interpolates
# KB-derived text, and KB text reaches it through two decoders, each of which can
# deliver a newline the file itself does not show:
#
#   - facts/candidates.csv — a quoted CSV field may span physical lines, so a
#     hand-edited status of "odd\nstatus: engine-did-not-run" arrives as a value
#     containing a real newline;
#   - facts/query.dl — `arg_value` is `json.loads`, so `"a\nstatus: ..."` written
#     as ONE physical line, with the escape as two ordinary characters, decodes
#     to the same thing. Splitting the file into lines cannot see it. This is the
#     worst carrier of the three: query.dl is an engine input this very gate
#     guards, and `/factlog query` writes it;
#   - facts/accepted.dl — `common.parse_relation_fact` is `json.loads` too, so a
#     compiled fact carries escapes exactly like a query does, and reaches here
#     through the ordinary pipeline: compile_facts renders a candidates value into
#     dl_string form and this report decodes it back. Covered by display_value and
#     the result renderers, which one_line every value they print.
#
# Whichever the carrier, the run SUCCEEDS, the report carries real counts, and
# both readers then call it an engine failure with `reason: (not recorded)` — the
# deadlock #338 removes, rebuilt out of KB content, and since the status fix
# repeated by two consumers rather than one.
#
# There is a THIRD carrier, and it is not a decoder at all: the RAW query line.
# Every error and echo in this module appends the offending line of
# facts/query.dl verbatim, and `query_lines` splits that file with
# `str.splitlines()`, which consumes line breaks and NOTHING else. NUL, ESC, DEL
# and the C1 range ride through untouched inside one physical line. That cannot
# forge the marker — a forged line needs a break, and a break is the one thing
# splitlines already ate — but it is the same defect with a different victim:
# `zz<ESC>[1A<ESC>[2Kerrors: 0 (all clear)(X)?` in query.dl produces a report
# whose own header says `errors: 1` and whose Errors section, on the terminal
# that SKILL.md Step 3 tells an operator to read it on, renders as
# `errors: 0 (all clear)`. Measured before the wrapping went in. So the raw line
# is wrapped at every site too, and the invariant below is about the report's
# reader as much as its parser.
#
# `one_line` is what keeps the two sets equal, and it has to be at every site
# where a KB-derived value — decoded or raw — reaches a report line; the
# csv-side sites alone left query.dl wide open. The claim to be careful with is
# the one this comment used to make: not "no interpolated value can open a line"
# as a property of the design, but "these call sites are wrapped", which is a
# property of a list and stays true only while the list is complete. tests/unit
# pins one carrier per decoder and one for the raw line; a new interpolation
# site needs its own.
#
# Two sites deliberately interpolate WITHOUT `one_line`, and neither is an
# omission: `policy_result_line` and the relation renderer print a query's
# argument text (`f"{arg}=..."`) only after `_query_shape_error` has passed it,
# and an arg that is not a quoted string must match `[A-Z_][A-Za-z0-9_]*` to get
# there. Wrapping them would be a no-op on every value that can reach them.
#
# The cost of the negative marker is also that a report truncated inside its
# first three lines would lose it and read as a success; _write_report closes
# that by replacing the file atomically.
ENGINE_FAILED_STATUS_LINE = "status: engine-did-not-run"

# Characters that may not appear inside a report line. Two families, and the
# second was learned the hard way:
#
#   - LINE BREAKS: every character str.splitlines() breaks on, which is wider
#     than "\n" because it is what a READER may split on (cli.py's splitlines()
#     did). A value carrying one opens a line for one reader and not for another.
#     U+2028 is routine in text pasted from PDFs — see factlog/common.py's
#     line-break handling.
#   - OTHER C0/C1 CONTROLS, above all NUL. A report line is judged by tools that
#     are not all Python, and a NUL made BSD sed abort mid-pipeline; because the
#     gate read that pipeline's status as "no marker", ONE NUL byte in the report
#     turned a DENY into an ALLOW. That is this whole change in reverse — not
#     forging the marker but erasing the reader's ability to see it — so no value
#     reaching a report line may carry one. ESC belongs here too: this report is
#     printed to a terminal.
#
# Pinned per family in tests/unit (newline, NUL, U+2028). Narrowing the set to
# "\n" alone used to leave every suite green.
_LINE_BREAKS = "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029"
# TAB is deliberately NOT here, and it is the only C0 character excluded. It
# cannot break a line, cannot end a reader, and cannot drive a terminal — it is
# the one control character people actually type into a KB — so escaping it would
# change the report's text for benign input while defending nothing. Measured:
# with TAB in the set, a tab-bearing status value renders differently from
# origin/main; without it, the report stays byte-identical.
_FORBIDDEN_IN_LINE = frozenset(
    _LINE_BREAKS
    + "".join(chr(c) for c in range(0x00, 0x20) if c != 0x09)
    + "\x7f"
    + "".join(chr(c) for c in range(0x80, 0xA0))
)


def one_line(value: object) -> str:
    """*value* as report text that cannot become more than one line, nor blind a
    reader of it.

    Values carrying none of _FORBIDDEN_IN_LINE — every ordinary status, entity
    and literal — are returned UNCHANGED, so the report stays byte-identical to
    what it has always written (tests/golden/logic_report.txt pins that). Only a
    value that would break or corrupt the line is escaped, via ``repr``, which
    keeps it readable and visible rather than silently dropping the offending
    part: a hand-edited status of ``odd\\nstatus: engine-did-not-run`` reports as
    ``'odd\\nstatus: engine-did-not-run'``, on one line, still naming what is
    wrong with the row.
    """
    text = str(value)
    return repr(text) if any(ch in _FORBIDDEN_IN_LINE for ch in text) else text


def _report_mode(out: Path) -> int:
    """The permission bits facts/logic_report.txt must end up with.

    `mkstemp` creates at 0600 and `os.replace` carries the SOURCE's mode onto
    the destination, so without this the atomic write silently narrows the
    report — measured 0644 on origin/main, 0600 here, on the SUCCESS path, every
    run. That is not a cosmetic difference in this hook's company: an unreadable
    report used to fall through to the mtime branch and be allowed, and now
    hooks/gate_check.sh HARD DENIES it. Wherever the check and the gate run as
    different UIDs — a devcontainer running the check as root, a CI stage that
    switches user, a group-shared KB — 0600 turns into a blanket refusal of every
    Write/Edit to facts/accepted.dl and facts/query.dl.

    Two cases, and they are not the same rule:

    - the report EXISTS: keep its mode. An operator who chmod'd it 0664 for a
      shared KB did so deliberately, and `write_text` (what origin/main used)
      never disturbed it. This is the case that matters, because it is the one a
      cross-UID setup arrives in.
    - it does NOT exist: 0666 masked by the process umask, which is exactly what
      `open(..., "w")` would have produced. Read by setting and restoring the
      umask, the only way to observe it; this module is single-threaded by the
      time it writes, and nothing else creates a file inside that window.

    Applied to the temp file BEFORE the replace, not to *out* after it, so no
    reader can ever see the report at a mode it should not have.
    """
    try:
        return stat.S_IMODE(out.stat().st_mode)
    except FileNotFoundError:
        umask = os.umask(0o022)
        os.umask(umask)
        return 0o666 & ~umask


def _write_report(text: str) -> None:
    """Put *text* in facts/logic_report.txt, atomically and with LF endings.

    temp + os.replace, not write_text: the gate reads this file to decide
    whether editing engine inputs is allowed, and a write interrupted after the
    header but before ENGINE_FAILED_STATUS_LINE would leave a file that is
    neither report yet passes the gate as one.

    ``newline="\\n"`` is load-bearing, not tidiness. Text mode translates "\\n" to
    os.linesep, so on Windows every line of this report would be written CRLF —
    and the gate's whole-line match then stops matching, which fails OPEN: it
    hands out edit rights on engine inputs at the moment the engine is broken.
    Measured here by writing a CRLF report by hand (gate exit 0 where LF gives
    2); that Windows *produces* one is read off the io.TextIOWrapper contract,
    not measured, since neither this lane nor the review could run Windows. The
    gate strips CR as well, so a report from either side reads correctly.

    NOT common._atomic_write_text: same temp+replace shape, but that one writes
    in default text mode. Reusing it would reintroduce exactly the translation
    this pins against; the duplication is the difference.

    KNOWN LIMITATION, and it is a behaviour change from origin/main: `mkstemp`
    needs write permission on facts/ITSELF, where `write_text` needed it only on
    the file. A KB with a read-only facts/ holding a writable logic_report.txt
    used to complete a SUCCESSFUL check; here that raises PermissionError out of
    main (measured: main exit 0, this exit 1 with a traceback). Not papered over
    with an in-place fallback: the fallback would be a truncate-then-write in the
    exact configuration nobody watches, which is the hazard the temp+replace
    exists to remove. Make facts/ writable, or run the check somewhere it is.
    """
    out = FACTS_DIR / "logic_report.txt"
    # A per-run temp name, not a fixed "<name>.tmp": two checks running against
    # one KB would otherwise write the SAME temp file and each replace it, so a
    # reader could be handed a file mixing both runs. Atomicity against
    # truncation held with the fixed name; atomicity against a concurrent run did
    # not. The finally-unlink keeps a failed replace from leaving the temp behind.
    fd, tmp_name = tempfile.mkstemp(dir=str(out.parent), prefix=out.name + ".", suffix=".tmp")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        tmp.write_text(text, encoding="utf-8", newline="\n")
        os.chmod(tmp, _report_mode(out))
        os.replace(tmp, out)
    finally:
        if tmp.exists():
            tmp.unlink()


def engine_failure_report(exc: BaseException) -> str:
    """The report for a run that could not run the engine (#338).

    Until this existed, an engine that could not start — pyrewire missing or too
    old, facts/accepted.dl absent, a policy program the engine refuses — left
    facts/logic_report.txt *untouched*. Two things followed, and the second is
    the reason this function reports failure rather than staying silent:

    - the previous run's report survived on disk and read as this run's result.
      A reader (and `/factlog check`'s output) had nothing to tell the two
      apart, so a report describing facts that are no longer compiled looked
      current;
    - the gate's freshness predicate had nothing to compare against, so it
      denied every edit to an engine input and pointed at `/factlog check`,
      which is the command that had just failed.

    What this report may and may not say:

    - It states the CAUSE and nothing about the KB. Every count a successful
      report carries (engine facts, policy findings, errors, warnings) is
      OMITTED rather than written as 0 — `engine facts: 0` is a claim that the
      engine ran over an empty KB, which is exactly the sentence this report
      must not produce.
    - It does NOT satisfy the gate. A report of a run that did not happen is not
      evidence that editing facts/accepted.dl or facts/query.dl is safe, so
      ENGINE_FAILED_STATUS_LINE keeps the deny in place — the gate's message
      just changes from "run /factlog check" to the actual cause. Recovery is
      the Bash route in docs/guide/determinism.md, which the hook's Write|Edit
      matcher deliberately leaves open.
    - It does not change the exit code. main re-raises, so `/factlog check`
      still fails and still prints the error on stderr; this file is a side
      effect of failing, never a sign of success.

    *reason* is collapsed to a single line and then escaped like any other
    KB-derived value: the format is line-oriented and judged whole-line on the
    other side, and an engine ParseError's message spans several lines.

    THE REASON LINE IS A CARRIER, and the one the carrier list above did not
    name — it is very nearly the only place KB text enters a FAILURE report. A
    line of facts/accepted.dl the engine refuses goes into the exception message
    verbatim, so whatever that line holds lands here. ``" ".join(split())``
    collapses Python whitespace only, so a NUL rode it into the report intact and
    blinded the gate's reader; ``one_line`` is what closes that, and it is the
    same grade of escaping every other interpolation gets.
    """
    reason = one_line(" ".join(str(exc).split())) or exc.__class__.__name__
    accepted = FACTS_DIR / "accepted.dl"
    query = FACTS_DIR / "query.dl"
    return "\n".join(
        [
            "Logic Check Report",
            "==================",
            ENGINE_FAILED_STATUS_LINE,
            "engine: wirelog / pyrewire",
            "input: facts/accepted.dl",
            f"reason: {reason}",
            f"reason type: {type(exc).__name__}",
            f"facts/accepted.dl: {'present' if accepted.is_file() else 'MISSING'}",
            f"facts/query.dl: {'present' if query.is_file() else 'absent'}",
            "",
            "The engine did not run, so this report says NOTHING about the KB.",
            "The counts a successful report carries — engine facts, policy",
            "findings, errors, warnings — are missing above rather than 0,",
            "because 0 would mean the engine ran and found nothing.",
            "",
            "Any earlier report has been replaced, so nothing here can be read as",
            "an older run's result. While this status line is present, EDITS to",
            "facts/accepted.dl and facts/query.dl stay denied — creating either",
            "for the first time in this KB is still allowed, as it is when no",
            "report exists at all.",
            "",
            "Recovery: fix the cause above and re-run the logic check. When the",
            "check cannot run at all, see the Bash recovery in",
            "docs/guide/determinism.md — the gate's Write|Edit matcher leaves it",
            "open on purpose.",
            "",
        ]
    )


def main() -> None:
    ensure_dirs()
    try:
        text = build_report_text()
    except Exception as exc:
        # Report the failure, then let it propagate untouched: run_cli prints a
        # FactlogError on stderr and exits 1, anything else keeps its traceback.
        # ensure_dirs is deliberately OUTSIDE this — "not a factlog KB root" has
        # no facts/ to write into, and is not a statement about the engine.
        #
        # THE GUARANTEE IS "an exception out of build_report_text", NOT "any run
        # in which the engine could not start", and it misses in BOTH directions.
        #
        # It over-labels. `run_wirelog` is only one statement inside
        # build_report_text, so an exception from what follows it — the policy
        # program, the query renderers, the report assembly — is written up as
        # `status: engine-did-not-run` too, and that report's sentence "The engine
        # did not run, so this report says NOTHING about the KB" is then false:
        # the engine DID run. Deliberate, in this direction. The label decides
        # whether the gate keeps denying, and "an engine result we could not
        # finish rendering" is not a result the gate may act on either, so the
        # conservative label is the safe one; `reason type:` carries the real
        # classification for whoever reads the file. Narrowing the try to
        # `run_wirelog` alone would trade a wrong word for a stale report, which
        # is the whole defect #338 exists to remove.
        #
        # It also under-labels. A run that dies before reaching
        # this try still leaves the previous report standing, and one such path
        # is a real engine failure rather than a hypothetical: `common` guards
        # its `import pyrewire` with `except ImportError` ONLY, so an engine
        # whose import fails some other way — a broken native extension raising
        # OSError from dlopen — propagates out of the `from common import ...`
        # above, at module import time, where no handler here can run. Measured:
        # no report is written, and the traceback is the only output. Widening
        # that guard belongs next to it in factlog/common.py, not here; catching
        # it at this module's import would mean rebuilding FACTS_DIR without the
        # module that defines it.
        #
        # BEST EFFORT, because reporting the failure must not REPLACE it. With
        # facts/ read-only, writing raised PermissionError from inside this
        # handler and that became the program's output: the operator got a
        # traceback about the report instead of the one clean line naming the
        # actual cause, which origin/main gave them. A report we could not write
        # is not worth the diagnosis we already have.
        try:
            _write_report(engine_failure_report(exc))
        except Exception as write_exc:  # noqa: BLE001 - never mask *exc*
            print(
                f"warning: could not write facts/logic_report.txt: {write_exc}",
                file=sys.stderr,
            )
        raise
    _write_report(text)
    print(text)


def build_report_text() -> str:
    """The report for a run that reached the engine, byte-identical to what this
    module has always written (tests/golden/logic_report.txt pins it).

    It is a separate function only so main can tell a report it could build from
    one it could not: everything here presumes ``run_wirelog`` returned, and the
    one caller turns any exception raised below into ``engine_failure_report``.

    ANY exception, including one raised AFTER ``run_wirelog`` has returned. Such
    a run is reported as `status: engine-did-not-run` even though the engine did
    run — the label is deliberately conservative rather than accurate, because it
    is what keeps the gate denying, and a report this module could not finish
    building is not a result the gate may act on. See main's handler.
    """
    facts = load_accepted_facts()
    candidates = load_facts()
    inferred = run_wirelog()
    policy_program = load_logic_policy()
    policy_query_predicates = policy_predicates(policy_program)
    # value_set (entities + literal values) so a query naming a literal object of
    # an attribute relation is not falsely warned as a non-engine entity.
    entities = value_set(facts)
    # entity_set is the narrower set a path endpoint must belong to — the same
    # test classify_query applies for `ask`, so the report and the router give
    # the same answer to the same path query (#329).
    path_nodes = entity_set(facts)
    # Derived ONCE for the whole run and handed to both consumers below, so the
    # vocabulary warnings and the answers are decided by the same map.
    spelling = kb_query_spellings(facts)
    relations = allowed_relations(facts)
    errors: list[str] = []
    warnings: list[str] = []
    policy_findings: list[str] = []

    for row in candidates:
        if not row["subject"] or not row["relation"] or not row["object"]:
            errors.append(f"incomplete fact row: {row}")
        if row["status"] not in KNOWN_STATUSES:
            # one_line: this is the report's most direct path from a hand-edited
            # CSV cell to a report line, and a quoted CSV field may contain a
            # newline. Unescaped, a status of "odd\nstatus: engine-did-not-run"
            # put ENGINE_FAILED_STATUS_LINE into a SUCCESSFUL report and both
            # readers then called a completed run an engine failure.
            warnings.append(f"unknown status treated as non-engine input: {one_line(row['status'])}")

    for predicate in sorted(policy_query_predicates):
        for target, reason in sorted(inferred[predicate]):
            policy_findings.append(f"{one_line(predicate)}: {one_line(target)} ({one_line(reason)})")

    for line in query_lines():
        query_errors, query_warnings = validate_query(
            line, entities, policy_query_predicates, path_nodes, spelling
        )
        errors.extend(query_errors)
        warnings.extend(
            [item for item in query_warnings if not names_a_relation(item, relations)]
        )

    report = [
        "Logic Check Report",
        "==================",
        "engine: wirelog / pyrewire",
        "input: facts/accepted.dl",
        f"policy: {LOGIC_POLICY_DL.relative_to(LOGIC_POLICY_DL.parents[1])}",
        f"engine facts: {len(facts)}",
        f"review facts outside engine input: {len(review_facts(candidates))}",
        f"policy findings: {len(policy_findings)}",
        f"errors: {len(errors)}",
        f"warnings: {len(warnings)}",
        "",
    ]
    if policy_findings:
        report.extend(["Policy Findings:", *[f"- {item}" for item in policy_findings], ""])
    if errors:
        report.extend(["Errors:", *[f"- {item}" for item in errors], ""])
    if warnings:
        report.extend(["Warnings:", *[f"- {item}" for item in warnings], ""])
    report.append("Policy evaluation:")
    policy_items = [
        # one_line for the same reason the Policy Findings line above wraps the
        # same value: a predicate name is generated from policy/logic-policy.dl,
        # and leaving the one site unwrapped is the asymmetry that lets the next
        # reader conclude the list is advisory.
        f"{one_line(predicate)}: {len(inferred[predicate])} rows"
        for predicate in sorted(policy_query_predicates)
    ]
    report.extend([f"- {item}" for item in policy_items] or ["- no generated policy predicates"])
    report.append("")
    report.append("Query evaluation:")
    query_results = evaluate_queries(facts, inferred, policy_query_predicates, path_nodes, spelling)
    if query_results:
        report.extend([f"- {item}" for item in query_results])
    elif not (FACTS_DIR / "query.dl").is_file() and not query_lines():
        report.append("- no facts/query.dl found")
    else:
        # A file whose every line was refused is not a missing file. SKILL.md
        # reads "no facts/query.dl found" as "the /factlog query step was
        # skipped", so printing it here would send the reader to re-run a step
        # they already ran instead of to the Errors section above.
        #
        # "Missing" needs BOTH signals absent. Keying on `query_lines()` alone
        # called a comment-only query.dl missing, which is the same false claim;
        # keying on the file alone would call a line set supplied by a caller
        # missing. A query.dl holding only variable-form path queries
        # (`path(X, Y)?`) lands here too — those are answerable, they just render
        # no result line, because a path result names its two endpoints.
        report.append("- no answerable queries in facts/query.dl (see Errors)")

    return "\n".join(report) + "\n"


if __name__ == "__main__":
    from common import run_cli

    # main() takes no argv; parse here only so `--wiki` is a documented option
    # with --help, and a mistyped flag is rejected instead of silently ignored.
    _parser = argparse.ArgumentParser(description="Run deterministic logic checks over facts and query drafts.")
    _parser.add_argument("--wiki", default=os.environ["FACTLOG_ROOT"], help="KB root")
    _parser.parse_args()
    raise SystemExit(run_cli(main))
