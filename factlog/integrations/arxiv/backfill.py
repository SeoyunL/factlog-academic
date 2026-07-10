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

``check_versions._diff`` computes ``changed = recorded is not None and current !=
recorded``. So:

* an **absent** ``version`` silently excludes the record from version checking: with
  ``recorded is None``, ``changed`` is false regardless of what arXiv serves, so the
  paper's drift is **never reported at all**. Measured, same paper with arXiv serving v7:
  a ledger recording ``version: 3`` prints ``Version changed: 1``; a ledger recording no
  version prints ``Version changed: 0``. A backfill that wrote a version-less record would
  drop a previously-healthy paper out of the one signal ``arxiv-check-versions`` exists to
  produce, so it refuses (an OpenAlex-authored ``.md`` echoes ``arxiv_id`` but never emits
  ``arxiv_version``: it reads ``None`` here). See #121;
* a merely **wrong** ``version`` (``0``, ``-1``) is ``changed``, gets rewritten by the
  first ``--auto-update`` (which writes ``current_version`` unconditionally, not gated on
  ``_diff``), and is meanwhile still reported — recordable, so *not* refused;
* ``withdrawn_by`` is ``None`` for every paper that is **not** withdrawn, the overwhelming
  majority; requiring it would refuse almost the whole library. Its ``None`` is a
  legitimate recordable value, not an unreadable identity, so it is **not** required.

``tests/unit/test_common_backfill_unhealable.py`` pins this boundary.
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
    )
