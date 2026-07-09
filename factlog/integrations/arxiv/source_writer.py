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
when ``journal``/``doi`` show a published version exists. The front-matter ``preprint`` flag is
**never flipped**, at merge time or ever: an arXiv record describes an arXiv
deposit, which stays a preprint whatever else exists (#60), and the original is
byte-immutable (P4) so there is no file to flip. Whether a peer-reviewed version
exists is derived from the provenance ledger — a non-preprint record beside this
one — not from a boolean anyone must keep in sync (§5.3, #65 H1).
"""
from __future__ import annotations

from factlog.integrations.arxiv.work_parser import (
    WITHDRAWN_BY_ADMIN,
    WITHDRAWN_BY_AUTHOR,
    ParsedArxivWork,
)
from factlog.integrations.common._textio import yaml_list as _yaml_list
from factlog.integrations.common._textio import yaml_scalar as _yaml_str
from factlog.integrations.common.provenance import SourceRecord
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
    # arXiv merges (§7.3): when a paper is already in the KB via another database
    # (an OpenAlex record of its published version, matched on the shared arXiv id
    # or DOI), the arXiv deposit is folded into that original's provenance sidecar
    # instead of writing a second file. OpenAlex now merges too; Zotero does not.
    merges_cross_source = True
    # The fields whose change an *import* has no authority to absorb. A version
    # bump means the deposit itself moved; a withdrawal is a signal the human gate
    # must see. Everything else — ``comment``, ``primary_category``,
    # ``last_updated`` — is upstream metadata arXiv edits without cutting a new
    # version (a moderator recategorizes, an author appends "Accepted at ICML 2024"
    # to the comment). If a drift there were a divergence, a routine re-import of
    # such a paper would error forever, and the remedy — ``arxiv-check-versions``,
    # which compares versions — could never clear it. Those fields go stale in the
    # ledger until a refresh updates them (the import/refresh boundary #58 drew).
    # arXiv can afford a non-empty tuple precisely because it HAS that refresh
    # command; OpenAlex, which does not, keeps the base's empty default.
    _IDENTIFYING_FIELDS = ("version", "withdrawn_by")

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
        # Always true: this record is the arXiv deposit, a preprint whatever else
        # exists (#60). `journal`/`doi` carry published-ness factually; this is
        # never flipped, at merge time or ever (#65 H1) — the published version's
        # existence is a ledger fact, not a boolean to keep in sync.
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

    # -- §7.3 merge into an existing original's provenance sidecar ----------
    def _provenance_record(self, parsed: ParsedArxivWork, imported_at: str) -> SourceRecord:
        """The arXiv contribution to a source's provenance ledger.

        Identity is the BASE, version-free id — the join key and the ledger's
        idempotency key both use it, so a re-import of the same version is a
        no-op. Fields carry what is arXiv-specific and stable per version:
        ``version``, ``submitted``, ``last_updated``, ``comment``,
        ``primary_category``, and ``withdrawn_by`` when the paper is withdrawn.

        Deliberately excluded: ``abs_url``/``pdf_url`` (derivable from the id, so
        storing them duplicates a thing that can go stale) and ``doi``/
        ``journal_ref`` (the merge target is an OpenAlex-primary record that
        already holds these authoritatively, and the DOI is the join key — a
        second copy invites disagreement about which is right).

        ``submitted``/``last_updated`` are ``datetime.date``. ``SourceRecord``
        serializes through ``json.dumps``, which raises ``TypeError`` on a
        ``date``, and ``provenance`` is source-agnostic and correctly refuses to
        guess types — so the conversion to an ISO string is this builder's job. A
        ``None`` field is dropped by :meth:`SourceRecord.to_dict`, so optional
        values pass straight through.
        """
        fields: dict[str, object | None] = {
            "version": parsed.version,
            "submitted": parsed.submitted.isoformat() if parsed.submitted else None,
            "last_updated": parsed.last_updated.isoformat() if parsed.last_updated else None,
            "comment": parsed.comment,
            "primary_category": parsed.primary_category or None,
        }
        if parsed.withdrawn_by is not None:
            fields["withdrawn_by"] = parsed.withdrawn_by
        return SourceRecord(
            type="arxiv", id=parsed.arxiv_id, imported_at=imported_at, fields=fields
        )

    def _divergence(self, existing: SourceRecord, incoming: SourceRecord) -> str:
        """Why an import refuses to revise the ledger, naming the field that moved.

        Overrides the base's generic message to point at ``arxiv-check-versions``,
        the refresh command that alone may rewrite the entry via
        :func:`update_source`. The message must not invent a version bump: a
        withdrawal can appear without one, and pointing a user at a version
        comparison for a change that command does not look at would leave them with
        no way forward. This text names ``arxiv-check-versions`` and so must never
        reach a non-arXiv error — which it cannot, because a writer keeping the
        empty :attr:`_IDENTIFYING_FIELDS` default never reaches :meth:`_divergence`.
        """
        was, now = existing.fields.get("version"), incoming.fields.get("version")
        if was != now:
            return (
                f"ledger records v{was}, arXiv now serves v{now}; run "
                "arxiv-check-versions to record the new version"
            )
        withdrawn = incoming.fields.get("withdrawn_by")
        if withdrawn:
            return (
                f"arXiv now reports v{now} as withdrawn by {withdrawal_agent(withdrawn)}; "
                "run arxiv-check-versions and review before relying on this source"
            )
        return (
            f"ledger no longer records v{now} as withdrawn; run arxiv-check-versions "
            "to refresh the entry"
        )
