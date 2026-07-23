# SPDX-License-Identifier: Apache-2.0
"""Where a source's YAML front matter ends, and how much of the file to read to find out.

This module is the **single source of truth for "which part of this file is front
matter"**. It answers where the block is — :func:`front_matter_block` — and, when
there is none, which of the four possible reasons that was
(:func:`front_matter_absence`, #422). Both are readings of the same scan and
neither takes a position on what a caller does with the answer: the reader does
not warn, refuse, or print, so a validator can build an operator-facing message on
the reason without this module acquiring an opinion. The two readers built on it want
different things out of a block (:mod:`factlog.bibtex` parses it into a citation,
:mod:`factlog.integrations.common.front_matter` pulls scalars for the
de-duplication index) and they keep their own policy: which keys to ask for,
whether an ``ignore_re`` marker voids the file, what to do with what comes back.
Extent here, meaning there.

The extraction is #419. Both readers grew the same chunked scan independently and
the copies had drifted into disagreeing about the same file — see
:func:`front_matter_block` for what that cost. They also agreed character for
character on the parts that were right, including a wrong comment that was copied
along with the code and had to be corrected twice (#417).

**Anything added here has to be a claim about where the block is.** How to parse
what is inside it is not that claim; that a missing end is worth warning an
operator about is not either — only that the end was not found, and how far the
search got before saying so.
"""
from __future__ import annotations

from pathlib import Path

# How much to pull per read while looking for the closing fence, and the point at
# which the search for one gives up. A well-formed block stops at its own fence,
# however long it is; the cap bounds the search on a file that has no closing fence
# to find.
#
# The cap is a *limit on the search*, not only on the pathological file: a block
# that is genuinely closed but longer than the cap is cut off before its fence is
# reached, and is then indistinguishable from an unclosed one — so it reads as no
# front matter at all. At ~40 characters per author that needs roughly 26,000
# authors in one ``authors:`` line, which no record approaches (the largest real
# collaborations run to a few thousand), so the cap buys bounded reads at a price
# nothing pays. Raising it costs only memory on malformed files.
#
# Both constants count **characters, not bytes**, as their names say: these are read
# from a text handle, so ``fh.read(n)`` yields n characters and ``len(head)`` counts
# characters. The distinction is not pedantic — a CJK front matter can run to three
# times the cap in bytes and still be read whole, so any budget reasoned in bytes
# understates the real ceiling by up to 3x. This exact confusion has now cost two
# separate fixes: the byte/char mix-up documented here (#419) and the
# ``_HEAD_SCAN_BYTES`` -> ``_HEAD_SCAN_CHARS`` rename in the annotation writer
# (#430), which was the same text-mode read wearing a byte-flavoured name. Keep the
# ``_CHARS`` suffix on anything added here, and say "characters" in the prose.
#
# The chunk size is not a free performance knob — it is load-bearing twice over:
#
# * below 3 it breaks correctness outright. The opening-fence test runs on the
#   first read alone, so a 1- or 2-char chunk makes ``startswith("---")`` false
#   for a perfectly well-formed file and every source reads as empty.
# * it quantises the cap. The loop checks the length *before* reading, so the
#   effective ceiling is ``ceil(FRONT_MATTER_MAX_CHARS / chunk) * chunk``, and
#   changing the chunk moves where the search is actually cut. Powers of two that
#   divide the cap keep that boundary put; other values shift it.
FRONT_MATTER_CHUNK_CHARS = 8192
FRONT_MATTER_MAX_CHARS = 1 << 20

# Why a file yielded no block. :func:`front_matter_block` answers ``None`` to four
# different files and a caller that has to tell an operator something cannot tell
# them apart from it; :func:`front_matter_absence` names which one it was.
#
# These are still claims about where the block is — "its end is not in this file",
# "its end is not in the part that was read" — which is what this module is for.
# Naming a reason is not warning about it: nothing here prints, raises, or refuses.
# The reader stays silent and the caller decides what is worth saying, the same
# split the two readers already keep over which keys to ask for.
#
# The wording follows ``annotation_writer._UNTERMINATED`` (#430): say what was
# observed, not what it implies about the file's owner or intent. A block that
# does not close may be a tool-written source a human damaged, a hand-written note
# that was never closed, or a document that merely opens with ``---``; the scan
# cannot tell, so the reason does not guess.
FRONT_MATTER_NO_OPENING_FENCE = "the file does not open with ---"
FRONT_MATTER_UNCLOSED = "front matter opens with --- and no closing fence appears before the end of the file"
FRONT_MATTER_UNSCANNED = (
    "front matter opens with --- and no closing fence appears in the first "
    f"{FRONT_MATTER_MAX_CHARS} characters, where the search stops"
)
FRONT_MATTER_UNREADABLE = "the file could not be read as UTF-8 text"


def _locate(path: Path | str) -> tuple[str | None, str | None]:
    """``(block, absence)`` for a file — exactly one of the two is not None.

    The single implementation behind both public functions. Keeping the scan in one
    place is the point of the module (#419); a second copy that only computed the
    reason would be free to drift into disagreeing with the block about the same
    file, which is the defect the extraction removed.
    """
    try:
        with Path(path).open("r", encoding="utf-8") as fh:
            head = fh.read(FRONT_MATTER_CHUNK_CHARS)
            if not head.startswith("---"):
                # No opening fence: nothing to find, and no reason to read the body.
                return None, FRONT_MATTER_NO_OPENING_FENCE
            # Stop as soon as the accumulated text holds a fence. Scanning the
            # accumulation rather than the latest chunk is a *read budget*, not a
            # correctness property: the extraction below searches all of ``head``
            # either way, so missing a straddling fence here would not lose it —
            # it would just keep reading to the cap. On a file with a large body
            # that is the difference between two chunks and a megabyte.
            while "\n---" not in head[3:] and len(head) < FRONT_MATTER_MAX_CHARS:
                chunk = fh.read(FRONT_MATTER_CHUNK_CHARS)
                if not chunk:
                    break
                head += chunk
    except (OSError, UnicodeDecodeError):
        return None, FRONT_MATTER_UNREADABLE
    rest = head[3:]
    end = rest.find("\n---")
    if end != -1:
        return rest[:end], None
    # Which of the two loop exits arrived here is the whole distinction between the
    # remaining reasons, and it is decided by the length alone: every read before
    # EOF returns a full chunk, so the loop's length test can only stop it at
    # exactly the cap. Below the cap the loop ran out of file and the absence of a
    # fence is a fact about the file; at the cap the search was abandoned and the
    # absence is only a fact about the part that was read.
    #
    # The boundary belongs to the weaker claim: a file that genuinely ends at the
    # cap with no fence is reported as unscanned rather than unclosed. Both are
    # true of it, and telling them apart would cost a read past the cap for a case
    # that needs a megabyte of front matter to reach.
    if len(head) >= FRONT_MATTER_MAX_CHARS:
        return None, FRONT_MATTER_UNSCANNED
    return None, FRONT_MATTER_UNCLOSED


def front_matter_absence(path: Path | str) -> str | None:
    """Why this file has no front-matter block, or None when it has one.

    One of the ``FRONT_MATTER_*`` reasons above. For a caller that has to act on
    the *absence* rather than on the block: :func:`front_matter_block` collapses
    four files into one ``None``, and the fail-closed policy it documents has a
    cost that only shows up afterwards — a tool-written source whose closing fence
    a human deleted reads as "not imported", drops out of de-duplication, and is
    noticed when a second ``.md`` appears beside the first. Naming the reason is
    what lets a validator say so before that happens (#422).

    Costs a second scan when a caller has already asked for the block. That is the
    price of leaving :func:`front_matter_block`'s signature alone, and it is paid
    only on the failing path, where the alternative is telling an operator nothing.
    """
    return _locate(path)[1]


def front_matter_block(path: Path | str) -> str | None:
    """The text between the opening ``---`` and the closing fence, or None.

    Reads to the block's **closing fence**, not to a fixed character count. A fixed
    window truncated the block mid-way and silently dropped every key past it, and
    both readers were measured losing real metadata to it: the arXiv writer emits
    one long ``authors:`` line ahead of ``year``/``journal``/``imported_from``, so
    against the 2048-character window 50 authors (2104-byte block) already lost
    ``imported_from`` and 60 lost ``year`` and ``journal`` too (#409), and against
    the 4096-character one a 200-author collaboration (7903-byte block) exported as a
    bare ``@misc`` with a title and nothing else — no author, year, venue, DOI, nor
    the type key that makes it a preprint (#395). The ID-keyed paths survived in
    both cases, because the writers emit their identity keys first; what broke was
    everything that reads further.

    The window was never a read budget either: the read stops at the fence, so a
    well-formed source costs its front matter and nothing more, whatever the body
    weighs. Returning early when there is no opening fence keeps that true for the
    ingest conversions that carry an HTML provenance comment instead of YAML.

    Returns None for an unreadable file, one with no opening fence, **and one whose
    closing fence is never found.** That last case is the policy both callers now
    share, and each was measured paying for the alternative:

    * The de-duplication index used to return everything read so far, which meant
      the body: a user's own note that opens with ``---`` and never closes it would
      hand its ``arxiv_id:``/``doi:``/``title:`` body lines to the caller as front
      matter, and the writers' caches would register them as that file's identity
      (``by_identity``/``by_cross_id``/``match_rows`` in
      ``common/source_writer``). An unrelated note then matches a real paper and
      the import is skipped or paired wrongly. Reading to the cap rather than to
      2048 characters widened how much body could be absorbed, but the defect predates
      that — the fixed window only made it smaller, and where the offending line
      sat in the body decided whether it appeared at all (#409).
    * ``export --bibtex`` used to do the same and emit the result. A reading note
      whose fence a human deleted, quoting a paper in its body, exported as
      ``@article{my-reading-note, author = {Ashish Vaswani}, title = {Attention Is
      All You Need}, year = {2017}, journal = {NeurIPS}, doi = {10.5555/3295222}}``
      — exit 0, no warning, and the note's own ``title`` overridden by the quoted
      one, since a later ``key:`` line wins. The belief that this was harmless
      ("the exporter reads only the handful of keys it names, so the entry is
      unaffected") was written down and held until #419 ran it.

    So an unclosed block yields nothing: its extent is unknowable, and a key read
    out of it cannot be told from a body line. The cost is the other direction — a
    genuinely tool-written source whose fence a human deleted now reads as "not
    imported", so re-importing writes a second ``.md`` instead of updating the
    first, and ``export`` reports it as skipped rather than citing it — measured,
    not inferred: an import that reports ``skipped`` against the intact source
    reports ``imported`` once the closing fence is deleted, leaving a ``…-2.md``
    beside it. Those are visible, recoverable outcomes; silently binding a
    stranger's note to a paper's identity, or printing it into a bibliography, is
    neither, which is why the trade is taken in this direction. Restoring the old
    value for such a file is not the goal — the old readers answered from where the
    offending line happened to sit, and an answer decided by an offset is the
    defect, not the behaviour to preserve.

    The tempting middle road — trust an unclosed block only up to where the body
    seems to start — is the one thing not to do here. This locates a block, it does
    not parse YAML, and guessing where front matter ends reintroduces exactly the
    defect above under a different knob.

    ``UnicodeDecodeError`` is caught alongside ``OSError``: undecodable bytes are
    "no front matter", like an unreadable file. **The two readers reached that need
    from opposite histories, and stating them in one sentence got the causation
    wrong once already — so they are separated:**

    * The de-duplication reader took a fixed 2048-char window, which a source that
      is valid UTF-8 in its head and mojibake in its body passed cleanly. Reading
      to the fence puts far more of the file through the codec, so that same file
      began raising at a caller that does not expect it. There the catch is
      cleanup after the widened read (#409).
    * The exporter never had such a window. Before #395 it called
      ``read_text(encoding="utf-8")`` on the **whole file** and sliced to 4096
      afterwards, so mojibake anywhere raised regardless of where the block ended —
      measured on a file whose first bad byte sits at offset 36,015, which
      ``fh.read(2048)`` decodes and ``read_text()[:4096]`` does not. Nothing on that
      path caught it, so one such source aborted the entire run with a traceback
      and wrote no output at all. There the catch is not cleanup after anything:
      it fixes a defect that predates the chunked read (#419).

    Which of those four files produced a ``None`` is :func:`front_matter_absence`'s
    question, not this one's. This stays the reader every caller already has.
    """
    return _locate(path)[0]
