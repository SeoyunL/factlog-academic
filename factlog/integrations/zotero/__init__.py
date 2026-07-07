# SPDX-License-Identifier: Apache-2.0
"""Zotero integration (roadmap phase 1): import bibliographic metadata.

Phase 1 copies bibliographic metadata from a personal Zotero library into a
factlog KB's ``sources/`` as markdown originals, one file per item, carrying a
provenance header (Zotero item key, DOI, PMID, authors, journal, year). It never
writes back to Zotero (one-way, read-only) and the imported items remain plain
candidates until the usual ``sync -> review -> accept`` gate promotes them.

Only :mod:`factlog.integrations.zotero.config` is import-light; the API client
imports ``pyzotero`` lazily so the extra dependency is required only when an
import actually runs.
"""

DEFAULT_LOCAL_PORT = 23119
