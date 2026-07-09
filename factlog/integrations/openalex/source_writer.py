#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Write a parsed OpenAlex work into a factlog ``sources/<slug>.md`` original.

Consumes :class:`~factlog.integrations.openalex.work_parser.ParsedWork` and
produces one markdown source file: YAML provenance front matter plus a readable
body (abstract + the original OpenAlex/DOI pointers).

Everything about *how* the file is placed — atomic write, slug, uniqueness
suffix, batch index, duplicate detection — comes from
:class:`~factlog.integrations.common.source_writer.BaseSourceWriter`, shared
with the Zotero importer. This module supplies the ``openalex_id`` identity, the
front matter, and the body.

**Retraction is recorded as OpenAlex's opinion, not as fact.** The front matter
emits ``openalex_is_retracted``, never a bare ``retracted:``. OpenAlex flags the
Lancet Commission dementia report (``W3046275966``, PMID 32738937) as retracted
while PubMed records no retraction for it (#51), and §7.2 gives PubMed authority
over retraction status. A reader — human or the extraction step — must be able
to see which database made the claim.
"""
from __future__ import annotations

from factlog.integrations.common._textio import yaml_list as _yaml_list
from factlog.integrations.common._textio import yaml_scalar as _yaml_str
from factlog.integrations.common.provenance import SourceRecord
from factlog.integrations.common.source_writer import BaseSourceWriter, WriteResult
from factlog.integrations.openalex.work_parser import ParsedWork

__all__ = ["OpenAlexSourceWriter", "WriteResult"]

def _scored_concepts(concepts) -> str:
    """Every concept with its score and level, as a one-line YAML flow sequence.

    Kept in full because the ``tags`` filter is lossy by design (#54 §4.3: nothing
    measured is discarded). Written on a single line for the same reason
    :meth:`OpenAlexSourceWriter._primary_topic_lines` is flat — an indented
    ``score:`` would be misread as a top-level front-matter key by
    :func:`factlog.bibtex.parse_front_matter`.
    """
    parts = []
    for concept in concepts:
        fields = [f"name: {_yaml_str(concept.name)}"]
        if concept.score is not None:
            fields.append(f"score: {concept.score:.4f}")
        if concept.level is not None:
            fields.append(f"level: {concept.level}")
        parts.append("{" + ", ".join(fields) + "}")
    return "[" + ", ".join(parts) + "]"


_RETRACTION_WARNING = (
    "> ⚠ **OpenAlex flags this work as retracted.** This flag is unverified: "
    "OpenAlex has false positives. Confirm against PubMed before relying on it."
)


class OpenAlexSourceWriter(BaseSourceWriter):
    """Render parsed OpenAlex works into ``sources/`` markdown originals."""

    identity_key = "openalex_id"
    source_name = "openalex"
    # OpenAlex merges (§7.3): a paper already in the KB via another database — an
    # arXiv deposit of the same preprint, a Zotero item of the same published work,
    # matched on a shared arXiv id, DOI or PMID — has OpenAlex's view folded into
    # that original's provenance sidecar instead of writing a second file. This is
    # the user-visible change of #73: such a cross-source match now reports
    # ``merged`` where it used to report ``skipped``. The shared
    # :meth:`_merge`/:meth:`_record` machinery in the base reads this one flag for
    # BOTH jobs on purpose — see :meth:`BaseSourceWriter._record` for why splitting
    # them would make the ledger order-dependent.
    merges_cross_source = True
    # EMPTY, and that is the whole design (#73). Any identifying field would raise
    # a per-id ``error`` on drift, and OpenAlex has NO refresh command — nothing
    # calls ``update_source`` for it — so that error would be permanently
    # unclearable, a paper you can never re-import. With the empty tuple every
    # field (``doi``, ``journal``, ``type``, retraction) drifts silently,
    # first-import-wins, and :meth:`_divergence` is never reached. Revisit only if
    # an ``openalex-refresh`` ever ships (#83); with a clearing path, a retraction
    # appearing between imports *should* then stop and ask a human.
    _IDENTIFYING_FIELDS: tuple[str, ...] = ()

    def identity_of(self, parsed: ParsedWork) -> str:
        return parsed.openalex_id

    def slug_fields(self, parsed: ParsedWork) -> tuple[str, str, str]:
        first_author = parsed.authors[0] if parsed.authors else ""
        year = str(parsed.year) if parsed.year else ""
        return (first_author, year, parsed.title or "")

    def cross_ids(self, parsed: ParsedWork) -> dict[str, str]:
        """DOI, PMID and a normalized arXiv id let §7.1 spot this paper arriving
        from another database. The arXiv id is the only exact join key for a
        preprint, which rarely carries a DOI."""
        return {
            key: value
            for key, value in (("doi", parsed.doi), ("pmid", parsed.pmid), ("arxiv_id", parsed.arxiv_id))
            if value
        }

    def render(self, parsed: ParsedWork, imported_at: str = "") -> str:
        return self._front_matter(parsed, imported_at) + self._body(parsed)

    def _front_matter(self, parsed: ParsedWork, imported_at: str) -> str:
        lines = ["---", f"openalex_id: {_yaml_str(parsed.openalex_id)}"]
        if parsed.work_type:
            lines.append(f"type: {_yaml_str(parsed.work_type)}")
        lines.append(f"title: {_yaml_str(parsed.title or '')}")
        if parsed.authors:
            lines.append(f"authors: {_yaml_list(parsed.authors)}")
        if parsed.year:
            lines.append(f"year: {parsed.year}")
        if parsed.journal:
            lines.append(f"journal: {_yaml_str(parsed.journal)}")
        if parsed.doi:
            lines.append(f"doi: {_yaml_str(parsed.doi)}")
        if parsed.pmid:
            lines.append(f"pmid: {_yaml_str(parsed.pmid)}")
        # Bare `arxiv_id`, not `openalex_arxiv_id`: the cross-source index scans
        # the literal key across every source file, and the arXiv writer emits it
        # as its identity key. The canonical, version-free base is written so it
        # matches an arXiv record of the same paper (§7.1).
        if parsed.arxiv_id:
            lines.append(f"arxiv_id: {_yaml_str(parsed.arxiv_id)}")
        if parsed.tags:
            lines.append(f"tags: {_yaml_list(parsed.tags)}")
        # Descriptors only, with no major/minor distinction: OpenAlex's
        # `is_major_topic` is unreliable before ~2022 (#53). Users who need
        # majorness run the PubMed commands.
        if parsed.mesh_terms:
            lines.append(f"mesh_terms: {_yaml_list(parsed.mesh_terms)}")
        if parsed.cited_by_count is not None:
            lines.append(f"cited_by_count: {parsed.cited_by_count}")
        if parsed.abstract_complete is not None:
            lines.append(f"abstract_complete: {'true' if parsed.abstract_complete else 'false'}")
        lines.extend(self._primary_topic_lines(parsed))
        lines.append("imported_from: openalex")
        if imported_at:
            lines.append(f"imported_at: {_yaml_str(imported_at)}")
        # Source-scoped on purpose; see the module docstring.
        if parsed.openalex_is_retracted:
            lines.append("openalex_is_retracted: true")
        if parsed.concepts:
            lines.append(f"openalex_concepts: {_scored_concepts(parsed.concepts)}")
        lines.append("---")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _primary_topic_lines(parsed: ParsedWork) -> list[str]:
        """`primary_topic` and its hierarchy, one flat key per level.

        Flat rather than a nested mapping: :func:`factlog.bibtex.parse_front_matter`
        strips each line before matching ``key: value``, so an indented ``score:``
        or ``field:`` would be read as a *top-level* key and pollute the export.
        """
        topic = parsed.primary_topic
        if topic is None:
            return []
        lines = [f"primary_topic: {_yaml_str(topic.display_name)}"]
        # The score is not decoration: primary_topic is just the top entry of
        # topics[], and that can score 0.06 (#54). A reader must see how confident
        # the classification is.
        if topic.score is not None:
            lines.append(f"primary_topic_score: {topic.score:.4f}")
        for key, value in (("subfield", topic.subfield), ("field", topic.field),
                           ("domain", topic.domain)):
            if value:
                lines.append(f"primary_topic_{key}: {_yaml_str(value)}")
        return lines

    def _body(self, parsed: ParsedWork) -> str:
        parts = [f"\n# {parsed.title or 'Untitled'}\n"]
        if parsed.openalex_is_retracted:
            parts.append(f"\n{_RETRACTION_WARNING}\n")
        if self.include_abstract:
            parts.append("\n## Abstract\n")
            parts.append(f"\n{parsed.abstract or '_No abstract available._'}\n")
        parts.append("\n## Original source\n")
        parts.append(f"\n- OpenAlex: `{parsed.openalex_url}`")
        if parsed.doi:
            parts.append(f"\n- DOI: {parsed.doi}")
        if parsed.pmid:
            parts.append(f"\n- PMID: {parsed.pmid}")
        return "".join(parts) + "\n"

    # -- §7.3 the OpenAlex contribution to a source's provenance ledger -----
    def _provenance_record(self, parsed: ParsedWork, imported_at: str) -> SourceRecord:
        """OpenAlex's record for a paper's provenance ledger.

        Fields are exactly ``doi``, ``work_type``, ``journal`` and ``is_retracted``.
        (The work type is keyed ``work_type``, not the bare ``type`` #73 named:
        ``SourceRecord.to_dict`` flattens ``fields`` to the record's top level, where
        ``type`` is the RESERVED key holding ``"openalex"`` — the ``(type, id)``
        idempotency key. A field literally named ``type`` would overwrite it and
        make the record's type read back as the work type, destroying the ledger's
        keying. ``work_type`` carries the same value with no collision.)

        ``doi`` is INCLUDED, inverting arXiv's choice to exclude it. arXiv excluded
        ``doi`` because its merge target was an OpenAlex-primary record that already
        held it authoritatively. OpenAlex's merge target is an arXiv-primary record,
        which is *not* authoritative on a published DOI — a preprint rarely has one
        — so the DOI is new information and the evidence that a peer-reviewed
        version exists (#65 H1). ``type`` and ``journal`` are OpenAlex-authoritative
        venue signals in the same role.

        Deliberately excluded: ``openalex_url`` (derivable from the id, like arXiv's
        ``abs_url``); ``cited_by_count`` (a volatile live metric, not provenance —
        freezing it would store a number that reads as current but is stale);
        ``pmid`` and ``arxiv_id`` (join keys — ``arxiv_id`` *is* the arXiv record's
        identity, so a second copy invites the "which is right" disagreement); and
        all content/classification fields, which belong in the ``.md``, not an
        audit ledger.

        ``is_retracted`` is emitted only as ``True`` (else ``None``, which
        :meth:`SourceRecord.to_dict` drops). A literal ``False`` would survive
        ``to_dict`` and change the bytes, breaking the byte-determinism the ledger
        depends on — so retraction absent from the JSON *means* not retracted.
        """
        return SourceRecord(
            type="openalex",
            id=parsed.openalex_id,
            imported_at=imported_at,
            fields={
                "doi": parsed.doi,
                "work_type": parsed.work_type,
                "journal": parsed.journal,
                "is_retracted": True if parsed.openalex_is_retracted else None,
            },
        )
