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
from factlog.integrations.common.provenance import (
    ProvenanceConflict,
    ProvenanceError,
    SourceRecord,
    add_source,
    read_provenance,
    sidecar_path,
    write_provenance,
)
from factlog.integrations.common.source_writer import BaseSourceWriter, WriteResult

__all__ = ["ArxivSourceWriter", "WriteResult", "withdrawal_agent", "withdrawal_warning"]

# The prose agent name for each withdrawer, used in the body warning and the
# CLI's stderr note. The word "retracted" never appears here (see the module
# docstring); "retraction" does, only to state that the two are not the same.
_WITHDRAWAL_AGENTS = {
    WITHDRAWN_BY_AUTHOR: "the author",
    WITHDRAWN_BY_ADMIN: "arXiv administrators",
}


# The fields whose change an *import* has no authority to absorb. A version bump
# means the deposit itself moved; a withdrawal is a signal the human gate must
# see. Everything else — `comment`, `primary_category`, `last_updated` — is
# upstream metadata that arXiv edits without cutting a new version (a moderator
# recategorizes, an author appends "Accepted at ICML 2024" to the comment). If a
# drift there were a divergence, a routine re-import of such a paper would error
# forever, and the suggested remedy — `arxiv-check-versions`, which compares
# versions — could never clear it. Those fields go stale in the ledger until a
# refresh updates them, which is exactly the import/refresh boundary #58 drew.
_IDENTIFYING_FIELDS = ("version", "withdrawn_by")


def _identity_fields(record: SourceRecord) -> dict:
    """The subset of a record that an import may not revise. See :data:`_IDENTIFYING_FIELDS`.

    ``imported_at`` is deliberately absent: it records when factlog first saw the
    provenance, not a fact about the paper, and the CLI stamps a fresh one every
    run. Comparing it would make a plain re-import look like a conflict.
    """
    return {name: record.fields.get(name) for name in _IDENTIFYING_FIELDS}


def _divergence(existing: SourceRecord, incoming: SourceRecord) -> str:
    """Why an import refuses to revise the ledger, naming the field that moved.

    The message must not invent a version bump: a withdrawal can appear without
    one, and pointing a user at ``arxiv-check-versions`` for a change that command
    does not look at would leave them with no way forward.
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
    # arXiv is the one integration that merges (§7.3): when a paper is already in
    # the KB via another database (an OpenAlex record of its published version,
    # matched on the shared arXiv id or DOI), the arXiv deposit is folded into
    # that original's provenance sidecar instead of writing a second file. Zotero
    # and OpenAlex leave this False and never touch a sidecar.
    merges_cross_source = True

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

    def _merge(
        self, parsed: ParsedArxivWork, decision: WriteResult, imported_at: str
    ) -> WriteResult:
        """Append this arXiv deposit to the existing original's sidecar (§7.3).

        The original ``.md`` is never opened — only its sidecar at
        ``sidecar_path(decision.path)`` is read-modify-written, so the original
        stays byte- and mtime-immutable (P4). The disk ledger is re-read every
        call (never cached), so a prior merge is respected.

        Idempotence (P3) is judged on the deposit's identity, never the import
        clock. The CLI stamps a fresh ``imported_at`` on every run, so comparing
        whole records (as :func:`add_source` does) would flag a plain re-import as
        a conflict. An arXiv record already present with the same
        :data:`_IDENTIFYING_FIELDS` is a no-op that keeps the *first* import's
        timestamp, and the ledger stays byte-identical.

        A **version bump or a withdrawal** is a divergence an *import* has no
        authority to revise (H2): it becomes a per-id ``error`` pointing at the
        refresh path (``arxiv-check-versions``), which alone may rewrite the entry
        via :func:`update_source`. Drift in the other fields is absorbed silently
        — see :data:`_IDENTIFYING_FIELDS` for why.

        Every failure is a per-id ``error``, never a batch crash. A corrupt or
        unreadable sidecar is one paper's problem; the imports queued behind it
        are not.
        """
        sidecar = sidecar_path(decision.path)
        record = self._provenance_record(parsed, imported_at)
        try:
            provenance = read_provenance(sidecar)
        except ProvenanceError as exc:
            return WriteResult(
                decision.path, "error",
                f"provenance sidecar is unreadable ({exc}); repair or delete "
                f"{sidecar.name} and re-import",
            )

        existing = next((r for r in provenance.records if r.key == record.key), None)
        if existing is not None:
            if _identity_fields(existing) == _identity_fields(record):
                return decision  # already recorded; keep the first timestamp
            return WriteResult(decision.path, "error", _divergence(existing, record))

        try:
            add_source(provenance, record)
            write_provenance(sidecar, provenance)
        except ProvenanceConflict:  # pragma: no cover - guarded by the check above
            return WriteResult(
                decision.path, "error",
                "provenance ledger diverged; run arxiv-check-versions",
            )
        except (ProvenanceError, OSError) as exc:
            return WriteResult(
                decision.path, "error", f"cannot write {sidecar.name}: {exc}",
            )
        return decision
