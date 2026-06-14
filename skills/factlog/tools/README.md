# Bundled deterministic engine

The deterministic scripts the skill calls live here (migrated in plan T1):

- `compile_facts.py` — confirmed facts → `facts/accepted.dl`
- `run_logic_check.py` — wirelog/pyrewire logic check → `facts/logic_report.txt`
- `generate_logic_policy.py` — validated policy JSON → `policy/logic-policy.dl`
- `merge_candidates.py` — merge/dedup/stale-detect candidate facts into `facts/candidates.csv`
- `review_candidates.py`, `validate.py`, `resolve_stale_refs.py`, `common.py`

The skill invokes these via `${CLAUDE_SKILL_DIR}/tools/<script>.py`. They are the
verifiable anchor — never replaced by model judgment.
