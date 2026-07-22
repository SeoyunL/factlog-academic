#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Turn a Zotero API item into factlog's standard bibliographic dict.

A pure, deterministic transform — no network, no filesystem — so it is fully
unit-testable and satisfies P3: the same item always yields the same dict.
:mod:`factlog.integrations.zotero.source_writer` consumes this dict, so the
schema here is a contract:

    {
      "zotero_key":    str,   # Zotero 8-char item key (provenance identifier)
      "item_type":     str,   # e.g. "journalArticle"
      "title":         str,
      "authors":       [ {"last": str, "first": str, "name": str} ],  # in order
      "year":          str,   # 4-digit year extracted from `date` ("" if none)
      "date":          str,   # raw Zotero date string
      "journal":       str,   # publicationTitle
      "doi":           str,
      "pmid":          str,   # parsed from the free-form `extra` field
      "abstract":      str,   # abstractNote
      "tags":          [str], # in input order
      "date_modified": str,   # dateModified (used later to judge staleness)
      "retracted":     bool,  # a case-insensitive "retracted" tag is present
    }

Only the Zotero item ``key`` is kept as the identifier. The spec sketch showed a
separate numeric ``zotero_id`` and string ``zotero_key``, but the Zotero
Web/Local API identifies items solely by ``key`` (the ``zotero://`` select URI
uses it too), so a single ``zotero_key`` avoids inventing an id the API does not
expose.

Determinism note: creator and tag order are preserved exactly as Zotero returns
them — never sorted — because a reordering would change the derived source file
(first author drives the slug) and break idempotent re-import.
"""
from __future__ import annotations

import re
import unicodedata

_YEAR_RE = re.compile(r"\d{4}")
_PMID_RE = re.compile(r"\bPMID\s*[:=]?\s*(\d+)", re.IGNORECASE)
# A DOI in `extra` is taken only from a line that carries a DOI label, and only
# the canonical DOI core (10.<registrant>/<suffix>) is kept — this avoids
# capturing "doi.org" URL cruft (e.g. ".org/10.1/x") as if it were the DOI.
_DOI_LABEL_RE = re.compile(r"\bDOI\b\s*[:=]?\s*(.+)", re.IGNORECASE)
_DOI_CORE_RE = re.compile(r"10\.\d+/[^\s\"'<>]+")


def _data_of(item: dict) -> dict:
    """Return the Zotero ``data`` sub-object, tolerating a bare data dict.

    pyzotero returns ``{"key": ..., "data": {...}}``; callers (or tests) may pass
    just the inner data dict. Either is accepted.
    """
    if not isinstance(item, dict):
        return {}
    data = item.get("data")
    return data if isinstance(data, dict) else item


def _str(value: object) -> str:
    """Coerce a field to a stripped string; non-strings (incl. None) -> ""."""
    return value.strip() if isinstance(value, str) else ""


def _ascii_digits(run: str) -> str:
    """Rewrite a run of Unicode decimal digits to its ASCII spelling.

    Only ever called on text captured by a ``\\d`` group, and Python's ``\\d`` is
    exactly the Unicode ``Nd`` (decimal number) category — every member of which
    has a defined decimal value — so ``unicodedata.digit`` is total here and the
    function cannot raise. Verified against all 680 code points ``\\d`` matches.

    Deliberately NOT ``NFKC``, for two measured reasons:

    - NFKC is incomplete. It folds full-width ``２０２０`` but leaves Arabic-Indic
      ``٢٠٢٠``, Devanagari ``२०२०`` and Extended-Arabic ``۲۰۲۰`` untouched, and
      ``\\d`` matches all of those. They would still reach the front matter
      non-ASCII and still be refused downstream (#388).
    - NFKC is too broad. It also manufactures digits out of characters that are
      not digits at all (``①`` -> ``1``, ``²`` -> ``2``), so running it over the
      raw date could invent a year the source never stated. The codebase
      normalizes to NFC precisely to avoid that class of folding.

    Mapping the matched run digit-by-digit is exactly as wide as the match and
    no wider.
    """
    return "".join(str(unicodedata.digit(char)) for char in run)


def extract_year(date: object) -> str:
    """First 4-digit run in a Zotero date ("2005", "June 2005", "2005-06-01").

    The returned year is always ASCII. Zotero is an external source whose ``date``
    field is whatever the library holds, so a non-ASCII digit here is upstream
    data, not something the user typed or can fix from inside factlog — normalizing
    is right where refusing (#388, for hand-written literals) is right.

    Normalizing rather than narrowing ``_YEAR_RE`` to ``[0-9]{4}``: narrowing makes
    ``２０２０-06-01`` match nothing at all (no 4-run of ASCII digits survives), so
    ``year`` would silently go empty and lose a fact the source did state. Keeping
    the wide match and converting preserves the year.
    """
    match = _YEAR_RE.search(date) if isinstance(date, str) else None
    return _ascii_digits(match.group(0)) if match else ""


def extract_pmid(extra: object) -> str:
    """PMID from the free-form ``extra`` field (multi-line, any case).

    The first ``PMID`` match wins if several appear.

    ASCII-normalized for the same reason as :func:`extract_year`, and safely so: a
    PMID is by definition a decimal integer, so a non-ASCII spelling is a rendering
    of the same identifier and converting it yields the PubMed record actually
    meant. The DOI path is deliberately left alone — a DOI suffix is an opaque
    string, not a number, so rewriting its characters would fabricate a *different*
    identifier rather than respell the same one.
    """
    if not isinstance(extra, str):
        return ""
    match = _PMID_RE.search(extra)
    return _ascii_digits(match.group(1)) if match else ""


def _doi_from_extra(extra: object) -> str:
    """DOI core from the first DOI-labelled line of ``extra``, or "".

    Scans line by line so a stray identifier elsewhere cannot leak in, and keeps
    only the ``10.x/y`` core so a ``doi.org`` URL wrapper is stripped.
    """
    if not isinstance(extra, str):
        return ""
    for line in extra.splitlines():
        label = _DOI_LABEL_RE.search(line)
        if not label:
            continue
        core = _DOI_CORE_RE.search(label.group(1))
        if core:
            return core.group(0).rstrip(".,;")
    return ""


def parse_creators(creators: object) -> list[dict]:
    """Normalize Zotero creators to ordered ``{last, first, name}`` dicts.

    Only ``author``-type creators are kept (editors/translators are dropped for
    phase 1). A creator may be two-field (``firstName``/``lastName``) or
    single-field (``name`` — institutions, "et al." placeholders). ``name`` is a
    display string: "Last First" for two-field, the raw ``name`` otherwise.
    Order is preserved.
    """
    if not isinstance(creators, list):
        return []
    out: list[dict] = []
    for creator in creators:
        if not isinstance(creator, dict):
            continue
        if _str(creator.get("creatorType")).lower() not in ("", "author"):
            # An explicit non-author role is skipped; a missing role is treated
            # as an author (some exports omit creatorType). Case-insensitive so a
            # stray "Author" is not silently dropped.
            continue
        last = _str(creator.get("lastName"))
        first = _str(creator.get("firstName"))
        single = _str(creator.get("name"))
        if last or first:
            display = " ".join(part for part in (last, first) if part)
        elif single:
            display = single
        else:
            continue  # an empty creator carries no information
        out.append({"last": last, "first": first, "name": display})
    return out


def extract_tags(tags: object) -> list[str]:
    """Ordered list of tag strings from Zotero's ``[{"tag": ...}]`` shape.

    Both manual and automatic (``type: 1``) tags are kept — an automatic MeSH or
    Retraction-Watch tag is still signal a reviewer may want. ``type`` is ignored.
    """
    if not isinstance(tags, list):
        return []
    out: list[str] = []
    for entry in tags:
        if isinstance(entry, dict):
            tag = _str(entry.get("tag"))
        elif isinstance(entry, str):
            tag = entry.strip()
        else:
            tag = ""
        if tag:
            out.append(tag)
    return out


def parse_item(item: dict) -> dict:
    """Transform one Zotero API item into the standard bibliographic dict."""
    data = _data_of(item)

    # `key` may sit on the item wrapper or inside data; prefer data.
    key = _str(data.get("key")) or (_str(item.get("key")) if isinstance(item, dict) else "")

    extra = data.get("extra")
    tags = extract_tags(data.get("tags"))
    date = _str(data.get("date"))

    return {
        "zotero_key": key,
        "item_type": _str(data.get("itemType")),
        "title": _str(data.get("title")),
        "authors": parse_creators(data.get("creators")),
        "year": extract_year(date),
        "date": date,
        "journal": _str(data.get("publicationTitle")),
        "doi": _str(data.get("DOI")) or _doi_from_extra(extra),
        "pmid": extract_pmid(extra),
        "abstract": _str(data.get("abstractNote")),
        "tags": tags,
        "date_modified": _str(data.get("dateModified")),
        # Substring match so retraction variants ("Retraction", "Retracted
        # Publication", "RETRACTED ARTICLE") all raise the flag. This only
        # surfaces a warning for the human gate — never auto-decides — so
        # over-flagging is safer than missing a real retraction.
        "retracted": any("retract" in tag.lower() for tag in tags),
    }


class ItemParser:
    """Stateless wrapper matching the integration's documented interface."""

    def parse(self, item: dict) -> dict:
        return parse_item(item)
