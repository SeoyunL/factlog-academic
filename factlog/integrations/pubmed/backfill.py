# SPDX-License-Identifier: Apache-2.0
"""The PubMed :class:`BackfillSchema` — what is PubMed-specific about a backfill (#172, #105).

``common/backfill.py`` (#113) holds the read-modify-write once and never names a field;
each integration supplies a small schema binding its own collection seam and its
front-matter-derived field values. This module is the PubMed one, kept beside the package
that owns those field meanings rather than inline in ``cli.py``, so the arXiv, OpenAlex and
PubMed schemas cannot drift apart (the #64 shape) and the command entry point stays a thin
caller.

## The bootstrap this command exists for (#105/#110/#171)

A PubMed paper imported before provenance ledgers existed has front matter and no sidecar,
so a re-import short-circuits on the front-matter identity match before the sidecar writer,
its ledger is never created, and ``pubmed-acknowledge-retraction`` refuses it with
``ACK_NO_LEDGER`` and points *here*. ``pubmed-refresh`` still reads such a paper — a
retraction that appeared since import is real news — but a signal that fires on every run
can never be acknowledged until a ledger exists. This command is the only thing that builds
it, from what the ``.md`` already asserts (``add_source`` into a fresh sidecar, no new
claim), so acknowledge can then silence the repeat.

## No network, ever

Querying PubMed would make this a *refresh*, whose write is ``update_source``. A backfill
records what the import recorded, not what PubMed says *now*, so it never absorbs a
retraction that appeared after the import (the exact signal the import/refresh/acknowledge
split exists to protect). This module imports no PubMed client; a test asserts that the
``efetch`` transport is never called.

## The #117 trap: coverage may not turn on a filename

A sidecar is written **per ``.md``**, from that ``.md``'s own front matter. Two ``.md`` may
carry one PMID — a PubMed deposit and another database's import echoing ``pmid:`` as a
cross-reference. ``refresh.collect_ledger_entries`` deduplicates them to one entry keyed by
``pmid`` (right for a *check*, which asks about a paper once), and its front-matter branch
keeps only the first source in walk order — so reusing it directly would backfill only
whichever file sorts first and leave the other with no sidecar, a coverage that depends on a
filename (#117, P3). So this module keeps the shared ledger scan for the corrupt-ledger
poison and the ledger-covered set, but does its **own** per-``.md`` walk of
``provenance_sources`` (the shared #112 walker, so a nested paper is covered too, #112),
building one view per ``.md``. Each view carries only its own front matter and names only
its own ``.md``; both files are then written, from their own values, into their own
sidecars, and the result is identical whichever way the walk enumerates them. The views and
the results are both sorted, so nothing observable depends on enumeration order.

## No ``required`` guard, for OpenAlex's structural reason

``PubMedSourceWriter._IDENTIFYING_FIELDS`` is **empty** (#166), exactly like OpenAlex's. With
no identifying field, a re-import never reaches ``_divergence``, so the false-conflict hazard
#113's ``required`` exists to prevent — a backfill writing an identifying field it could not
read, which a later import then reads as ``{name: None}`` and calls a permanent divergence —
**cannot arise here at all**. ``required`` is therefore ``()``, not by omission but because
there is no identity to fail to read.

The other half of that hazard is closed too, but differently from OpenAlex: ``pmid`` is a
§7.1 cross-source join key, so a paper imported from another database *does* echo it. But the
PMID is the record's *id*, not a field — this module never creates a view for a ``.md`` with
no readable ``pmid`` (``collect_entries`` drops it), so a backfill can never assert a PMID the
front matter did not carry (#73/#84: a ledger claiming an id it cannot read is worse than no
ledger). A ``.md`` that echoes a PMID this integration's own writer never wrote still names a
real paper by a real id, and the record built for it — ``doi``, ``journal`` and PubMed's
retraction signal, each read from that ``.md``'s own front matter — is what the KB already
believes; with no identifying field, a later real PubMed import merges into it first-wins and
never errors.

## What a backfilled ledger carries, and the one thing it does not

The fields reproduce ``PubMedSourceWriter._provenance_record`` for the values the front
matter carries, in the exact shape the import wrote them (each key confirmed against the
writer's ``_front_matter``):

===========================  =============================  ===========================
ledger field                 front-matter key               source
===========================  =============================  ===========================
``doi``                      ``doi``                        ``source_writer._front_matter``
``journal``                  ``journal``                    ``source_writer._front_matter``
``retracted``                ``pubmed_retracted``           ``source_writer._front_matter``
``retraction_notice_pmid``   ``pubmed_retraction_notice_pmid``  ``source_writer._front_matter``
===========================  =============================  ===========================

``retracted`` reproduces ``_provenance_record``'s ``True``-or-absent shape: the writer emits
``pubmed_retracted`` only when PubMed flags a retraction, so **absence means not retracted**,
and a literal ``False`` is never written. ``retraction_notice_pmid`` is emitted only *with* a
retraction (the writer gates it inside ``if parsed.retracted``), so this module drops a stray
notice on a non-retracted paper rather than record a notice for a retraction the ``.md`` did
not state.

The **one** ledger field a backfill cannot reproduce is ``retraction_verified_at`` — the
import clock (the time PubMed was consulted), which is not a front-matter key. A backfill
consulted PubMed at no time, so it has no honest value to write and does not invent one; this
is the same asymmetry OpenAlex documents for a field its writer does not emit, and arXiv for
``submitted``. It is not an identifying field, so its absence causes no divergence: a later
refresh compares the *retraction status*, not the timestamp, and a re-import merges
first-wins without error.

## The retraction flag is promoted verbatim, never coerced (#98/#109)

Front matter is a looser medium than the ledger, and ``read_scalars`` is a line reader, so
what it yields is a string. :func:`refresh.parse_retraction_flag` is the **one** place that
says which strings are booleans; this module calls it and adds no rule of its own, exactly as
OpenAlex's backfill delegates to *its* ``parse_retraction_flag`` (were the boolean words
written down twice, widening one copy to YAML 1.1's ``yes``/``on`` would make ``pubmed-refresh``
report a paper retracted that this command still refuses a ledger, and the repeat #105 exists
to end would run forever — #64/#98/#111). ``True`` is recorded; ``False`` (an absent or empty
key) becomes ``None`` so the field is omitted, as the import omits it; a value that function
will not read as a boolean (``1``, ``yes``, ``on``) is passed into the record **verbatim**,
and ``common/backfill.py``'s ``signal_field_error`` guard refuses that paper per id — the
ledger's value space for ``("pubmed", "retracted")`` is a boolean (fixed at the read boundary,
#109). Neither dropping the value (which would assert PubMed does **not** flag the paper,
silencing a retraction the ``.md`` was stating) nor coercing it to ``True`` (which would assert
a retraction no source made) is a write the KB has any warrant for.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factlog.integrations.common.backfill import BackfillSchema
from factlog.integrations.common.front_matter import read_scalars
from factlog.integrations.common.provenance import provenance_sources
from factlog.integrations.pubmed import refresh
from factlog.integrations.pubmed.refresh import RETRACTION_KEY, RETRACTION_NOTICE_KEY

__all__ = ["RETRACTION_KEY", "RETRACTION_NOTICE_KEY", "backfill_schema"]

#: Bibliographic front-matter keys the PubMed writer emits and the ledger records verbatim.
_DOI_KEY = "doi"
_JOURNAL_KEY = "journal"


@dataclass(frozen=True)
class _BackfillView:
    """One PubMed ``.md`` as the backfill writes it: its own front matter, its own sidecar.

    ``retracted`` is what the *record* should carry: ``True``, ``None`` (absent — the
    import's own shape for "not retracted"), or, for a front-matter value this tool will not
    interpret, that value **verbatim**, so ``common/backfill.py`` refuses the paper rather
    than this module guessing (see the module docstring). ``sources`` names exactly the one
    ``.md`` this view speaks for (#117).
    """

    pmid: str
    recorded_doi: str | None
    recorded_journal: str | None
    retracted: Any
    recorded_notice_pmid: str | None
    sources: tuple[str, ...]


@dataclass(frozen=True)
class _BackfillEntry:
    """One PubMed paper the KB holds only in front matter, with one view per ``.md``.

    ``sources`` are every ``.md`` that carries this PMID (all under ``sources/``, so
    ``refresh.provenance_of`` classifies the entry ``front-matter``); ``per_source`` is the
    per-``.md`` views the shared writer builds a sidecar from — one each, never a fold across
    files (#117).
    """

    pmid: str
    sources: tuple[str, ...]
    per_source: tuple[_BackfillView, ...]


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _retracted_value(raw: str) -> Any:
    """What ``retracted`` should be, from a ``.md``'s ``pubmed_retracted`` scalar.

    Delegates every question about *which strings are booleans* to
    :func:`refresh.parse_retraction_flag` (the shared parser; this module keeps no copy of
    the boolean words). ``True`` is recorded; ``False`` (absent or empty) becomes ``None`` so
    the field is omitted, exactly as the import omits it; a string the parser will not read as
    a boolean is returned unchanged, for the shared writer to refuse in PubMed's vocabulary.
    """
    parsed = refresh.parse_retraction_flag(raw)
    if parsed is True:
        return True
    if parsed is False:
        return None
    return parsed


def _view(pmid: str, path: Path, root: Path, scalars: dict[str, str]) -> _BackfillView:
    retracted = _retracted_value(scalars.get(RETRACTION_KEY, ""))
    # The writer emits `pubmed_retraction_notice_pmid` only alongside a retraction, so a
    # notice without a `retracted: True` flag is not a shape the import produces. Gate it on
    # the flag rather than record a notice for a retraction the `.md` did not state; a
    # verbatim (uninterpretable) flag is not True, so its notice is dropped and the paper is
    # refused for the flag anyway.
    notice = (scalars.get(RETRACTION_NOTICE_KEY) or None) if retracted is True else None
    return _BackfillView(
        pmid=pmid,
        recorded_doi=scalars.get(_DOI_KEY) or None,
        recorded_journal=scalars.get(_JOURNAL_KEY) or None,
        retracted=retracted,
        recorded_notice_pmid=notice,
        sources=(_relative(path, root),),
    )


def _collect_entries(kb_root: Path | str):
    """Front-matter-only PubMed papers, one view per ``.md``, plus corrupt-ledger errors.

    The ledger scan and the corrupt-ledger poison are **not** re-derived: this calls PubMed's
    own :func:`refresh.collect_ledger_entries` for the errors and for the set of PMIDs a
    PubMed ledger already covers (via :func:`refresh.provenance_of`), so membership is decided
    by the integration's own predicate, never a second copy (#111). It then does its own
    per-``.md`` walk of ``provenance_sources`` — because ``refresh``'s front-matter branch
    collapses several ``.md`` sharing a PMID to the first in walk order, which a backfill (one
    sidecar per ``.md``) must not do (#117). Every ``.md`` is opened read-only (P4). Views and
    entries are sorted so nothing observable depends on enumeration order.
    """
    root = Path(kb_root)
    base_entries, errors = refresh.collect_ledger_entries(root)
    ledger_pmids = {
        entry.pmid
        for entry in base_entries
        if refresh.provenance_of(entry.sources) == "ledger"
    }

    views: dict[str, list[_BackfillView]] = {}
    for path in provenance_sources(root):
        scalars = read_scalars(
            path,
            ("pmid", _DOI_KEY, _JOURNAL_KEY, RETRACTION_KEY, RETRACTION_NOTICE_KEY),
        )
        pmid = scalars.get("pmid", "")
        # A `.md` with no readable PMID cannot name a PubMed paper, so a backfill will not
        # assert one for it (#73/#84). A paper a PubMed ledger already covers is left to that
        # ledger — the record belongs there, not to a `.md`.
        if not pmid or pmid in ledger_pmids:
            continue
        views.setdefault(pmid, []).append(_view(pmid, path, root, scalars))

    entries = [
        _BackfillEntry(
            pmid=pmid,
            sources=tuple(sorted(view.sources[0] for view in group)),
            per_source=tuple(sorted(group, key=lambda view: view.sources[0])),
        )
        for pmid, group in views.items()
    ]
    return entries, errors


def backfill_schema() -> BackfillSchema:
    """The PubMed backfill schema, bound to the real PubMed collection seam.

    ``fields`` reads each ledger field off the per-``.md`` view built from that ``.md``'s own
    front matter, so a ledger field can only ever be populated from a value the ``.md``
    actually held, and ``retracted`` reproduces ``_provenance_record``'s ``True``-or-absent
    shape — or carries an uninterpretable value verbatim for the shared writer to refuse.

    ``required`` is ``()``: PubMed declares no identifying fields, so the false conflict a
    ``required`` guard prevents cannot arise (see the module docstring). ``sources_of`` hands
    the shared writer the per-``.md`` views, so the ledger is built from, and written beside,
    each file that carries the PMID (#117).
    """
    return BackfillSchema(
        type="pubmed",
        id_key="pmid",
        collect_entries=_collect_entries,
        provenance_of=refresh.provenance_of,
        id_of=lambda entry: entry.pmid,
        fields={
            "doi": lambda entry: entry.recorded_doi,
            "journal": lambda entry: entry.recorded_journal,
            "retracted": lambda entry: entry.retracted,
            "retraction_notice_pmid": lambda entry: entry.recorded_notice_pmid,
        },
        required=(),
        sources_of=lambda entry: entry.per_source,
    )
