# Bundled deterministic engine

The deterministic scripts the skill calls live here (migrated in plan T1).
**Python 3.11+ required** (the engine dependency `pyrewire` needs 3.11+; see `requires-python` in `pyproject.toml`).

## Scripts (8 files)

| Script | Purpose |
|---|---|
| `compile_facts.py` | confirmed facts → `facts/accepted.dl` |
| `run_logic_check.py` | wirelog/pyrewire logic check → `facts/logic_report.txt` |
| `generate_logic_policy.py` | validated policy JSON → `policy/logic-policy.dl` |
| `merge_candidates.py` | merge/dedup/stale-detect candidate facts into `facts/candidates.csv` |
| `review_candidates.py` | review candidate facts |
| `validate.py` | schema and referential validation |
| `resolve_stale_refs.py` | stale-reference resolution |
| `common.py` | shared helpers, `decode_wirelog_value`, `validate_candidate_query` |

The skill invokes these via `${CLAUDE_PLUGIN_ROOT}/tools/<script>.py`. They are the
verifiable anchor — never replaced by model judgment.

## KB root resolution

Every script here resolves its KB root with the same precedence as the CLI:

```
--wiki / --target flag  >  $FACTLOG_ROOT  >  active-KB config  >  cwd
```

`validate.py` takes the root as a positional argument instead of a flag; the
positional occupies the first slot. The active-KB config is what `factlog use
<kb>` records — `init`/`setup` record their target only while the config holds
nothing yet, or when asked with `--activate` — so with no flag and no
`$FACTLOG_ROOT` these scripts operate on the active KB from any directory.

## Intentionally absent scripts

`02_translate_question.py` and `04_self_correct.py` from the workshop source
(`llmwiki-ops`) are **not migrated** as runnable scripts.  Their LLM loops
(subprocess calls to the Claude CLI) are inherently Claude-native and are
implemented directly in the skill (`skills/factlog/SKILL.md`).  The deterministic
core of `04_self_correct.py` (`validate_candidate_query`) was promoted into
`common.py` in u1 so all deterministic steps remain in this directory.

## Private API note

`common.decode_wirelog_value` uses `session._intern` (a private pyrewire
EasySession attribute).  The dependency is pinned `pyrewire>=1.0.3,<2.0` in
`pyproject.toml` to guard against silent breakage if the internal API changes in
a future major release.
