# arXiv import (`factlog arxiv-*`)

> 🌐 **English** | [한국어](arxiv.md)

You can import papers from the preprint repository [arXiv](https://arxiv.org) by
id, or search and import them, and check whether a record you imported is still
the latest version. Imported items are still **candidates** and pass the
`sync → review → accept` gate.

## Prerequisites

This needs the `arxiv` extra, and the arXiv API is **unauthenticated** — no API
key or account. There is no credit budget either — instead factlog keeps to
arXiv's recommended 3-second delay between requests on its own (a courtesy that
is not enforced).

When arXiv pushes back (HTTP 503/429) factlog honours `Retry-After`: it waits
as long as the server asked before retrying (one try plus up to two retries),
and falls back to a 2s/4s exponential backoff when the header is absent or
unreadable. A requested wait shorter than 2 seconds still waits 2 seconds:
`Retry-After` is a minimum, so waiting longer complies, and some floor has to
remain under a retry even where `request_delay` has been set to 0. **A requested
wait longer than 60 seconds is not retried
at all** — the command stops immediately. Trimming such a wait down to 60s and
knocking again would be requesting inside the window the server just named,
while the screen quotes the server's own number back at you; the advice and the
behaviour would disagree. The error reports both the wait arXiv asked for and
how many attempts were actually made, so you can wait that long and re-run the
same command.

```bash
pip install 'factlog-academic[arxiv] @ git+https://github.com/SeoyunL/factlog-academic'
```

> The bare name `factlog` on PyPI belongs to an unrelated 2013 project ("File
> ACTivity LOGger"), which has no such extras — pip warns and exits 0, so you get
> a success message and none of the dependencies. Always install from the URL
> above (or from a clone: `pip install -e '.[arxiv]'`).

## Usage

```bash
factlog arxiv-import --id 2311.09277          # latest version
factlog arxiv-import --id 2311.09277v1        # a specific version, pinned inline in the id (there is no --version flag)
factlog arxiv-search --query "chain of thought" --category cs.CL --year 2020-2025
factlog arxiv-check-versions                  # reports only; --auto-update records to the ledger
factlog arxiv-acknowledge-withdrawal --id 2311.09277
factlog arxiv-backfill-provenance             # gives a ledger to papers that have only front matter
```

`--id` is repeatable, up to 100 per run.

## Where a search query lies quietly

If you pass several words as-is, arXiv does not read them as a phrase but matches
them loosely — measured live, the unwrapped query returns close to what its first
word alone matches, many times more than the phrase. So factlog wraps them for
you and sends `all:"your words"`, and tells you it wrapped. If you want loose
matching, use a field prefix (`ti:`, `au:`, `abs:`) or a boolean (`AND`/`OR`/`ANDNOT`)
yourself. Your own double quotes also turn wrapping off, but that is still a
phrase search, not loose matching. A single-word query is never wrapped.
`--show-query` prints the exact query that would be sent, without spending a
request (`--dry-run` does search, but writes no files). arXiv answers a
nonexistent category, field, or year with `200 OK` and "0 results" — which an
operator reads as "no such literature exists" — so factlog validates the values
before sending the request.

## Determinism boundary

The three ledger-based imports — OpenAlex, arXiv, PubMed — obey the same three
rules.

1. **Import has no ledger authority.** An imported record is a `candidate` until it
   passes the `sync → review → accept` gate; import never writes engine input.
2. **`--auto-update` touches only the ledger, never `sources/*.md`.** A `*-refresh`
   / `*-check-versions --auto-update` records a few metadata fields to the
   per-paper ledger under `source-provenance/`; your source files are never rewritten.
3. **A retraction or withdrawal is never absorbed automatically.** It is reported
   to a human on every run and closed only by an explicit `*-acknowledge-*`
   command — revoking a published result is the judgment a human must make, not
   the tool.

arXiv specifics:

- `--auto-update` writes only `version` / `last_updated` / `comment`.
- A **withdrawal** (`withdrawn_by`) is arXiv's own act, not a journal retraction
  (see the note below). Acknowledge with `factlog arxiv-acknowledge-withdrawal
  --id <id>`. `--yes` can *record* a withdrawal but never *clear* one — arXiv no
  longer reporting one is ambiguous (reversed? or the sentence could not be read?),
  so clearing needs a human at the prompt.
- Backfill needs the paper's real version in front matter. A front-matter-only
  paper is closable only if it carries `arxiv_version: <N>` — the one value with no
  ledger fallback, so you add it by hand (read `<N>` from
  `https://arxiv.org/abs/<id>`; factlog does not fetch it), after which
  `factlog arxiv-backfill-provenance` builds the ledger.
- A paper whose version cannot be compared is reported as **`no-version`**, not
  `unchanged` — nothing was compared, so it cannot be called unchanged. The fix
  depends on the cause, and sometimes there is none.

> arXiv's **withdrawal** is not the same as a journal's **retraction** (OpenAlex's
> `is_retracted`). The former is an act by the author or an arXiv administrator on
> a preprint; the latter is a journal revoking a published paper. The recorded
> agent is either `author` or `admin`.

## Configuration

Settings resolve in the order `<KB>/policy/arxiv-config.toml` >
`~/.config/factlog/arxiv.toml` > built-in defaults. Like OpenAlex, there is **no
secrets boundary**: `email` is not authentication but an identification courtesy
carried in the User-Agent.

## Further reading

The Korean [arXiv 가져오기](arxiv.md) covers more than this page does:
per-command options and output, how `--show-query` and `--dry-run` differ,
`version-conflict` between sources of one paper, the shape of the generated
source file and its provenance front matter, the config file keys, and the
idempotency guarantees.
