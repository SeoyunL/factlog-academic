# Spike: title+author+year fallback precision (#74)

*Measurement only. No import path changed. Regenerate with `python3 tools/spike_fallback_precision.py --write-report`.*

## Corpus

- Queries (fixed): 10 `cat:<subfield> AND jr:<venue>` searches, page size 60, sort `submitted` descending:
  - `cat:cs.CL AND jr:ACL`
  - `cat:cs.CL AND jr:EMNLP`
  - `cat:cs.CL AND jr:NAACL`
  - `cat:cs.CL AND jr:TACL`
  - `cat:cs.CL AND jr:COLING`
  - `cat:cs.LG AND jr:NeurIPS`
  - `cat:cs.LG AND jr:ICML`
  - `cat:cs.LG AND jr:ICLR`
  - `cat:cs.CV AND jr:CVPR`
  - `cat:cs.AI AND jr:AAAI`

- arXiv only searches nine fields, so we cannot filter on "has a DOI" directly; `jr:` (journal reference) is the proxy for a published paper, and published papers are the ones with DOIs.
- The queries surface 113 DOI-carrying arXiv papers (deduplicated by arXiv base id). **Sample size: 97** of them were resolved against OpenAlex within one day's ~1000-credit budget (10 credits per title search); the committed cache pins exactly which, so replay is deterministic.
- Each resolved paper supplies a DOI-true OpenAlex pairing as ground truth.
- Ground truth retrievable (the DOI-true record appeared in the title search results): **88/97**. Recall is computed over these.
- Candidate pool per paper: OpenAlex `search=<title>` top 25.
- Cost: OpenAlex title search = 10 credits/paper, DOI lookup = 0.

**Why this size means something, and its bias.** A precision estimate on the fired matches has a standard error near sqrt(p(1-p)/n); at these counts a false-merge rate above a few percent is distinguishable from zero, which is what #75/#76 need — the question is whether false merges happen at all, not their third decimal. The sample is biased *toward* the matcher: DOI-carrying papers are published work with clean, canonical metadata, whereas #57 shows the fallback would mostly run on fresh preprints with none. Confusion found here is a floor.

## Precision / recall

The swept score is normalized-title token Jaccard. First-author surname agreement and year agreement are required conjuncts (a match must clear all three). A *false merge* (FP) is the matcher firing on a candidate whose DOI differs from the paper's own. `ambiguous` counts papers where two or more distinct works clear every gate at that threshold.

### Year tolerance ±1 (preprint vs publication year)

| title threshold | fired | TP | FP (false merge) | FN | precision | recall | ambiguous |
|---|---|---|---|---|---|---|---|
| 0.50 | 87 | 73 | 14 | 3 | 0.839 | 0.961 | 35 |
| 0.60 | 87 | 73 | 14 | 3 | 0.839 | 0.961 | 35 |
| 0.70 | 86 | 72 | 14 | 4 | 0.837 | 0.947 | 35 |
| 0.80 | 86 | 72 | 14 | 4 | 0.837 | 0.947 | 35 |
| 0.90 | 86 | 72 | 14 | 4 | 0.837 | 0.947 | 35 |
| 1.00 | 86 | 72 | 14 | 4 | 0.837 | 0.947 | 35 |

### Year must match exactly

| title threshold | fired | TP | FP (false merge) | FN | precision | recall | ambiguous |
|---|---|---|---|---|---|---|---|
| 0.50 | 74 | 60 | 14 | 15 | 0.811 | 0.800 | 30 |
| 0.60 | 74 | 60 | 14 | 15 | 0.811 | 0.800 | 30 |
| 0.70 | 73 | 59 | 14 | 16 | 0.808 | 0.787 | 30 |
| 0.80 | 73 | 59 | 14 | 16 | 0.808 | 0.787 | 30 |
| 0.90 | 73 | 59 | 14 | 16 | 0.808 | 0.787 | 30 |
| 1.00 | 73 | 59 | 14 | 16 | 0.808 | 0.787 | 30 |

## Confusion cases (named individually)

At title threshold 0.80, year tolerance ±1, the matcher fired on the wrong DOI for **14 of 97** papers. Every one shares the arXiv paper's first author, an adjacent year, and — at threshold 1.00 — a byte-identical title with the record it was merged into; only the DOI differs. Separately, in **35 of 97** papers two or more *distinct* records cleared every gate at once: the matcher had no signal to prefer the DOI-true one over an equally-scoring decoy.

What the wrong record actually was:
- 11× — arXiv-preprint mirror (same work, distinct OpenAlex record)
- 2× — distinct non-arXiv DOI (a different source record; may be a different work)
- 1× — no-DOI record (same title, source unidentifiable)

The distinction matters and the matcher cannot draw it: an arXiv-preprint mirror is arguably the *same* work under a second OpenAlex record, while a distinct non-arXiv DOI (a conference paper vs a medRxiv posting of the same title, a dataset registered twice) may be a genuinely different source. Title+author+year is identical across both, so nothing in the matcher's inputs separates the benign duplicate from the harmful one — the human gate is the only thing that can (#75, #76).

Named individually — arXiv paper on the left, the different-DOI OpenAlex record it was merged into on the right:

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2404.01822 (2024) — 'A (More) Realistic Evaluation Setup for Generalisation of Community Models on Malicious Content Detection'
    - first author: 'Ivo Verhoeven'; DOI `10.18653/v1/2024.findings-naacl.30`
  - OpenAlex:W4393931081 (2024) — 'A (More) Realistic Evaluation Setup for Generalisation of Community Models on Malicious Content Detection'
    - first author: 'Ivo Verhoeven'; DOI `10.48550/arxiv.2404.01822`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2404.08189 (2024) — 'Reducing hallucination in structured outputs via Retrieval-Augmented Generation'
    - first author: 'Patrice Béchard'; DOI `10.18653/v1/2024.naacl-industry.19`
  - OpenAlex:W4394838812 (2024) — 'Reducing hallucination in structured outputs via Retrieval-Augmented Generation'
    - first author: 'Patrice Béchard'; DOI `10.48550/arxiv.2404.08189`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2406.09043 (2024) — 'Language Models are Crossword Solvers'
    - first author: 'Soumadeep Saha'; DOI `10.18653/v1/2025.naacl-long.104`
  - OpenAlex:W4399695551 (2024) — 'Language Models are Crossword Solvers'
    - first author: 'Soumadeep Saha'; DOI `10.48550/arxiv.2406.09043`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2410.14235 (2024) — 'Towards Robust Knowledge Representations in Multilingual LLMs for Equivalence and Inheritance based Consistent Reasoning'
    - first author: 'Gaurav Arora'; DOI `10.18653/v1/2025.naacl-long.394`
  - OpenAlex:W4403995631 (2024) — 'Towards Robust Knowledge Representations in Multilingual LLMs for Equivalence and Inheritance based Consistent Reasoning'
    - first author: 'Gaurav Arora'; DOI `10.48550/arxiv.2410.14235`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2411.15927 (2024) — 'Generative Prompt Internalization'
    - first author: 'Haebin Shin'; DOI `10.18653/v1/2025.naacl-long.376`
  - OpenAlex:W4404987031 (2024) — 'Generative Prompt Internalization'
    - first author: 'Haebin Shin'; DOI `10.48550/arxiv.2411.15927`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2502.06394 (2025) — 'SynthDetoxM: Modern LLMs are Few-Shot Parallel Detoxification Data Annotators'
    - first author: 'Daniil Moskovskiy'; DOI `10.18653/v1/2025.naacl-long.294`
  - OpenAlex:W4407359015 (2025) — 'SynthDetoxM: Modern LLMs are Few-Shot Parallel Detoxification Data Annotators'
    - first author: 'Daniil Moskovskiy'; DOI `10.48550/arxiv.2502.06394`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2503.16457 (2025) — 'Integrating Personality into Digital Humans: A Review of LLM-Driven Approaches for Virtual Reality'
    - first author: 'Iago Alves Brito'; DOI `10.18653/v1/2025.findings-emnlp.506`
  - OpenAlex:W4417149532 (2025) — 'Integrating Personality into Digital Humans: A Review of LLM-Driven Approaches for Virtual Reality'
    - first author: 'Iago Alves Brito'; DOI `10.48550/arxiv.2503.16457`

- **title Jaccard 1.00** — _distinct non-arXiv DOI (a different source record; may be a different work)_
  - arXiv:2506.18120 (2025) — 'The Syntactic Acceptability Dataset (Preview): A Resource for Machine Learning and Linguistic Analysis of English'
    - first author: 'Tom S Juzek'; DOI `10.17605/OSF.IO/5E8KZ`
  - OpenAlex:W7131853804 (2024) — 'The Syntactic Acceptability Dataset (Preview): A Resource for Machine Learning and Linguistic Analysis of English'
    - first author: 'Thomas Stephan Juzek'; DOI `10.63317/3f2yamzokm9i`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2508.21589 (2025) — 'Middo: Model-Informed Dynamic Data Optimization for Enhanced LLM Fine-Tuning via Closed-Loop Learning'
    - first author: 'Zinan Tang'; DOI `10.18653/v1/2025.emnlp-main.350`
  - OpenAlex:W4414691774 (2025) — 'Middo: Model-Informed Dynamic Data Optimization for Enhanced LLM Fine-Tuning via Closed-Loop Learning'
    - first author: 'Tang, Zinan'; DOI `10.48550/arxiv.2508.21589`

- **title Jaccard 1.00** — _distinct non-arXiv DOI (a different source record; may be a different work)_
  - arXiv:2509.00891 (2025) — 'ChatCLIDS: Simulating Persuasive AI Dialogues to Promote Closed-Loop Insulin Adoption in Type 1 Diabetes Care'
    - first author: 'Zonghai Yao'; DOI `10.1609/aaai.v40i46.41305`
  - OpenAlex:W4413974202 (2025) — 'ChatCLIDS: Simulating Persuasive AI Dialogues to Promote Closed-Loop Insulin Adoption in Type 1 Diabetes Care'
    - first author: 'Zonghai Yao'; DOI `10.1101/2025.09.02.25334973`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2509.01058 (2025) — 'Speaking at the Right Level: Literacy-Controlled Counterspeech Generation with RAG-RL'
    - first author: 'Xiaoying Song'; DOI `10.18653/v1/2025.findings-emnlp.153`
  - OpenAlex:W4414747030 (2025) — 'Speaking at the Right Level: Literacy-Controlled Counterspeech Generation with RAG-RL'
    - first author: 'Xiaofei Song'; DOI `10.48550/arxiv.2509.01058`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2509.17459 (2025) — 'PRINCIPLES: Synthetic Strategy Memory for Proactive Dialogue Agents'
    - first author: 'Namyoung Kim'; DOI `10.18653/v1/2025.findings-emnlp.1164`
  - OpenAlex:W4415254635 (2025) — 'PRINCIPLES: Synthetic Strategy Memory for Proactive Dialogue Agents'
    - first author: 'Nam‐Young Kim'; DOI `10.48550/arxiv.2509.17459`

- **title Jaccard 1.00** — _arXiv-preprint mirror (same work, distinct OpenAlex record)_
  - arXiv:2509.20810 (2025) — 'Enrich-on-Graph: Query-Graph Alignment for Complex Reasoning with LLM Enriching'
    - first author: 'Songze Li'; DOI `10.18653/v1/2025.emnlp-main.390`
  - OpenAlex:W4414789398 (2025) — 'Enrich-on-Graph: Query-Graph Alignment for Complex Reasoning with LLM Enriching'
    - first author: 'Songze Li'; DOI `10.48550/arxiv.2509.20810`

- **title Jaccard 1.00** — _no-DOI record (same title, source unidentifiable)_
  - arXiv:2511.07204 (2025) — 'Evaluating Online Moderation Via LLM-Powered Counterfactual Simulations'
    - first author: 'Giacomo Fidone'; DOI `10.1609/aaai.v40i45.41186`
  - OpenAlex:W7105507688 (2025) — 'Evaluating Online Moderation Via LLM-Powered Counterfactual Simulations'
    - first author: 'Fidone, Giacomo'; DOI `None`

## Behaviour on the required edge cases

- **large author list (21 authors, arXiv:2502.16923)**: the matcher reads only the first author ('Kiran Ramnath' -> surname 'ramnath'); list length is irrelevant, so a 100+ author collaboration neither helps nor breaks it.
- **150-author synthetic list**: first_surname -> 'family'; unaffected by the other 149.
- **`et al.` truncation**: arXiv marks a truncated list with a sentinel author. surname('et al.') is blanked to '' (was 'al' without the guard), so a truncated first author yields no surname and the gate fails closed rather than matching on the word 'al'. But if the *real* first author is present and only later ones are dropped, truncation is invisible to a first-author matcher — it cannot tell a 3-author paper from its 200-author twin.
- **non-ASCII author name (real sample)**: 'Patrice Béchard' -> surname 'bechard' (NFKD fold drops the accent, so it agrees with an ASCII spelling).
- **non-ASCII probe 'François Fleuret'**: surname -> 'fleuret'
- **non-ASCII probe 'Kyunghyun Cho'**: surname -> 'cho'
- **non-ASCII probe 'Ming‐Wei Chang'**: surname -> 'chang'
- **`Family, Given` vs `Given Family`: 'Vaswani, Ashish' / 'Ashish Vaswani'**: surnames 'vaswani' / 'vaswani' -> agree
- **`Family, Given` vs `Given Family`: 'Faronius, Håkan Karlsson' / 'Håkan Karlsson Faronius'**: surnames 'faronius' / 'faronius' -> agree
- **`Family, Given` vs `Given Family`: 'van der Berg, Jan' / 'Jan van der Berg'**: surnames 'vanderberg' / 'berg' -> DISAGREE
- **LaTeX title 'Sorting in $O(n\\log n)$ Time'**: normalized -> 'sorting in time'
- **LaTeX title 'A $\\mathcal{O}(1)$ Data Structure'**: normalized -> 'a data structure'
- **subtitle after a colon**: title_similarity('BERT', full) = 0.10 (main-title-only Jaccard = 1.00); a record that stored only the acronym scores far below any usable threshold against the full title.

## Reading of the numbers

- **Precision is flat at ~0.84 across every title threshold from 0.50 to 1.00.** The score does not separate right from wrong matches, because the wrong ones have title Jaccard 1.00 — a byte-identical title. There is no knee in the curve to tune to; a stricter threshold buys nothing and only costs recall.
- Of 14 false merges at threshold 0.80, 12 point at the *same paper* under a second OpenAlex record (chiefly the arXiv-preprint DOI `10.48550/arxiv.*`, which OpenAlex keeps separate from the published record the arXiv metadata names). Merging those still attaches the wrong source record — a different DOI, a different peer-review status — to the paper. The remaining 2 point at a genuinely distinct source: an AAAI paper vs a medRxiv posting of the same title (arXiv:2509.00891), a dataset registered under two DOIs (arXiv:2506.18120).
- In 35 of 97 papers, two or more distinct records cleared every gate simultaneously. Even when the matcher happened to pick the DOI-true record, it did so with no signal distinguishing it from an equally-scoring decoy — the successes are as blind as the failures.

**Recommendation.** No title+author+year threshold is safe to auto-merge on. The failures are not low-similarity near-misses that a higher bar would exclude; they are exact-title, same-author, same-year collisions between distinct source records, which is precisely the case #76 says no threshold saves. Title, author and year are identical across the benign duplicate and the harmful one, so nothing the matcher can see separates them. Implement the fallback as #75 specifies — surface a candidate for the human gate, never merge, and remember a rejected pairing so it is not re-proposed. The measured floor for #75's threshold constant: precision ~0.84 on published papers with clean metadata (a friendly upper-bound population), with the residual error being a category the matcher is structurally unable to resolve.

**What this spike could not measure.** (1) The real target population — fresh preprints without DOIs (#57) — has no DOI ground truth, so its false-merge rate is unmeasured and can only be worse than this published-paper floor. (2) OpenAlex title search is the candidate generator; a different generator (filtered search, fuzzy title) would change recall and the decoy pool. (3) Precision here counts a wrong DOI as a false merge; whether merging an arXiv-preprint record into its published twin is *harmful* to a specific KB depends on what was already imported — a question for #75's flow, not this measurement. (4) One day's OpenAlex budget capped the resolved sample at 97/113; the 9 papers whose DOI-true record did not surface in the title search (recall denominator 88) are a retrieval limit, not a matcher limit.
