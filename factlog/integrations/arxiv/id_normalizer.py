#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Canonicalize the many shapes an arXiv identifier arrives in (#57 §2.1).

Validation lives here, before the request, for the same reason
:func:`factlog.integrations.openalex.api_client.normalize_work_id` validates
OpenAlex ids: **arXiv answers a well-formed-but-wrong id with HTTP 200 and zero
entries.** There is no 404. Measured (#57):

* ``arXiv:1706.03762`` — the form arXiv itself prints on every abs page and in
  every BibTeX entry it emits — returns **zero entries**. Passed through
  untouched, the single most likely copy-paste silently finds nothing.
* ``math.GT/0309136`` returns zero entries; the API wants ``math/0309136``. The
  subject class must be stripped, and the API echoes ids back without it.
* ``10.48550/arXiv.1706.03762`` at least fails loudly (HTTP 400).
* Only syntactic garbage (``notanid``) produces a 400.

Two id schemes exist, told apart by the ``/``:

* **new-style** (2007-04 onward) ``YYMM.NNNN`` or ``YYMM.NNNNN`` — the sequence
  widened from four to five digits in 2015; both remain valid.
* **old-style** (pre 2007-04) ``archive/YYMMNNN``, optionally cited with a
  subject class (``math.GT/0309136``). The archive itself may contain a hyphen
  (``hep-th``, ``cond-mat``) but never a dot, which is what makes stripping the
  subject class exact: everything from the first ``.`` to the ``/`` goes.

Modelling the subject class was tried and is wrong: they are not two uppercase
letters (``cond-mat.stat-mech``, ``physics.flu-dyn``, ``nlin.CD``), and a regex
that assumes so both rejects the valid ``hep-th/9901001`` and accepts the
silently-missing ``math.GT/0309136``.

Version, when present, is carried alongside the base id rather than inside it:
``arxiv-check-versions`` compares versions, and the base id is the join key for
cross-source duplicate detection (spec §5.2).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from factlog.integrations.arxiv.config import OLD_STYLE_ARCHIVES

__all__ = ["ArxivId", "ArxivIdError", "normalize_arxiv_id"]

# `YYMM.NNNN` (2007-04..2014) or `YYMM.NNNNN` (2015-01..), optional `vN`.
_NEW_STYLE_RE = re.compile(
    r"^(?P<base>[0-9]{4}\.[0-9]{4,5})(?:v(?P<version>[0-9]+))?$", re.IGNORECASE
)

# The right-hand side of an old-style id: exactly seven digits, optional `vN`.
_OLD_STYLE_TAIL_RE = re.compile(
    r"^(?P<seq>[0-9]{7})(?:v(?P<version>[0-9]+))?$", re.IGNORECASE
)

# The `/abs/` or `/pdf/` path segment of an arxiv.org URL. Matched case-
# insensitively on the *original* string: locating it in a lowercased copy and
# slicing the original by that index is unsafe, because `str.lower()` is not
# length-preserving for every character.
_URL_MARKER_RE = re.compile(r"/(?:abs|pdf)/", re.IGNORECASE)

# Prefixes and wrappers users paste. Order matters: the DOI form is checked
# before the bare `arxiv:` prefix, since it contains one.
_DOI_PREFIX = "10.48550/arxiv."
_ARXIV_PREFIX = "arxiv:"


class ArxivIdError(Exception):
    """A string could not be read as an arXiv identifier."""


@dataclass(frozen=True)
class ArxivId:
    """A parsed arXiv identifier.

    ``base`` is the canonical, version-free form the API accepts and echoes:
    ``2311.09277`` or ``math/0309136``. ``version`` is None when the caller did
    not pin one, which means "whatever arXiv currently considers latest".
    """

    base: str
    version: int | None = None

    def __str__(self) -> str:
        return self.base if self.version is None else f"{self.base}v{self.version}"

    @property
    def query_value(self) -> str:
        """The value to place in ``id_list``. A pinned version is sent as-is."""
        return str(self)

    @property
    def abs_url(self) -> str:
        return f"https://arxiv.org/abs/{self}"


def _strip_wrappers(raw: str) -> str:
    """Reduce a URL/DOI/prefixed citation to the bare identifier text."""
    candidate = raw.strip()

    # https://arxiv.org/abs/2311.09277v2       -> 2311.09277v2
    # https://arxiv.org/pdf/math/0309136v1.pdf -> math/0309136v1
    # https://arxiv.org/abs/2311.09277?context=cs.CL -> 2311.09277
    marker = _URL_MARKER_RE.search(candidate)
    if marker is not None:
        candidate = candidate[marker.end():]
        # An abs URL commonly carries `?context=cs.CL`; a fragment is rarer.
        for separator in ("?", "#"):
            candidate = candidate.split(separator, 1)[0]
    candidate = candidate.strip("/")
    if candidate.lower().endswith(".pdf"):
        candidate = candidate[:-4]

    # 10.48550/arXiv.1706.03762 -> 1706.03762   (arXiv's own DataCite DOI form)
    if candidate.lower().startswith(_DOI_PREFIX):
        return candidate[len(_DOI_PREFIX):].strip()

    # arXiv:1706.03762 -> 1706.03762
    if candidate.lower().startswith(_ARXIV_PREFIX):
        candidate = candidate[len(_ARXIV_PREFIX):]

    return candidate.strip()


def _parse_old_style(candidate: str) -> ArxivId:
    """``archive[.subject_class]/YYMMNNN[vN]`` -> canonical ``archive/YYMMNNN``."""
    left, _, right = candidate.partition("/")
    # The archive never contains a dot; the subject class always follows one.
    # Splitting on the first dot is therefore exact and needs no subject grammar.
    archive = left.split(".", 1)[0].lower()
    if archive not in OLD_STYLE_ARCHIVES:
        raise ArxivIdError(
            f"unknown arXiv archive {archive!r} in {candidate!r}; "
            f"expected one of the {len(OLD_STYLE_ARCHIVES)} pre-2007 archives "
            "(e.g. 'hep-th', 'math', 'cond-mat')."
        )
    match = _OLD_STYLE_TAIL_RE.match(right)
    if match is None:
        raise ArxivIdError(
            f"invalid old-style arXiv id {candidate!r}; expected the form "
            "'math/0309136' (seven digits, optional 'vN')."
        )
    version = match.group("version")
    return ArxivId(f"{archive}/{match.group('seq')}", int(version) if version else None)


def normalize_arxiv_id(value: str) -> ArxivId:
    """Return the canonical :class:`ArxivId` for any accepted input form.

    Accepts a bare id, an ``arXiv:`` prefix, an ``arxiv.org/abs/`` or
    ``/pdf/`` URL, a trailing ``.pdf``, arXiv's ``10.48550/arXiv.<id>`` DOI, and
    an old-style id with or without its subject class — each with an optional
    ``vN`` suffix. Raises :class:`ArxivIdError` on anything else rather than
    sending it, because the API would answer 200 with no entries.
    """
    if not isinstance(value, str) or not value.strip():
        raise ArxivIdError("arXiv id must be a non-empty string.")

    candidate = _strip_wrappers(value)
    if not candidate:
        raise ArxivIdError(f"invalid arXiv id {value!r}: no identifier found.")

    if "/" in candidate:
        return _parse_old_style(candidate)

    match = _NEW_STYLE_RE.match(candidate)
    if match is None:
        raise ArxivIdError(
            f"invalid arXiv id {value!r}; expected the form '2311.09277' "
            "(new-style) or 'math/0309136' (old-style), each with an optional "
            "'vN' suffix."
        )
    version = match.group("version")
    # A pinned v0 does not exist; arXiv numbers versions from v1.
    if version is not None and int(version) < 1:
        raise ArxivIdError(f"invalid arXiv version in {value!r}; versions start at v1.")
    return ArxivId(match.group("base"), int(version) if version else None)


def parse_entry_id(entry_id: str) -> ArxivId:
    """Read the ``<id>`` URL arXiv echoes back, e.g. ``http://arxiv.org/abs/2311.09277v1``.

    The version is available *only* here — the Atom response carries no version
    field of its own (spec §4.1). A response entry always pins a version, so an
    :class:`ArxivId` from this function always has ``version`` set.
    """
    parsed = normalize_arxiv_id(entry_id)
    if parsed.version is None:
        raise ArxivIdError(
            f"arXiv returned an entry id with no version: {entry_id!r}. "
            "Every response entry is expected to pin one."
        )
    return parsed
