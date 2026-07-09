# SPDX-License-Identifier: Apache-2.0
"""Zotero-specific text markers, plus the shared text/IO helpers re-exported.

``yaml_scalar``/``atomic_write_text`` now live in
:mod:`factlog.integrations.common._textio` so every integration shares one
implementation. They are re-exported here because the Zotero writers (and their
tests) already import them from this module.
"""
from __future__ import annotations

import re

from factlog.integrations.common._textio import atomic_write_text, yaml_list, yaml_scalar

__all__ = [
    "ANNOTATION_SOURCE_MARKER",
    "ANNOTATION_MARKER_RE",
    "atomic_write_text",
    "yaml_list",
    "yaml_scalar",
]

# Front-matter marker identifying a companion annotation source (<stem>-notes.md).
# Shared so the annotation writer (which stamps it) and the bibliographic writer
# (which must ignore such files when de-duplicating by zotero_key) agree, and so
# both match it line-anchored — a title/tag that merely contains the text is not
# mistaken for the marker.
ANNOTATION_SOURCE_MARKER = "source_kind: annotations"
ANNOTATION_MARKER_RE = re.compile(r"^source_kind:\s*annotations\s*$", re.MULTILINE)
