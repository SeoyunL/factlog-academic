#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Write a parsed arXiv work into a factlog ``sources/<slug>.md`` original.

Consumes :class:`~factlog.integrations.arxiv.work_parser.ParsedArxivWork` and
produces one markdown source file: flat YAML provenance front matter plus a
readable body (abstract + the versioned arXiv/DOI pointers).

Everything about *how* the file is placed — atomic write, slug, uniqueness
suffix, batch index, duplicate detection — comes from
:class:`~factlog.integrations.common.source_writer.BaseSourceWriter`, shared with
the Zotero and OpenAlex importers. This module supplies the ``arxiv_id``
identity, the front matter, and the body.

**Withdrawal is recorded as arXiv's signal, not as fact.** The front matter emits
``arxiv_withdrawn``/``arxiv_withdrawn_by``, never a bare ``withdrawn:`` — exactly
as ``openalex_is_retracted`` is source-scoped rather than a bare ``retracted:``
(#51). A reader (human or the extraction step) must see which database made the
claim and *which agent* withdrew it: arXiv administrators withdraw papers for
authorship disputes and inflammatory content, so a bare boolean would let the
body assert an author action that never happened (#57). And **withdrawal is not
retraction** — arXiv has no peer-reviewed retraction process — so this flag must
not seed retraction logic and the word "retracted" is never used for it.

**Identity is the BASE, version-free id.** :meth:`identity_of` returns
``arxiv_id``, never ``versioned_id``: keying on the versioned form would import a
fresh file on every version bump, breaking idempotent re-import (P3).

**``doi`` stays a bare key.** It must not be source-scoped as ``arxiv_doi``:
:meth:`BaseSourceWriter._index` scans the literal ``doi`` across every source
file regardless of which integration wrote it, and that is what makes §7.1
cross-source duplicate detection work. ``preprint: true`` is emitted
unconditionally — this record *is* an arXiv deposit, and remains a preprint even
when ``journal``/``doi`` show a published version exists (§5.2's ``preprint:
false`` fires only at merge time, which is a later step).
"""
from __future__ import annotations

from factlog.integrations.arxiv.work_parser import (
    WITHDRAWN_BY_ADMIN,
    WITHDRAWN_BY_AUTHOR,
    ParsedArxivWork,
)
from factlog.integrations.common._textio import yaml_list as _yaml_list
from factlog.integrations.common._textio import yaml_scalar as _yaml_str
from factlog.integrations.common.source_writer import BaseSourceWriter, WriteResult

__all__ = ["ArxivSourceWriter", "WriteResult", "withdrawal_agent", "withdrawal_warning"]

# The prose agent name for each withdrawer, used in the body warning and the
# CLI's stderr note. The word "retracted" never appears here (see the module
# docstring); "retraction" does, only to state that the two are not the same.
_WITHDRAWAL_AGENTS = {
    WITHDRAWN_BY_AUTHOR: "the author",
    WITHDRAWN_BY_ADMIN: "arXiv administrators",
}


def withdrawal_agent(withdrawn_by: str | None) -> str:
    """Prose name of who withdrew the paper (``the author``/``arXiv administrators``)."""
    return _WITHDRAWAL_AGENTS.get(withdrawn_by or "", "arXiv")


def withdrawal_warning(withdrawn_by: str | None) -> str:
    """The body blockquote for a withdrawn paper. Mirrors ``_RETRACTION_WARNING``.

    Names the agent and states that withdrawal is not retraction; it must never
    use the word "retracted".
    """
    agent = withdrawal_agent(withdrawn_by)
    return (
        f"> ⚠ **arXiv reports this paper as withdrawn (by {agent}).** "
        "Withdrawal is not retraction: arXiv has no peer-reviewed retraction "
        "process. This signal is unverified and flags the paper for human review "
        "before any claim from it is trusted."
    )


class ArxivSourceWriter(BaseSourceWriter):
    """Render parsed arXiv works into ``sources/`` markdown originals."""

    identity_key = "arxiv_id"
    source_name = "arxiv"

    def identity_of(self, parsed: ParsedArxivWork) -> str:
        # The BASE id, never versioned_id: P3 idempotence keys on it, so a later
        # version re-import must match and skip rather than write a second file.
        return parsed.arxiv_id

    def slug_fields(self, parsed: ParsedArxivWork) -> tuple[str, str, str]:
        first_author = parsed.authors[0] if parsed.authors else ""
        # `year` is the <published> (v1 submission) year, stable across versions.
        year = str(parsed.year) if parsed.year else ""
        return (first_author, year, parsed.title or "")

    def cross_ids(self, parsed: ParsedArxivWork) -> dict[str, str]:
        """DOI lets §7.1 spot this paper arriving from another database.

        Bare ``doi`` on purpose — see the module docstring. arXiv exposes no PMID.
        """
        return {"doi": parsed.doi} if parsed.doi else {}

    def render(self, parsed: ParsedArxivWork, imported_at: str = "") -> str:
        return self._front_matter(parsed, imported_at) + self._body(parsed)

    def _front_matter(self, parsed: ParsedArxivWork, imported_at: str) -> str:
        lines = [
            "---",
            f"arxiv_id: {_yaml_str(parsed.arxiv_id)}",
            f"arxiv_version: {parsed.version}",
            f"title: {_yaml_str(parsed.title or '')}",
        ]
        if parsed.authors:
            lines.append(f"authors: {_yaml_list(parsed.authors)}")
        if parsed.year:
            lines.append(f"year: {parsed.year}")
        if parsed.primary_category:
            lines.append(f"primary_category: {_yaml_str(parsed.primary_category)}")
        # Controlled, moderator-curated vocabulary: no score filter (§4.3),
        # primary category first (the parser already orders it that way).
        if parsed.categories:
            lines.append(f"tags: {_yaml_list(parsed.categories)}")
        # Bare `doi`, not `arxiv_doi`: the cross-source index scans the literal key.
        if parsed.doi:
            lines.append(f"doi: {_yaml_str(parsed.doi)}")
        if parsed.journal_ref:
            lines.append(f"journal: {_yaml_str(parsed.journal_ref)}")
        # Always true: this record is the arXiv deposit. `journal`/`doi` carry the
        # published-ness factually; flipping this would front-run merge logic.
        lines.append("preprint: true")
        lines.append("imported_from: arxiv")
        if imported_at:
            lines.append(f"imported_at: {_yaml_str(imported_at)}")
        # Source-scoped, at the end, mirroring `openalex_is_retracted`. Both keys
        # appear only when withdrawn; the agent is recorded, never assumed.
        if parsed.withdrawn:
            lines.append("arxiv_withdrawn: true")
            lines.append(f"arxiv_withdrawn_by: {_yaml_str(parsed.withdrawn_by or '')}")
        lines.append("---")
        return "\n".join(lines) + "\n"

    def _body(self, parsed: ParsedArxivWork) -> str:
        parts = [f"\n# {parsed.title or 'Untitled'}\n"]
        if parsed.withdrawn:
            parts.append(f"\n{withdrawal_warning(parsed.withdrawn_by)}\n")
        if self.include_abstract:
            parts.append("\n## Abstract\n")
            parts.append(f"\n{parsed.abstract or '_No abstract available._'}\n")
        parts.append("\n## Original source\n")
        # The versioned abs URL, e.g. https://arxiv.org/abs/2311.09277v2.
        parts.append(f"\n- arXiv: `{parsed.abs_url}`")
        if parsed.doi:
            parts.append(f"\n- DOI: {parsed.doi}")
        return "".join(parts) + "\n"
