#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Write a parsed PubMed record into a factlog ``sources/<slug>.md`` original (#166).

Consumes :class:`~factlog.integrations.pubmed.work_parser.ParsedPubMedWork` and
produces one markdown source file: flat YAML provenance front matter plus a
readable body (abstract + the PubMed/DOI pointers).

Everything about *how* the file is placed — atomic write, slug, uniqueness
suffix, batch index, cross-source duplicate detection, the provenance sidecar —
comes from
:class:`~factlog.integrations.common.source_writer.BaseSourceWriter`, shared with
the arXiv, OpenAlex and Zotero importers. This module supplies only the ``pmid``
identity, the front matter, the body, and the PubMed contribution to a paper's
provenance ledger. **None of that machinery is rebuilt here.**

**Identity is the PMID, and PMID is also a §7.1 cross-source join key.** Because
``identity_key = "pmid"`` is itself one of :data:`CROSS_SOURCE_IDS`, the base
contributes it as a cross-id automatically — so a paper already imported from
OpenAlex (whose front matter echoes ``pmid:``) is *merged* into that original's
provenance sidecar rather than written a second time, and no ``cross_ids``
re-declaration of the PMID is needed. ``doi`` is added as the other join key so a
PubMed record folds onto an OpenAlex/Zotero record that shares only a DOI.

**PubMed merges (§7.3), like arXiv and OpenAlex.** When a PubMed record matches a
paper already in the KB (shared DOI or PMID), :attr:`merges_cross_source` folds a
``pubmed`` record into the existing original's provenance sidecar. The original
``.md`` is never touched (P4). A record already imported from OpenAlex may carry a
flat ``mesh_terms`` list; PubMed's richer major/minor reading does **not**
overwrite it — the two coexist under the source-scoped ``pubmed_mesh_*`` namespace
:func:`~factlog.integrations.pubmed.mesh.mesh_provenance_fields` emits.

**Retraction is recorded as PubMed's signal, not as fact.** The front matter emits
``pubmed_retracted``, never a bare ``retracted:`` — exactly as
``openalex_is_retracted`` and ``arxiv_withdrawn`` are source-scoped. §7.2 gives
PubMed authority over retraction status, but an import still may not write the
merged top-level ``retracted:`` claim: it is a **signal a human must act on**
(via a downstream ``pubmed-acknowledge-retraction``), never an absorbed truth.
The import does record ``retraction_verified_at`` — the time it read the live
PubMed record — so a later refresh can tell a fresh check from a stale one.
"""
from __future__ import annotations

from factlog.integrations.common._textio import yaml_list as _yaml_list
from factlog.integrations.common._textio import yaml_scalar as _yaml_str
from factlog.integrations.common.provenance import SourceRecord
from factlog.integrations.common.source_writer import BaseSourceWriter, WriteResult
from factlog.integrations.pubmed.mesh import (
    major_topic_descriptors,
    mesh_provenance_fields,
    minor_topic_descriptors,
)
from factlog.integrations.pubmed.work_parser import ParsedPubMedWork

__all__ = ["PubMedSourceWriter", "WriteResult", "retraction_warning"]


def retraction_warning(notice_pmid: str | None) -> str:
    """The body blockquote for a retracted paper. Mirrors OpenAlex/arXiv warnings.

    Names PubMed as the source of the signal, states it is unverified and flags
    the paper for human review, and points at the retraction *notice* PMID when
    one is linkable so a reader can confirm the retraction themselves.
    """
    where = (
        f" See the retraction notice (PMID {notice_pmid})."
        if notice_pmid
        else ""
    )
    return (
        "> ⚠ **PubMed reports this paper as retracted.** This is PubMed's signal, "
        "not an absorbed fact: it is unverified and flags the paper for human "
        f"review before any claim from it is trusted.{where}"
    )


class PubMedSourceWriter(BaseSourceWriter):
    """Render parsed PubMed records into ``sources/`` markdown originals."""

    identity_key = "pmid"
    source_name = "pubmed"
    # PubMed merges (§7.3): a paper already in the KB via another database — an
    # OpenAlex/Zotero record of the same work matched on a shared DOI, or any
    # record echoing the PMID — has PubMed's view folded into that original's
    # provenance sidecar instead of writing a second file. arXiv and OpenAlex opt
    # in too; Zotero does not. The shared _merge/_record machinery reads this one
    # flag for BOTH jobs (see BaseSourceWriter._record for why splitting them would
    # make the ledger order-dependent).
    merges_cross_source = True
    # A paper imported from PubMed that resembles an existing source but shares no
    # DOI/PMID surfaces a title+author+year *candidate* for a human (#75). It never
    # merges and never changes the ``imported`` outcome. arXiv and OpenAlex opt in
    # too; Zotero does not.
    surfaces_candidates = True
    # EMPTY, like OpenAlex and for the same reason (#73): any identifying field
    # would raise a per-id ``error`` on drift, and this issue ships no
    # ``pubmed-refresh`` — nothing calls ``update_source`` for a pubmed record — so
    # that error would be permanently unclearable, a paper you could never
    # re-import. With the empty tuple every field drifts silently, first-import
    # wins, and :meth:`_divergence` is never reached. Revisit only when #170's
    # refresh ships a clearing path: a retraction appearing between imports *should*
    # then stop and ask a human.
    _IDENTIFYING_FIELDS: tuple[str, ...] = ()

    def identity_of(self, parsed: ParsedPubMedWork) -> str:
        return parsed.pmid

    def slug_fields(self, parsed: ParsedPubMedWork) -> tuple[str, str, str]:
        # Authors are "Given Family" order (parser), so authors[0] is the first
        # author's display name — the same shape the slug builder expects.
        first_author = parsed.authors[0] if parsed.authors else ""
        year = str(parsed.year) if parsed.year else ""
        return (first_author, year, parsed.title or "")

    def cross_ids(self, parsed: ParsedPubMedWork) -> dict[str, str]:
        """DOI lets §7.1 spot this paper arriving from another database.

        Bare ``doi`` on purpose — the cross-source index scans the literal key
        across every source file regardless of which integration wrote it. The
        PMID is *also* a join key, but it is contributed automatically because it
        is this writer's identity key and one of ``CROSS_SOURCE_IDS`` (see
        :meth:`BaseSourceWriter._cross_id_values`), so it need not be re-declared.
        """
        return {"doi": parsed.doi} if parsed.doi else {}

    def render(self, parsed: ParsedPubMedWork, imported_at: str = "") -> str:
        return self._front_matter(parsed, imported_at) + self._body(parsed)

    def _front_matter(self, parsed: ParsedPubMedWork, imported_at: str) -> str:
        lines = [
            "---",
            f"pmid: {_yaml_str(parsed.pmid)}",
            f"title: {_yaml_str(parsed.title or '')}",
        ]
        if parsed.authors:
            lines.append(f"authors: {_yaml_list(list(parsed.authors))}")
        if parsed.year:
            lines.append(f"year: {parsed.year}")
        if parsed.journal:
            lines.append(f"journal: {_yaml_str(parsed.journal)}")
        # Bare `doi`, not `pubmed_doi`: the cross-source index scans the literal key.
        if parsed.doi:
            lines.append(f"doi: {_yaml_str(parsed.doi)}")
        # Source-scoped MeSH: major/minor kept apart (the level OpenAlex drops,
        # #53/#165). A `pubmed_mesh_*` namespace so it coexists with an OpenAlex
        # record's flat `mesh_terms` rather than overwriting it (spec §7).
        major = major_topic_descriptors(parsed.mesh_headings)
        minor = minor_topic_descriptors(parsed.mesh_headings)
        if major:
            lines.append(f"pubmed_mesh_major: {_yaml_list(list(major))}")
        if minor:
            lines.append(f"pubmed_mesh_minor: {_yaml_list(list(minor))}")
        lines.append("imported_from: pubmed")
        if imported_at:
            lines.append(f"imported_at: {_yaml_str(imported_at)}")
        # Source-scoped on purpose, at the end, mirroring `openalex_is_retracted`
        # and `arxiv_withdrawn`. Emitted only when PubMed flags a retraction; its
        # absence *means* not-retracted. Never the bare top-level `retracted:`.
        if parsed.retracted:
            lines.append("pubmed_retracted: true")
            if parsed.retraction_notice_pmid:
                lines.append(
                    f"pubmed_retraction_notice_pmid: {_yaml_str(parsed.retraction_notice_pmid)}"
                )
        lines.append("---")
        return "\n".join(lines) + "\n"

    def _body(self, parsed: ParsedPubMedWork) -> str:
        parts = [f"\n# {parsed.title or 'Untitled'}\n"]
        if parsed.retracted:
            parts.append(f"\n{retraction_warning(parsed.retraction_notice_pmid)}\n")
        if self.include_abstract:
            parts.append("\n## Abstract\n")
            parts.append(f"\n{parsed.abstract or '_No abstract available._'}\n")
        parts.append("\n## Original source\n")
        parts.append(f"\n- PubMed: `https://pubmed.ncbi.nlm.nih.gov/{parsed.pmid}/`")
        if parsed.doi:
            parts.append(f"\n- DOI: {parsed.doi}")
        return "".join(parts) + "\n"

    # -- §7.3 the PubMed contribution to a source's provenance ledger -------
    def _provenance_record(self, parsed: ParsedPubMedWork, imported_at: str) -> SourceRecord:
        """PubMed's record for a paper's provenance ledger (§7.3).

        Identity is the PMID — the join key and the ledger's idempotency key both
        use it. Fields carry what is PubMed-authoritative and additive beside an
        existing OpenAlex/arXiv record:

        * ``doi`` and ``journal`` — bibliographic provenance PubMed is
          authoritative on for a published article.
        * ``pubmed_mesh_major`` / ``pubmed_mesh_minor`` — the major/minor split
          preserved from :func:`mesh_provenance_fields`, under a namespace that
          **cannot collide** with OpenAlex's flat ``mesh_terms`` so both survive
          (spec §7). Absent on an unindexed record (``mesh_provenance_fields``
          returns ``{}``).
        * ``retracted`` / ``retraction_notice_pmid`` / ``retraction_verified_at``
          — a *source-scoped* signal, emitted only when PubMed flags a retraction.
          ``retracted`` is named under a **pubmed** record, distinct from
          OpenAlex's ``openalex_is_retracted``; the two coexist and never overwrite
          one another. ``retraction_verified_at`` is the import clock: it records
          *when* PubMed was consulted, so a later refresh can tell a fresh check
          from a stale one. **This never writes the merged top-level ``retracted:``
          claim** — that stays a human acknowledgement (§7.2).

        A ``None`` field is dropped by :meth:`SourceRecord.to_dict`, so optional
        values pass straight through. The mesh tuples serialize to JSON lists;
        idempotence is judged on :attr:`_IDENTIFYING_FIELDS` (empty), so a re-merge
        is a no-op regardless of the tuple/list round-trip.
        """
        fields: dict[str, object | None] = {
            "doi": parsed.doi,
            "journal": parsed.journal,
        }
        fields.update(mesh_provenance_fields(parsed.mesh_headings))
        if parsed.retracted:
            fields["retracted"] = True
            if parsed.retraction_notice_pmid:
                fields["retraction_notice_pmid"] = parsed.retraction_notice_pmid
            if imported_at:
                fields["retraction_verified_at"] = imported_at
        return SourceRecord(
            type="pubmed", id=parsed.pmid, imported_at=imported_at, fields=fields
        )
