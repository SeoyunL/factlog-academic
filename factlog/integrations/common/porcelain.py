# SPDX-License-Identifier: Apache-2.0
"""The one neutralization rule every ``--porcelain`` row shares (issue #141).

A porcelain row is a tab-separated positional contract (#78): a fixed number of
fields, read by column offset. Any caller-influenced value in a row — a source or
corrupt-ledger *path* in an id column, an ``OSError`` message that carries a path in a
``reason``, a list of sidecar paths — can hold a tab and add a column, or a CR/LF and
split the row; either way a positional consumer reads the wrong field, silently.

The arXiv and OpenAlex integrations both emit such rows, so the rule lives here rather
than hand-mirrored in each (#111 added it to OpenAlex's ``reason`` alone; #141 found the
copy in arXiv had drifted to cover more fields). One definition keeps "both integrations'
porcelain emits the documented field count" from quietly splitting in two.
"""
from __future__ import annotations


def porcelain_field(text: str) -> str:
    """Replace every tab, CR and LF in ``text`` with a single space each.

    Each control character maps to one space, so ``"\\r\\n"`` becomes two spaces. That is
    deliberate: the guarantee is that no tab, CR or LF survives — never that the field's
    length is preserved — so a row keeps its field count and stays a single line. A
    human-readable field elsewhere in the report is left untouched; this is only for the
    machine contract.
    """
    return text.replace("\t", " ").replace("\r", " ").replace("\n", " ")
