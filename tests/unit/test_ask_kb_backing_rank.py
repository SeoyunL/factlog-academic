# SPDX-License-Identifier: Apache-2.0
"""KB-vocabulary backing credited to a row the lexical scan already cited (#594).

#576 built the bridge but scored it on one side only: a file the scan never cited
became a promoted row, and a file it DID cite got no credit for the accepted facts
that reach it. On a corpus whose sources borrow the English term as their title —
the reference KB's `neurosymbolic` papers — that rule subtracts exactly the right
files, because a topically-matching source is the one the lexical scan finds. The
issue's own evidence file ranked 13th of 28 with a flat (1,1,1) key, and the only
rows the bridge added were the two off-topic ones at 21 and 22.

So the credit has to reach the cited rows, and these assert the shape of it: what
it counts, what it must not count twice, what it may not overrule, and what it
leaves alone. The shell baseline (tests/test_ask_wiki_search.sh PIN9) pins the
resulting ANSWER; a rendered answer can only ever show one combination came out
right.
"""
from __future__ import annotations

import unicodedata

import ask_router

SOURCE_MD = """\
# {title}

{body}
"""


def build(tmp_path, sources, *, accepted=None, candidates=None, supplementary=None):
    """A KB root with the given {name: body} sources and optional fact files."""
    root = tmp_path / "kb"
    (root / "facts").mkdir(parents=True)
    (root / "sources").mkdir()
    (root / "policy").mkdir()
    for name, body in sources.items():
        (root / "sources" / f"{name}.md").write_text(
            SOURCE_MD.format(title=name, body=body), encoding="utf-8"
        )
    for name, body in (supplementary or {}).items():
        (root / "decisions").mkdir(exist_ok=True)
        (root / "decisions" / f"{name}.md").write_text(
            SOURCE_MD.format(title=name, body=body), encoding="utf-8"
        )
    if accepted is not None:
        (root / "facts" / "accepted.dl").write_text(accepted, encoding="utf-8")
    if candidates is not None:
        (root / "facts" / "candidates.csv").write_text(candidates, encoding="utf-8")
    return root


def csv_rows(*rows):
    header = "subject,relation,object,source,status,confidence,note\n"
    return header + "".join(f"{row}\n" for row in rows)


def refs(results):
    return [f"{r['file']}:{r['line']}" for r in results]


class TestCoverageCredit:
    """The backing joins the coverage component — the same reading a lexical hit
    gets: one more of the question's words that this row answers."""

    def test_backing_lifts_a_cited_row_over_an_equally_covered_one(self, tmp_path):
        # The issue's case, minimized. Both files match the ASCII keyword and nothing
        # else, so before this change the Korean half of the question decided nothing
        # and the order was frequency then corpus order — a-plain first, on both
        # counts. One of them is ALSO where an accepted fact about the question's
        # Korean word came from, and that is invisible to the lexical scan by
        # construction: the word occurs in neither file.
        #
        # a-plain repeats the keyword so it WINS the frequency component. That is what
        # makes this test about coverage: a credit that only reached frequency leaves
        # b-backed at (1, 2) against (1, 3) and the order unchanged.
        root = build(
            tmp_path,
            {
                "a-plain": "A neurosymbolic method. A neurosymbolic pipeline, neurosymbolic throughout.",
                "b-backed": "A neurosymbolic method.",
            },
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,sources/b-backed.md,confirmed,0.9,"),
        )
        question = "neurosymbolic 해석가능성에서 어떤 이점을 갖는가"
        assert refs(ask_router.search(question, root, limit=None)) == [
            "sources/b-backed.md:3",
            "sources/a-plain.md:3",
        ]
        # No promoted row exists to carry that fact: b-backed was cited, so #576's
        # rule skips it. Without the credit the KB's evidence reaches nothing at all
        # for this question — which is the defect, not a side effect of it.
        assert all("via" not in r for r in ask_router.search(question, root, limit=None))

    def test_a_term_reached_both_ways_counts_once(self, tmp_path):
        # A Korean source can BOTH spell the question's word and be the source of an
        # accepted fact carrying it. Counting the term on each channel would rank
        # that row as answering more of the question than it does — here it would
        # beat a file that really does cover three of the question's words.
        #
        # a-backed sorts first, so a double count would also WIN the tie it creates:
        # coverage 3 either way, and its frequency (2 hits + 1 backing fact) equals
        # c-plain's 3 hits.
        root = build(
            tmp_path,
            {
                "a-backed": "설명가능성 benchmark 결과.",
                "c-plain": "설명가능성 benchmark alpha 결과.",
            },
            accepted='relation("s", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s,이점,설명가능성_향상,sources/a-backed.md,confirmed,0.9,"),
        )
        assert refs(ask_router.search("설명가능성 benchmark alpha", root, limit=None)) == [
            "sources/c-plain.md:3",
            "sources/a-backed.md:3",
        ]

    def test_an_nfd_question_reaches_the_bridge_in_neither_composition(self, tmp_path):
        # Why the union above can be a plain set union, and what would break it.
        #
        # The two sides come from ONE keyword list, but only one of them is folded:
        # _keyword_hits carries the question's surface term, _bridge_terms folds its
        # copy to NFC to join candidates.csv. An NFD-typed question would therefore
        # put two spellings of one word into the union — a silent over-credit that
        # no ranking assertion would look wrong.
        #
        # It cannot happen today, and this pins the reason rather than assuming it:
        # NFD Hangul decomposes to conjoining jamo, which _is_cjk does not accept, so
        # _bridge_terms drops the term before it can differ. Measured, not assumed —
        # writing the same question in NFC bridges the identical word. If a future
        # normalizer (#581) widens that filter, this test fails and the fold in
        # _keyword_hits becomes load-bearing instead of defensive.
        nfd = unicodedata.normalize("NFD", "설명가능성")
        assert ask_router._bridge_terms(f"{nfd} benchmark") == []
        assert ask_router._bridge_terms("설명가능성 benchmark") == ["설명가능성"]

    def test_a_converted_source_under_runs_is_credited_too(self, tmp_path):
        # runs/sources/ is the other half of the corpus (converted originals), and it
        # is where an extractor's provenance points for anything that was not markdown
        # to begin with. Crediting only sources/ would make the rank of a paper depend
        # on which format it arrived in.
        root = build(
            tmp_path,
            {"a-plain": "A neurosymbolic method. neurosymbolic, neurosymbolic."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,runs/sources/b-backed.txt,confirmed,0.9,"),
        )
        (root / "runs" / "sources").mkdir(parents=True)
        (root / "runs" / "sources" / "b-backed.txt").write_text(
            "A neurosymbolic method.\n", encoding="utf-8"
        )
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        assert refs(results) == ["runs/sources/b-backed.txt:1", "sources/a-plain.md:3"]

    def test_backing_facts_join_the_frequency_component(self, tmp_path):
        # Second component, same reading as #576 gave a promoted row: how much
        # evidence says this row answers those words. Two files cover the question
        # equally (one ASCII hit + the same bridged word); the one more accepted
        # facts were extracted from ranks first.
        root = build(
            tmp_path,
            {"a-thin": "A neurosymbolic method.", "b-thick": "A neurosymbolic method."},
            accepted=(
                'relation("s1", "이점", "해석가능성_향상").\n'
                'relation("s2", "핵심_기법", "해석가능성_분석").\n'
            ),
            candidates=csv_rows(
                "s1,이점,해석가능성_향상,sources/a-thin.md,confirmed,0.9,",
                "s1,이점,해석가능성_향상,sources/b-thick.md,confirmed,0.9,",
                "s2,핵심_기법,해석가능성_분석,sources/b-thick.md,confirmed,0.9,",
            ),
        )
        assert refs(ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)) == [
            "sources/b-thick.md:3",
            "sources/a-thin.md:3",
        ]

    def test_a_korean_filename_spelled_two_ways_still_joins(self, tmp_path):
        # The credit is looked up by PATH, and the two sides of that lookup come from
        # different places: the ref from the filesystem, the key from candidates.csv.
        # A Korean filename has two equal spellings (NFC/NFD) that compare unequal
        # character by character, so an unfolded lookup silently credits nothing —
        # no error, no empty result, just the old ranking. Every other reader of
        # candidates' `source` already folds (common.py, merge_candidates, cli), and
        # search() folds the same ref into its `cited` set for the same reason.
        nfd = unicodedata.normalize("NFD", "해석연구")
        root = build(
            tmp_path,
            {"a-plain": "A neurosymbolic method. neurosymbolic, neurosymbolic."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows(
                f"s,이점,해석가능성_향상,sources/{unicodedata.normalize('NFC', '해석연구')}.md,confirmed,0.9,"
            ),
        )
        (root / "sources" / f"{nfd}.md").write_text(
            SOURCE_MD.format(title="x", body="A neurosymbolic method."), encoding="utf-8"
        )
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        # Premise: the scan really did read the name back in the other composition.
        # If the filesystem normalized it away, this test proves nothing and says so.
        assert [r["file"] for r in results if nfd in r["file"]], [r["file"] for r in results]
        assert results[0]["file"] == f"sources/{nfd}.md"
        # ...and not by being promoted: it was cited lexically, like the other file.
        assert all("via" not in r for r in results)


class TestWhatTheCreditMayNotOverrule:
    """Two standing contracts the new component must not become a way around."""

    def test_a_supplementary_excerpt_still_ranks_below_every_primary_one(self, tmp_path):
        # #572: the directory grade is the top key. A decisions/ note named as a
        # provenance source would otherwise buy its way above source text — and it
        # is the ONE row type SKILL.md promises can never displace a source.
        root = build(
            tmp_path,
            {"a-plain": "A neurosymbolic method."},
            supplementary={"notes": "A neurosymbolic method."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,decisions/notes.md,confirmed,0.9,"),
        )
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        assert refs(results) == ["sources/a-plain.md:3", "decisions/notes.md:3"]
        assert results[0]["dir"] == "sources"

    def test_a_credited_row_is_never_labeled_a_bridged_one(self, tmp_path):
        # 수용 기준 2 of #576 defines the tag as "reached via KB vocabulary and NOT
        # by a lexical match". This row WAS matched lexically; tagging it would say
        # something false, and the reader would look for a keyword-free excerpt.
        root = build(
            tmp_path,
            {"b-backed": "A neurosymbolic method."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,sources/b-backed.md,confirmed,0.9,"),
        )
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        assert results and all("via" not in r for r in results)
        out = ask_router.render_wiki_answer("q", "unknown entity", results)
        assert ask_router.VIA_KB_VOCABULARY_TAG not in out
        assert "← accepted" not in out
        # And the block it renders in is still the unverified one: a higher rank is
        # not a verdict. No line STARTS a VERIFIED block ('UNVERIFIED' on line 1
        # contains the substring, so an unanchored check passes vacuously).
        assert out.startswith("UNVERIFIED — wiki exploration")
        assert not [line for line in out.splitlines() if line.startswith("VERIFIED")]

    def test_a_promoted_row_no_longer_outranks_the_source_that_holds_the_keyword(self, tmp_path):
        # Measured on the reference KB before this change, question '오메가-3 보충이
        # COPD 환자에게 효과있음을 보인 연구는?': the top FOUR rows of 151 were promoted
        # rows, above every excerpt that actually contains the reader's 'COPD' —
        # a promoted row's frequency is its backing fact count, which is unbounded,
        # while a lexical row's was its keyword count alone. Crediting the cited rows
        # puts the row that answers BOTH channels first; the promoted row keeps its
        # own key and stays reachable (it is still returned, just not first).
        root = build(
            tmp_path,
            {"a-cited": "A neurosymbolic method.", "z-uncited": "An unrelated method."},
            accepted=(
                'relation("s1", "이점", "해석가능성_향상").\n'
                'relation("s2", "핵심_기법", "해석가능성_분석").\n'
                'relation("s3", "다룬_주제", "해석가능성_평가").\n'
            ),
            candidates=csv_rows(
                "s1,이점,해석가능성_향상,sources/a-cited.md,confirmed,0.9,",
                "s1,이점,해석가능성_향상,sources/z-uncited.md,confirmed,0.9,",
                "s2,핵심_기법,해석가능성_분석,sources/z-uncited.md,confirmed,0.9,",
                "s3,다룬_주제,해석가능성_평가,sources/z-uncited.md,confirmed,0.9,",
            ),
        )
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        assert refs(results) == ["sources/a-cited.md:3", "sources/z-uncited.md:1"]
        assert "via" not in results[0] and results[1]["via"]["terms"] == ["해석가능성에서"]


class TestWhatIsLeftAlone:
    """The credit is additive: a KB that cannot produce it must be unaffected."""

    def test_a_kb_without_fact_files_ranks_exactly_as_before(self, tmp_path):
        # The degrade path (#576 수용 기준 5), now also for the cited rows. Corpus
        # order, no reordering, no new keys.
        root = build(tmp_path, {"a-plain": "A neurosymbolic method.", "b-plain": "A neurosymbolic method."})
        results = ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)
        assert refs(results) == ["sources/a-plain.md:3", "sources/b-plain.md:3"]
        assert all("via" not in r for r in results)

    def test_a_non_engine_status_backs_nothing(self, tmp_path):
        # Same filter the promoted path applies (ENGINE_STATUSES). A superseded row
        # must not raise a source's rank either — quietly re-ordering an answer
        # around retired evidence is the same defect as quoting it.
        root = build(
            tmp_path,
            {"a-plain": "A neurosymbolic method.", "b-super": "A neurosymbolic method."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,sources/b-super.md,superseded,0.9,"),
        )
        assert refs(ask_router.search("neurosymbolic 해석가능성에서", root, limit=None)) == [
            "sources/a-plain.md:3",
            "sources/b-super.md:3",
        ]

    def test_the_recall_tally_still_reports_the_bridged_term_as_unmatched(self, tmp_path):
        # #575 reports which of the question's keywords the CORPUS contains. The
        # credit is a ranking signal, not a claim about the text: '해석가능성에서'
        # occurs in no file here, and a tally that said otherwise would promise
        # wording the sources do not use.
        root = build(
            tmp_path,
            {"b-backed": "A neurosymbolic method."},
            accepted='relation("s", "이점", "해석가능성_향상").\n',
            candidates=csv_rows("s,이점,해석가능성_향상,sources/b-backed.md,confirmed,0.9,"),
        )
        recall: dict[str, list[str]] = {}
        ask_router.search("neurosymbolic 해석가능성에서", root, limit=None, recall=recall)
        assert recall == {"matched": ["neurosymbolic"], "unmatched": ["해석가능성에서"]}
