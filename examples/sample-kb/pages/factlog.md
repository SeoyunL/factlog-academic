# factlog

factlog is a Claude Code plugin for keeping a markdown knowledge base honest.
It follows one rule: the agent does not draw conclusions — the agent produces
files and calls a CLI, and the CLI returns a verifiable report.

## Key facts

- Type: Claude Code plugin
- Performs: fact extraction (via Claude in-session)
- Performs: logic checking (via deterministic Python scripts / wirelog engine)
- Part of: Claude Code plugin system

## How it works

Facts flow through a deterministic pipeline:

1. **Extract** — Claude reads `sources/` and writes candidate facts to `runs/*.json`
2. **Merge** — `merge_candidates.py` normalises candidates into `facts/candidates.csv`
3. **Compile** — `compile_facts.py` promotes confirmed rows to `facts/accepted.dl`
4. **Check** — `run_logic_check.py` runs the wirelog/pyrewire engine and writes `facts/logic_report.txt`

Candidates are not engine input until a human confirms them.

## Related

- [Claude Code](claude-code.md) — the plugin host environment
- Sources: sources/example.md#factlog-skill, sources/example.md#plugin-system
