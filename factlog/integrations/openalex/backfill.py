# SPDX-License-Identifier: Apache-2.0
"""The OpenAlex :class:`BackfillSchema` — what is OpenAlex-specific about a backfill (#115).

``common/backfill.py`` (#113) holds the read-modify-write once and never names a field;
each integration supplies a small schema binding its own collection seam and its
front-matter-derived field values. This module is the OpenAlex one, kept beside the
package that owns those field meanings rather than inline in ``cli.py``, so the arXiv and
OpenAlex schemas cannot drift apart (the #64 shape) and the command entry point stays a
thin caller.

Membership is decided by the integration's *own* ``refresh.collect_ledger_entries`` and
exported ``refresh.provenance_of`` — never a second copy of either the flat walk or the
predicate, which is how #64, #98 and the empty-tuple divergence fixed in #111 all happened.

## Unlike arXiv, OpenAlex loses nothing

Every field the OpenAlex ledger record holds has a front-matter equivalent that the writer
already emits, and ``refresh.collect_ledger_entries``'s front-matter fallback already reads
exactly those keys:

===============  =========================  ================================
ledger field     front-matter key           source
===============  =========================  ================================
``doi``          ``doi``                    ``source_writer._front_matter``
``work_type``    ``type``                   ``source_writer._front_matter``
``journal``      ``journal``                ``source_writer._front_matter``
``is_retracted`` ``openalex_is_retracted``  ``source_writer._front_matter``
===============  =========================  ================================

So a backfilled OpenAlex ledger **is**, field for field, what the import would have
written, ``imported_at`` included (front matter carries it). This is an asymmetry with
arXiv, and it is a fact about the two *writers*, not an accident of this command: the
arXiv record holds ``submitted``, ``last_updated`` and ``comment``, none of which its
front matter emits, and ``submitted`` is not in ``AUTO_UPDATE_FIELDS``, so it is
unrecoverable forever (#114). OpenAlex's writer emits every ledger field it records, so
nothing is lost and nothing has to be invented.

## No ``required`` guard, and the reason is structural

``OpenAlexSourceWriter._IDENTIFYING_FIELDS`` is **empty** (``source_writer.py:86``) — the
whole design of #73. With no identifying field, a re-import never reaches ``_divergence``,
so the false-conflict hazard #113's ``required`` exists to prevent (a backfill writing an
identifying field it could not read, which a later import then reads as ``{name: None}``
and calls a permanent divergence) **cannot arise here at all**. ``required`` is therefore
``()``, not by omission but because there is no identity to fail to read. The other half
of the same hazard is closed too: ``openalex_id`` is emitted only by OpenAlex's own writer,
so a front-matter-only entry is always this integration's own deposit, never another
integration echoing the id (which is exactly what forces arXiv's ``required=("version",)``).

## Vocabulary: OpenAlex's opinion, in OpenAlex's words

``is_retracted`` is *OpenAlex's claim about the world*, not the KB's: OpenAlex flags the
Lancet Commission dementia report (``W3046275966``) as retracted while PubMed records no
retraction (#51). The front-matter key is ``openalex_is_retracted`` — source-scoped, never
a bare ``retracted:`` — and the ledger field is ``is_retracted`` inside a record whose
``type`` is ``"openalex"``. Renaming either, in code or in a message, would strip the
attribution that makes the claim auditable; arXiv's word for its own, different process is
arXiv's, and never appears in anything this module emits.

## The retraction flag is promoted verbatim, never coerced

``source_writer._provenance_record`` emits ``is_retracted`` **only** as ``True`` (otherwise
``None``, which ``SourceRecord.to_dict`` drops): a literal ``False`` would survive
serialization and change the bytes, so *absence from the JSON means not retracted*. The
schema reproduces that shape, so a backfilled record is byte-identical to the import's.

Front matter, however, is a looser medium than the ledger, and what its reader yields is a
string. :func:`refresh.parse_retraction_flag` is the **one** place that says which strings
are booleans; this module calls it and adds no rule of its own. A value it will not read as
a boolean (``1``, ``yes``, ``on``) is passed into the record **verbatim**, and
``common/backfill.py`` refuses that paper per-id: the ledger's value space for
``("openalex", "is_retracted")`` is fixed at the read boundary (#109), and
``signal_field_error`` names the offending field in OpenAlex's own words before any sidecar
is opened. Neither alternative is available to a backfill:

* dropping the value would write ``is_retracted`` absent — an assertion that OpenAlex does
  **not** flag this paper, silencing a retraction the ``.md`` was trying to state;
* coercing it to ``True`` would assert a retraction no source made.

Both are writes the KB has no warrant for. The paper is refused, its neighbours are
backfilled normally, and the refusal happens before ``_backfill_source``'s ``dry_run``
early return, so a preview can never disagree with the run about it.

**The refusal is the only place such a value is ever heard from.** ``openalex-refresh``
narrows the same parse to a ``bool`` (a compare needs one), so a hand-typed ``yes`` is
compared as *not retracted*: with OpenAlex also not flagging the work, neither
``newly_retracted`` nor ``un_retracted`` fires and **nothing surfaces, ever**. Measured. So
this refusal does not merely defer a signal that would keep nagging — it is the signal. Say
so, rather than let the refusal read as harmless.

That shared parse is load-bearing, and is why this module does not keep its own copy of the
words ``true``/``false``. Were they written down twice, widening one copy to YAML 1.1's
``yes``/``on`` — a plausible bug report — would make ``openalex-refresh`` report a paper
retracted that this command still refuses a ledger. The retraction could then never be
acknowledged, and the repeat #105 exists to end would run forever, for exactly the papers a
user had just fixed. That is #64, #98 and #111 in their exact shape.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factlog.integrations.common.backfill import BackfillSchema
from factlog.integrations.common.front_matter import read_scalar
from factlog.integrations.openalex import refresh
from factlog.integrations.openalex.refresh import RETRACTION_KEY

__all__ = ["RETRACTION_KEY", "backfill_schema"]


@dataclass(frozen=True)
class _BackfillEntry:
    """One OpenAlex work as the backfill sees it.

    Everything but ``is_retracted`` is ``refresh.LedgerEntry``'s already-parsed value.
    ``is_retracted`` is what the *record* should carry: ``True``, ``None`` (absent — the
    import's own shape for "not retracted"), or, for a front-matter value this tool will
    not interpret, that value **verbatim**, so ``common/backfill.py`` refuses the paper
    rather than this module guessing (see the module docstring).
    """

    openalex_id: str
    recorded_doi: str | None
    recorded_work_type: str | None
    recorded_journal: str | None
    is_retracted: Any
    sources: tuple[str, ...]


def _retraction_value(md_path: Path) -> Any:
    """What ``is_retracted`` should be, read from a front-matter-only paper's one ``.md``.

    Delegates every question about *which strings are booleans* to
    :func:`refresh.parse_retraction_flag`. ``False`` (an absent or empty key) becomes ``None``
    so the field is omitted, exactly as the import omits it; ``True`` is recorded; a string —
    a value that function will not read as a boolean — is returned unchanged, for the shared
    writer to refuse in OpenAlex's vocabulary.

    **One** ``.md``, not a list. ``collect_ledger_entries``' front-matter branch fills a slot
    only for an ``openalex_id`` no ledger and no earlier source covered, so a front-matter-only
    entry has exactly one source; ``test_a_front_matter_only_entry_has_exactly_one_source``
    pins that. Folding several sources' flags here would have to pick a winner, and any pick is
    a claim no single ``.md`` made. When #112 or #117 gives one entry several sources, that test
    goes red — which is the point — and the per-source value must be plumbed through the shared
    writer rather than guessed at here.
    """
    parsed = refresh.parse_retraction_flag(read_scalar(md_path, RETRACTION_KEY))
    if parsed is True:
        return True
    if parsed is False:
        return None
    return parsed


def _collect_entries(kb_root: Path | str):
    """``refresh.collect_ledger_entries``, with each entry's retraction flag left unnarrowed.

    The walk, the ledger reads and the front-matter fallback are *not* re-derived: this calls
    OpenAlex's own collector, then re-reads one key — ``openalex_is_retracted`` — off each
    front-matter-only paper's ``.md``, because ``LedgerEntry.recorded_is_retracted`` is already
    narrowed to a ``bool`` for the comparison a refresh needs, and a backfill must be able to
    tell a hand-typed ``yes`` from an honest absence. The *parse* is still the shared one
    (:func:`refresh.parse_retraction_flag`); only the narrowing is skipped. Every ``.md`` is
    opened read-only (P4). A ledger-backed paper is skipped by ``backfill()`` before any field
    is read, so its flag is taken as ``collect_ledger_entries`` parsed it.
    """
    root = Path(kb_root)
    entries, errors = refresh.collect_ledger_entries(root)
    wrapped = [
        _BackfillEntry(
            openalex_id=entry.openalex_id,
            recorded_doi=entry.recorded_doi,
            recorded_work_type=entry.recorded_work_type,
            recorded_journal=entry.recorded_journal,
            is_retracted=(
                _retraction_value(root / entry.sources[0])
                if refresh.provenance_of(entry.sources) == "front-matter"
                else (True if entry.recorded_is_retracted else None)
            ),
            sources=entry.sources,
        )
        for entry in entries
    ]
    return wrapped, errors


def backfill_schema() -> BackfillSchema:
    """The OpenAlex backfill schema, bound to the real OpenAlex collection seam.

    ``fields`` reads each ledger field off the entry ``refresh.collect_ledger_entries``
    already parsed, so a ledger field can only ever be populated from a value the front
    matter actually held, and ``is_retracted`` reproduces ``_provenance_record``'s
    ``True``-or-absent shape — or carries an uninterpretable value verbatim for the shared
    writer to refuse.

    ``required`` is ``()``: OpenAlex declares no identifying fields, so the false conflict a
    ``required`` guard prevents cannot arise (see the module docstring).
    """
    return BackfillSchema(
        type="openalex",
        id_key="openalex_id",
        collect_entries=_collect_entries,
        provenance_of=refresh.provenance_of,
        id_of=lambda entry: entry.openalex_id,
        fields={
            "doi": lambda entry: entry.recorded_doi,
            "work_type": lambda entry: entry.recorded_work_type,
            "journal": lambda entry: entry.recorded_journal,
            "is_retracted": lambda entry: entry.is_retracted,
        },
        required=(),
    )
