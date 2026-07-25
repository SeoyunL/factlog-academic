# Active KB (target the set-up KB from anywhere)

> 🌐 **English** | [한국어](active-kb.md)

After `factlog init`/`setup` (or `factlog use <kb>`), the chosen KB is recorded
as the **active KB**, so `ingest`/`ask`/`sync` and the tools target it from any
working directory — no `--target`/`--wiki` needed:

```bash
factlog use ~/wiki        # make ~/wiki the active KB (recorded in config)
factlog where             # show the active KB and how it was resolved
factlog sources           # list registered sources (original, conversion, fact count)
factlog status            # KB state: facts by status, vocabulary, conflicts, questions, logic freshness, engine
cd /anywhere && factlog ingest report.pdf   # → ~/wiki/runs/sources/report.txt
factlog eject report.pdf  # inverse of ingest: remove the conversion + retire its facts
factlog ignore drafts/*.md   # exclude sources from sync (re-extraction)
factlog provenance Acme uses FastAPI   # trace a fact to its source(s)
```

Resolution precedence: `--target`/`--wiki` flag > `$FACTLOG_ROOT` > active-KB
config (`${XDG_CONFIG_HOME:-~/.config}/factlog/config.json`) > current directory.
With no config set, behavior is unchanged (uses the current directory).

## Resolution precedence table

The four candidates are walked from the top and the **first one with a value**
wins. Which one won is printed on `factlog where`'s `resolved from:` line.

| Rank | Source | How to set it | `resolved from:` in `factlog where` |
|------|--------|---------------|-------------------------------------|
| 1 | command-line flag | `--target <path>` (`--wiki <path>` on some tools) | (not shown — see below) |
| 2 | environment variable | `export FACTLOG_ROOT=<path>` | `env ($FACTLOG_ROOT)` |
| 3 | active-KB config | `factlog use <path>` (or recorded automatically by `factlog init`/`setup`) | `config file` |
| 4 | current directory | (the fallback when nothing else is set) | `current directory` |

Rank 1 never appears in `factlog where`'s output because `where` itself does not
take `--target`. A flag applies only to the **single command** it was given to, so
`where` always reports a result resolved from ranks 2–4.

> **Tools that take a positional root have one extra rank.** `tools/validate.py`
> also accepts the KB path as a positional argument (`validate.py <path>`), and that
> argument sits **between ranks 1 and 2**: `--target`/`--wiki` > positional >
> `$FACTLOG_ROOT` > active-KB config > current directory. The shell harnesses
> (`tests/*.sh`) and `merge_candidates`' delegate pass the KB in that position, so
> ranking it below the config would **validate the active KB instead of the KB the
> caller named**. An empty value (`validate.py ""`) is refused on the spot (exit 1)
> rather than falling through to the next rank — otherwise one unset variable
> silently changes the target.

> **The two mutating tools refuse a rank-3 root.** `tools/finalize.py` and
> `tools/merge_candidates.py` resolve by the table above, but then **refuse** a KB
> that was named only by rank 3 (the active-KB config) while the current directory
> is outside that KB (exit 1). Both rewrite `facts/candidates.csv`, `pages/` and
> `decisions/open-questions.md`, so the refusal stops a run nobody aimed from
> silently overwriting the active KB and invalidating its logic report. There are
> three ways to aim one: name it with `--target`/`--wiki`, name it with
> `$FACTLOG_ROOT`, or run from inside it. The refusal prints the resolved path and
> both ways to name it. `compile_facts.py`/`run_logic_check.py`, which only rewrite
> their own engine outputs, take a rank-3 root with no flag.

Whichever way a path arrives, it goes through `~` expansion and absolute-path
normalization. If the config file is missing, its JSON is corrupt, or its `root`
field is empty, resolution **falls through to the next rank instead of crashing** —
ultimately to the current directory.

## Checking which KB won

*Type in Claude Code:*

```bash
factlog where
```

```text
active KB: /Users/me/wiki
resolved from: config file (precedence: --flag > $FACTLOG_ROOT > config > cwd)
config file: /Users/me/.config/factlog/config.json
```

If you have set a narration language with `factlog lang`, a `narration language:`
line is printed as well (it applies to the assistant's prose only and has no
effect on engine output).

For scripting, `--porcelain` prints **only the active KB's absolute path, on one
line** — no label, no other lines.

*Run in the terminal:*

```bash
export FACTLOG_ROOT="$(factlog where --porcelain)"
```

A KB-targeting command like `ingest`, when run without a flag, tells you on its
first line which KB it picked and where that came from, so you can notice a write
to an unintended KB.

```text
factlog ingest: target KB /Users/me/wiki (from config)
```
