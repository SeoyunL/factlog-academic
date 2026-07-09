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
from factlog.integrations.common.source_writer import BaseSourceWriter, WriteResult
from factlog.integrations.openalex.work_parser import ParsedWork

__all__ = ["OpenAlexSourceWriter", "WriteResult"]

_RETRACTION_WARNING = (
    "> ⚠ **OpenAlex flags this work as retracted.** This flag is unverified: "
    "OpenAlex has false positives. Confirm against PubMed before relying on it."
)


class OpenAlexSourceWriter(BaseSourceWriter):
    """Render parsed OpenAlex works into ``sources/`` markdown originals."""

    identity_key = "openalex_id"

    def identity_of(self, parsed: ParsedWork) -> str:
        return parsed.openalex_id

    def slug_fields(self, parsed: ParsedWork) -> tuple[str, str, str]:
        first_author = parsed.authors[0] if parsed.authors else ""
        year = str(parsed.year) if parsed.year else ""
        return (first_author, year, parsed.title or "")

    def cross_ids(self, parsed: ParsedWork) -> dict[str, str]:
        """DOI and PMID let §7.1 spot this paper arriving from another database."""
        return {key: value for key, value in (("doi", parsed.doi), ("pmid", parsed.pmid)) if value}

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
        if parsed.concepts:
            lines.append(f"concepts: {_yaml_list(parsed.concepts)}")
        if parsed.cited_by_count is not None:
            lines.append(f"cited_by_count: {parsed.cited_by_count}")
        lines.append("imported_from: openalex")
        if imported_at:
            lines.append(f"imported_at: {_yaml_str(imported_at)}")
        # Source-scoped on purpose; see the module docstring.
        if parsed.openalex_is_retracted:
            lines.append("openalex_is_retracted: true")
        lines.append("---")
        return "\n".join(lines) + "\n"

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
