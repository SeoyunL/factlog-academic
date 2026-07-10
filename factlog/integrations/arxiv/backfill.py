# SPDX-License-Identifier: Apache-2.0
"""The arXiv :class:`BackfillSchema` — what is arXiv-specific about a provenance backfill.

``common/backfill.py`` (#113) holds the read-modify-write once and never names a field;
each integration supplies a small schema binding its own collection seam and its
front-matter-derived field values. This module is the arXiv one, kept beside the arXiv
package that owns those field meanings rather than inline in ``cli.py``, so the arXiv and
OpenAlex schemas cannot drift apart (the #64 shape) and the command entry point stays a
thin caller.

Membership is decided by the integration's *own* ``collect_ledger_entries`` and exported
``provenance_of`` — never a second copy of either the flat walk or the predicate, which is
how #64, #98 and the empty-tuple divergence fixed in #111 all happened.

## Why ``required`` is exactly ``("version",)``

``version`` is an **identifying** field (``ArxivSourceWriter._IDENTIFYING_FIELDS``), and a
backfill never populates an identifying field it cannot read. So:

* an **absent** ``version`` cannot be written as absent. ``_record_fields`` omits a
  ``None``, and ``_identity_fields`` reads an omitted field back as ``{"version": None}``,
  so a later import carrying the real value sees ``None != 7``, calls it a divergence, and
  errors — a *false* conflict the backfill itself manufactured, on a paper that had none.
  Measured, same paper, arXiv serving v7::

      backfill refuses           -> later ArxivSourceWriter().write(v7) == "merged"
      backfill writes no version -> later ArxivSourceWriter().write(v7) == "error"

  That is the whole difference between writing and refusing, and it is why ``version`` is
  ``required`` (an OpenAlex-authored ``.md`` echoes ``arxiv_id`` but never emits
  ``arxiv_version``: it reads ``None`` here). Note what is *not* the argument: "an absent
  version excludes the paper from version checking" is true but does not discriminate —
  a refused paper is read from front matter and lands in
  ``check_versions.STATUS_NO_VERSION`` just the same (#121), and unlike the written record
  it has no ledger for ``--auto-update`` to fill. Exclusion argues against refusing, not
  for it;
* a merely **wrong** ``version`` (``0``, ``-1``) is ``changed``, gets rewritten by the
  first ``--auto-update`` (which writes ``current_version`` unconditionally, not gated on
  ``_diff``), and is meanwhile still reported — recordable, so *not* refused;
* ``withdrawn_by`` is ``None`` for every paper that is **not** withdrawn, the overwhelming
  majority; requiring it would refuse almost the whole library. Its ``None`` is a
  legitimate recordable value, not an unreadable identity, so it is **not** required.

``tests/unit/test_common_backfill_identity_fields.py`` pins this boundary, including the
merged/error contrast above.
"""
from __future__ import annotations

from factlog.integrations.arxiv import check_versions
from factlog.integrations.common.backfill import BackfillSchema


def backfill_schema() -> BackfillSchema:
    """The arXiv backfill schema, bound to the real arXiv collection seam.

    ``fields`` reads each ledger field off the entry the arXiv ``collect_ledger_entries``
    already parsed from front matter (``version`` ← ``recorded_version``, ``withdrawn_by``
    ← ``recorded_withdrawn_by``), so a ledger field can only ever be populated from a value
    the front matter actually held. ``required`` is exactly ``("version",)`` — see the
    module docstring for why a version-less record must be refused (#121).

    ``sources_of`` hands the shared writer the entry's ``per_source`` views — one per
    ``.md`` carrying the id — so the ledger is built from, and written beside, the file
    whose front matter actually holds the values (#117). Each view is a
    :class:`~factlog.integrations.arxiv.check_versions.LedgerEntry` too, so ``id_of``,
    ``fields`` and ``required`` above read it without knowing the difference.
    """
    return BackfillSchema(
        type="arxiv",
        collect_entries=check_versions.collect_ledger_entries,
        provenance_of=check_versions.provenance_of,
        id_of=lambda entry: entry.arxiv_id,
        fields={
            "version": lambda entry: entry.recorded_version,
            "withdrawn_by": lambda entry: entry.recorded_withdrawn_by,
        },
        required=("version",),
        sources_of=lambda entry: entry.per_source,
    )
