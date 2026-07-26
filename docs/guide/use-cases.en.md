# Use cases — common workflows

> 🌐 **English** | [한국어](use-cases.md)

The uses listed in the [overview](concepts.en.md#overview) (reports, slides,
papers, code documentation, datasets, notes, wiki) unfold into real command flows
as follows. The skeleton is the same in every case — `sync`/the slash command
only produces **candidates**, a human has to confirm them with `factlog accept`
to make them **accepted**, and only then do they back the answers
from `check`/`ask` (see the
[trust boundary](concepts.en.md#candidate-vs-accepted--the-trust-boundary)).
If you are new, start with the
[quick-start tutorial](../../examples/sample-kb/README.md) (Korean only), which
walks the whole flow through once without your own data.

> **Every command below is typed into a Claude Code session — you never need to
> open a separate terminal.** `/factlog ...` is the slash command that invokes the
> skill; `factlog ...` is the CLI, which Claude runs for you inside the session.
> Both call the same deterministic engine, and promoting a candidate to accepted
> is still decided by **a human typing `factlog accept`**.

**Verifying a report**

- Put the report file in the KB's `sources/`
- `/factlog sync` to extract candidate facts
- `factlog review` to check the candidates and `factlog accept` to approve them
- `/factlog check` or `/factlog ask` to see answers grounded in the accepted facts

**Checking the claims and evidence in slides (PPT)**

- Put the `.pptx` file itself in the KB's `sources/` (a built-in converter turns it into text)
- `/factlog sync` to pull the slides' claims out as candidate facts
- `factlog review` to review them, and `factlog accept` to approve only the claims with clear evidence
- `/factlog ask` to check that the approved claims do not contradict each other
- Mind the conversion limits for slides — it reads only the text shown on the slides, skips presenter notes, and **flattens tables to one line per cell, losing the row/column association** (check the original table when reviewing numeric evidence). Legacy `.ppt` has no converter, so save it as `.pptx` first. See [source file formats](../reference/sources.en.md) for details

**Organizing the core claims of a paper or technical document**

- Put the paper or design document in the KB's `sources/`
- `/factlog sync` to extract the core claims as candidate facts
- `factlog review` to review them and `factlog accept` to confirm them
- `/factlog check` to verify questions against the accepted facts

**Maintaining a wiki you already run**

- Bring the wiki documents into the KB's `sources/`
- `/factlog sync` to refresh candidate facts from the document changes
- `factlog review` to check the new candidates and `factlog accept` to approve them
- `/factlog check` to inspect the consistency of the accumulated accepted facts

**Tracing a fact to its source**

- `factlog search` to find the fact you want to check
- `factlog provenance` to trace which source it came from
- For details, see [Tracing a fact to its source](../reference/search-provenance.en.md#tracing-a-fact-to-its-source-factlog-provenance)

**Cleaning up badly extracted candidates**

- `/factlog sync` to produce candidate facts
- `factlog review` to review the candidates
- retire badly extracted candidates with `factlog reject`; for candidates that only need their wording tidied, fix the value with `factlog amend` and then approve with `factlog accept` (or `factlog amend --accept`)
- For details, see [Reviewing facts](../reference/review.en.md)
