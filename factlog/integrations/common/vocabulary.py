#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""The closed value spaces an integration's ledger fields draw from.

## Why these constants moved down here (#109)

``common/provenance.py`` judges a ledger's *signal* fields at the read boundary: a
present ``is_retracted`` that is not a ``bool``, or a present ``withdrawn_by`` that
names no agent, raises rather than being read as "no signal". To do that it must know
which agents exist — and it must know it **always**, not only when some other module
happened to be imported first (a registry each integration fills at import time would
make a corrupt ledger's fate depend on import order, which is the failure #109 removes).

Importing the constants from ``arxiv/work_parser.py`` would have given ``common`` that
knowledge at the price of a ``common -> arxiv`` arrow: a one-way door, after which no
arXiv module on that import chain could ever import ``common/provenance.py``,
``common/backfill.py``, ``common/acknowledge.py`` or ``common/source_writer.py``. So the
dependency is inverted instead. The vocabulary lives here, at the bottom; arXiv imports
it and re-exports it under its own name, because ``withdrawn_by`` is still **arXiv's**
word (#57 §6.3, #93 Q2) and every reader of that field should meet it there.

This module holds values, not meanings. It imports nothing, names no policy, and exists
so that a value space has exactly one definition — a third withdrawal agent is added in
one place, and the read boundary and the parser cannot disagree about what the second
one was called.
"""
from __future__ import annotations

__all__ = [
    "WITHDRAWN_BY_ADMIN",
    "WITHDRAWN_BY_AUTHOR",
]

#: arXiv's two withdrawal agents. §6.3 called withdrawal "the author's own action", but
#: arXiv administrators also withdraw papers (authorship disputes, inflammatory content),
#: and a bare boolean would force downstream text to claim the author withdrew a paper the
#: administrators pulled. See ``arxiv/work_parser.py`` for the detection that produces them.
WITHDRAWN_BY_AUTHOR = "author"
WITHDRAWN_BY_ADMIN = "admin"
