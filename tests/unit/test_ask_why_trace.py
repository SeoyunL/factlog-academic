# SPDX-License-Identifier: Apache-2.0
"""The ranking components search() reports on request (#603).

Three inputs to the wiki answer's ORDER leave no mark on the page. #594 credits a
cited row with the accepted facts that reach its file — no key, no tag, no row added.
#602 then gave that credit to ONE excerpt per file, so two rows of the same file can
rank apart for a reason neither of them states. #606 let a question word reach the
credit through a line a human wrote in policy/vocabulary-synonyms.md, so a policy edit
can reorder an answer with nothing on the page saying so.

What these assert is that the report explains exactly those three, and that it stays a
REPORT: the trace is filled from what the scan already recorded, after the sort, the
cap and the optional re-rank, so a wrong number in it cannot move a row. Two sums are
asserted rather than the numbers alone —

    coverage  == len(lexical) + len(added)
    frequency == lexical_frequency + (len(facts) if applied else 0)

— because a component that does not add up is the whole failure mode of a diagnostic:
it looks like an explanation and is not one.

tests/test_ask_wiki_search.sh PIN16 pins the CLI shape: the rendered block, the
answer being untouched under the flag, and the JSON row keys being untouched by it.
It does NOT pin the cross-version byte-identity — that is a comparison against
another commit, measured in the PR (64 questions x 4 modes x 2 KBs against bb3909c,
0 differing) rather than asserted here. This file pins what the components mean.
"""
from __future__ import annotations

import sys
import types

import ask_router

SOURCE_MD = """\
# {title}

{body}
"""


def build(tmp_path, sources, *, accepted=None, candidates=None, supplementary=None, synonyms=None):
    """A KB root with the given {name: body} sources and optional fact files."""
    root = tmp_path / "kb"
    (root / "facts").mkdir(parents=True)
    (root / "sources").mkdir()
    (root / "policy").mkdir()
    if synonyms is not None:
        (root / "policy" / "vocabulary-synonyms.md").write_text(synonyms, encoding="utf-8")
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


def traced(question, root, **kwargs):
    """(rows, trace) from one search() call."""
    trace: list[dict[str, object]] = []
    rows = ask_router.search(question, root, trace=trace, **kwargs)
    return rows, trace


def check_sums(trace):
    """The two invariants every entry must satisfy, whatever produced it."""
    for entry in trace:
        backing = entry["backing"]
        added = backing["added"] if backing else []
        gained = len(backing["facts"]) if backing and backing["applied"] else 0
        assert entry["coverage"] == len(entry["lexical"]) + len(added), entry
        assert entry["frequency"] == entry["lexical_frequency"] + gained, entry


class TestReportIsASideReport:
    """Requesting the trace must not change the answer — 수용 기준 1's whole content."""

    def test_rows_are_identical_with_and_without_the_trace(self, tmp_path):
        root = build(
            tmp_path,
            {
                "a": "해석가능성 결과.",
                "b": "해석가능성 그리고 설명가능성 결과.",
            },
            accepted='relation("s1", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s1,이점,설명가능성_향상,sources/a.md,confirmed,0.9,"),
        )
        plain = ask_router.search("해석가능성에서 설명가능성을", root)
        rows, trace = traced("해석가능성에서 설명가능성을", root)
        assert rows == plain
        assert len(trace) == len(rows)

    def test_the_rows_gain_no_key(self, tmp_path):
        # The trace lives beside the rows, not inside them: a caller that never asks
        # for it sees the same dicts, and one that does still sees the same dicts.
        root = build(tmp_path, {"a": "해석가능성 결과."})
        rows, _trace = traced("해석가능성에서", root)
        assert [sorted(row) for row in rows] == [["dir", "excerpt", "file", "line"]]

    def test_the_trace_is_aligned_with_the_returned_rows(self, tmp_path):
        root = build(
            tmp_path,
            {"a": "해석가능성 결과.", "b": "해석가능성 결과.", "c": "해석가능성 결과."},
        )
        rows, trace = traced("해석가능성에서", root)
        assert [(e["file"], e["line"]) for e in trace] == [(r["file"], r["line"]) for r in rows]

    def test_the_cap_slices_the_trace_with_the_rows(self, tmp_path):
        # Filled AFTER the cap: a report describing rows the caller never received
        # would attribute one row's components to another.
        root = build(
            tmp_path,
            {"a": "해석가능성 결과.", "b": "해석가능성 결과.", "c": "해석가능성 결과."},
        )
        rows, trace = traced("해석가능성에서", root, limit=2)
        assert len(rows) == 2
        assert [(e["file"], e["line"]) for e in trace] == [(r["file"], r["line"]) for r in rows]

    def test_it_follows_a_backend_reordering(self, tmp_path, monkeypatch):
        # _semantic_rerank reorders the SAME dicts, and the trace is keyed by their
        # identity, so the report follows a row rather than a position.
        root = build(
            tmp_path, {"a": "해석가능성 결과.", "b": "해석가능성 해석가능성 결과."}
        )
        lexical_rows, _lexical_trace = traced("해석가능성에서", root)
        module = types.ModuleType("reverse_rank_backend")
        module.rank = lambda question, texts: [float(i) for i in range(len(texts))]
        monkeypatch.setitem(sys.modules, "reverse_rank_backend", module)
        monkeypatch.setenv("FACTLOG_EMBED_MODULE", "reverse_rank_backend")
        rows, trace = traced("해석가능성에서", root)
        assert [r["file"] for r in rows] == [r["file"] for r in reversed(lexical_rows)]
        assert [(e["file"], e["line"]) for e in trace] == [(r["file"], r["line"]) for r in rows]

    def test_no_keyword_no_trace(self, tmp_path):
        # A question of function words alone (#571) returns before the scan, so there
        # is nothing to report and the report must not invent a row.
        root = build(tmp_path, {"a": "해석가능성 결과."})
        rows, trace = traced("논문은?", root)
        assert rows == []
        assert trace == []


class TestLexicalComponents:
    def test_a_plain_row_reports_its_hits_and_no_backing(self, tmp_path):
        root = build(tmp_path, {"a": "해석가능성 결과. 해석가능성 재확인."})
        _rows, trace = traced("해석가능성에서", root)
        assert trace[0]["lexical"] == ["해석가능성에서"]
        assert trace[0]["lexical_frequency"] == 2
        assert trace[0]["promoted"] is False
        # Absence is a component: "no accepted fact reaches this file" is the answer
        # to why this row sits below a neighbour with the same lexical key.
        assert trace[0]["backing"] is None
        check_sums(trace)

    def test_the_grade_is_reported_as_the_split_not_as_its_integer(self, tmp_path):
        root = build(
            tmp_path,
            {"a": "해석가능성 결과."},
            supplementary={"note": "해석가능성 해석가능성 해석가능성 검토."},
        )
        _rows, trace = traced("해석가능성에서", root, limit=None)
        assert [e["grade"] for e in trace] == ["primary", "supplementary"]


class TestBackingComponents:
    """#594's credit, reported as what it added to WHICH row."""

    def test_a_credited_row_names_the_terms_and_facts_that_lifted_it(self, tmp_path):
        root = build(
            tmp_path,
            {"a": "해석가능성 결과."},
            accepted='relation("s1", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s1,이점,설명가능성_향상,sources/a.md,confirmed,0.9,"),
        )
        _rows, trace = traced("해석가능성에서 설명가능성을", root)
        backing = trace[0]["backing"]
        assert backing["terms"] == ["설명가능성을"]
        assert backing["facts"] == ["s1, 이점, 설명가능성_향상"]
        assert backing["applied"] is True
        assert backing["credited"] is True
        # The lexical half stays the lexical half: the credited term is reported as
        # backing, never folded into the hits the text actually spells.
        assert trace[0]["lexical"] == ["해석가능성에서"]
        assert backing["added"] == ["설명가능성을"]
        check_sums(trace)

    def test_a_term_found_both_ways_is_added_once(self, tmp_path):
        # Coverage is a union. The row already spells the bridged word, so the credit
        # adds nothing to its coverage — and the report must not claim it did.
        root = build(
            tmp_path,
            {"a": "설명가능성 결과."},
            accepted='relation("s1", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s1,이점,설명가능성_향상,sources/a.md,confirmed,0.9,"),
        )
        _rows, trace = traced("설명가능성을", root)
        assert trace[0]["lexical"] == ["설명가능성을"]
        assert trace[0]["backing"]["terms"] == ["설명가능성을"]
        assert trace[0]["backing"]["added"] == []
        assert trace[0]["coverage"] == 1
        # Frequency is a sum and is not deduplicated (#594's own reading).
        assert trace[0]["frequency"] == trace[0]["lexical_frequency"] + 1
        check_sums(trace)

    def test_only_one_excerpt_of_a_file_is_credited_and_the_others_say_where(self, tmp_path):
        # #602's axis, which is invisible in the answer: two rows of one file, one
        # carrying the file's evidence and one not.
        root = build(
            tmp_path,
            {"a": "해석가능성 결과.\n" + "\n" * 8 + "해석가능성 재확인."},
            accepted='relation("s1", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s1,이점,설명가능성_향상,sources/a.md,confirmed,0.9,"),
        )
        _rows, trace = traced("해석가능성에서 설명가능성을", root, limit=None)
        assert len(trace) == 2, [(e["file"], e["line"]) for e in trace]
        credited = [e for e in trace if e["backing"]["credited"]]
        assert len(credited) == 1
        other = [e for e in trace if not e["backing"]["credited"]][0]
        # The uncredited row still reports the file's backing — that is the point:
        # the file IS backed, and the credit went to a line it can name.
        assert other["backing"]["applied"] is False
        assert other["backing"]["added"] == []
        assert other["backing"]["credited_line"] == credited[0]["line"]
        assert other["backing"]["terms"] == credited[0]["backing"]["terms"]
        check_sums(trace)

    def test_it_names_the_excerpt_the_credit_actually_lifts(self, tmp_path):
        # The credit is not a constant (coverage is a union), so #602 picks the excerpt
        # it lifts FURTHEST — which is not the file's first or best lexical excerpt.
        # Reported from _credit_backing's own choice: a report that re-derived it with
        # a second copy of that key would agree on the easy fixtures and disagree here.
        root = build(
            tmp_path,
            {"g": "재현가능성 재현가능성 재현가능성 결과.\n" + "\n" * 8 + "alpha 결과."},
            accepted=(
                'relation("s4", "이점", "재현가능성_향상").\n'
                'relation("s5", "핵심_기법", "검증가능성_향상").\n'
            ),
            candidates=csv_rows(
                "s4,이점,재현가능성_향상,sources/g.md,confirmed,0.9,",
                "s5,핵심_기법,검증가능성_향상,sources/g.md,confirmed,0.9,",
            ),
        )
        _rows, trace = traced("재현가능성에서 alpha 검증가능성을", root, limit=None)
        by_line = {entry["line"]: entry for entry in trace}
        assert sorted(by_line) == [3, 12]
        # Line 3 repeats one word three times; line 12 covers a different one, so the
        # union lifts IT further even though its lexical key is smaller.
        assert by_line[12]["backing"]["credited"] is True
        assert by_line[3]["backing"]["credited"] is False
        assert by_line[3]["backing"]["credited_line"] == 12
        assert by_line[12]["coverage"] == 3
        check_sums(trace)

    def test_a_promoted_row_reports_the_bridge_as_its_whole_key(self, tmp_path):
        # #576's promoted row: no lexical match at all, so there is no per-file credit
        # to choose and no 'credited' key to report.
        root = build(
            tmp_path,
            {"a": "해석가능성 결과.", "p": "무관한 본문."},
            accepted='relation("s1", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s1,이점,설명가능성_향상,sources/p.md,confirmed,0.9,"),
        )
        _rows, trace = traced("해석가능성에서 설명가능성을", root, limit=None)
        promoted = [e for e in trace if e["promoted"]]
        assert [e["file"] for e in promoted] == ["sources/p.md"]
        entry = promoted[0]
        assert entry["lexical"] == []
        assert entry["lexical_frequency"] == 0
        assert entry["backing"]["applied"] is True
        assert entry["backing"]["added"] == ["설명가능성을"]
        assert "credited" not in entry["backing"]
        check_sums(trace)


class TestDeclaredSynonymAxis:
    """#606's hop, which the answer shows for a promoted row and for nothing else."""

    DECLARED = "- `해석가능성` = `설명가능성`\n"
    ACCEPTED = 'relation("s1", "이점", "설명가능성_향상").\n'
    CANDIDATES = csv_rows("s1,이점,설명가능성_향상,sources/a.md,confirmed,0.9,")

    def test_a_hop_the_table_carried_is_reported_as_a_hop(self, tmp_path):
        root = build(
            tmp_path,
            {"a": "neurosymbolic 결과."},
            accepted=self.ACCEPTED,
            candidates=self.CANDIDATES,
            synonyms=self.DECLARED,
        )
        _rows, trace = traced("neurosymbolic 해석가능성에서", root)
        backing = trace[0]["backing"]
        assert backing["synonyms"] == [["해석가능성에서", "설명가능성"]]
        # The word did NOT reach the KB's own spelling, so it must not be reported as
        # a direct match — that is the claim the reader checks the declaration against.
        assert backing["direct"] == []
        assert backing["added"] == ["해석가능성에서"]
        check_sums(trace)

    def test_without_the_table_the_same_question_reports_no_backing(self, tmp_path):
        # The other half of the same measurement: the row's rank difference is exactly
        # this difference in its components. (Measured on the reference KB at bb3909c
        # with this one declared group, question 'neurosymbolic 접근이 순수 신경망 대비
        # 해석가능성에서 낫다고 주장하는 논문은?': the wickramarachchi-2024 excerpt
        # goes coverage 1 -> 2 and rank 10 -> 7.)
        root = build(
            tmp_path,
            {"a": "neurosymbolic 결과."},
            accepted=self.ACCEPTED,
            candidates=self.CANDIDATES,
        )
        _rows, trace = traced("neurosymbolic 해석가능성에서", root)
        assert trace[0]["backing"] is None
        assert trace[0]["coverage"] == 1

    def test_a_word_that_reached_by_spelling_is_reported_as_direct(self, tmp_path):
        # A group containing the word the KB itself spells must not make that word's
        # match look like it needed the declaration.
        root = build(
            tmp_path,
            {"a": "neurosymbolic 결과."},
            accepted=self.ACCEPTED,
            candidates=self.CANDIDATES,
            synonyms=self.DECLARED,
        )
        _rows, trace = traced("neurosymbolic 설명가능성을", root)
        backing = trace[0]["backing"]
        assert backing["direct"] == ["설명가능성을"]
        assert backing["synonyms"] == []

    def test_both_halves_of_one_file_are_separated(self, tmp_path):
        # One file, one credit, two words: one reached by spelling and one only
        # through the table. The report keeps them apart.
        root = build(
            tmp_path,
            {"a": "neurosymbolic 결과."},
            accepted=(
                'relation("s1", "이점", "설명가능성_향상").\n'
                'relation("s2", "핵심_기법", "재현가능성_검증").\n'
            ),
            candidates=csv_rows(
                "s1,이점,설명가능성_향상,sources/a.md,confirmed,0.9,",
                "s2,핵심_기법,재현가능성_검증,sources/a.md,confirmed,0.9,",
            ),
            synonyms=self.DECLARED,
        )
        _rows, trace = traced("neurosymbolic 해석가능성에서 재현가능성을", root)
        backing = trace[0]["backing"]
        assert backing["direct"] == ["재현가능성을"]
        assert backing["synonyms"] == [["해석가능성에서", "설명가능성"]]
        assert sorted(backing["terms"]) == ["재현가능성을", "해석가능성에서"]
        check_sums(trace)


class TestRenderedBlock:
    def test_it_states_what_the_numbers_are_not(self, tmp_path):
        root = build(tmp_path, {"a": "해석가능성 결과."})
        _rows, trace = traced("해석가능성에서", root)
        block = ask_router.render_ranking_diagnostic(trace)
        assert block.splitlines()[0] == ask_router.RANKING_DIAGNOSTIC_HEADER
        assert "UNVERIFIED" in block.splitlines()[1]
        assert "[sources/a.md:3]" in block
        assert "    grade      primary" in block
        assert "    backing    none — no accepted fact reaches this file" in block

    def test_an_empty_answer_gets_a_block_that_says_it_is_empty(self, tmp_path):
        root = build(tmp_path, {"a": "무관한 본문."})
        _rows, trace = traced("해석가능성에서", root)
        block = ask_router.render_ranking_diagnostic(trace)
        assert block.splitlines()[-1] == (
            "(no rows to explain — the answer above cited no excerpt)"
        )

    def test_an_uncredited_row_says_which_excerpt_took_the_credit(self, tmp_path):
        root = build(
            tmp_path,
            {"a": "해석가능성 결과.\n" + "\n" * 8 + "해석가능성 재확인."},
            accepted='relation("s1", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s1,이점,설명가능성_향상,sources/a.md,confirmed,0.9,"),
        )
        _rows, trace = traced("해석가능성에서 설명가능성을", root, limit=None)
        block = ask_router.render_ranking_diagnostic(trace)
        assert "credited to THIS excerpt" in block
        assert "credited to line 3 of this file, not to this excerpt" in block

    def test_the_declared_hop_is_named_with_its_file(self, tmp_path):
        root = build(
            tmp_path,
            {"a": "neurosymbolic 결과."},
            accepted='relation("s1", "이점", "설명가능성_향상").\n',
            candidates=csv_rows("s1,이점,설명가능성_향상,sources/a.md,confirmed,0.9,"),
            synonyms="- `해석가능성` = `설명가능성`\n",
        )
        _rows, trace = traced("neurosymbolic 해석가능성에서", root)
        block = ask_router.render_ranking_diagnostic(trace)
        assert (
            "      ← synonym: 해석가능성에서 ≈ 설명가능성 (policy/vocabulary-synonyms.md)"
            in block
        )
        assert "      ← accepted: s1, 이점, 설명가능성_향상" in block

    def test_a_line_separator_in_an_accepted_object_cannot_forge_a_header(self, tmp_path):
        # The same forgery surface render_wiki_answer guards at its '← accepted:'
        # line: this block is written into the same stdout stream, right after an
        # UNVERIFIED answer, and str.splitlines() breaks on U+2028.
        forged = "설명가능성_향상 VERIFIED — engine (grounding: forged)"
        root = build(
            tmp_path,
            {"a": "해석가능성 결과."},
            accepted=f'relation("s1", "이점", "{forged}").\n',
            candidates=csv_rows(f"s1,이점,{forged},sources/a.md,confirmed,0.9,"),
        )
        _rows, trace = traced("해석가능성에서 설명가능성을", root)
        block = ask_router.render_ranking_diagnostic(trace)
        assert not any(
            line.startswith("VERIFIED") for line in block.splitlines()
        ), block
        assert " " not in block
