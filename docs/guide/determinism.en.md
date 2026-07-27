# Determinism & limitations

> 🌐 **English** | [한국어](determinism.md)

A skill is a prompt, so the model is *guided*, not *forced*, to run each step. factlog keeps every step that must be reliable — fact compilation, the wirelog logic check, policy compilation, validation — as **bundled scripts the skill is instructed to run and trust**, never as model judgment. The logic check report is always produced by the engine, never narrated by the model.

### AC4 — stale-edit guard (two levels)

factlog enforces freshness through two distinct mechanisms:

| Level | Mechanism | What it guarantees |
|-------|-----------|-------------------|
| **Hook-enforced** | `PreToolUse` hook denies any `Write`/`Edit` to `facts/accepted.dl` or `facts/query.dl` when `facts/logic_report.txt` is missing or older than those files (run `/factlog check` → `run_logic_check.py` to refresh), subject to the bootstrap exception below | The engine's compiled inputs cannot be overwritten when the logic report is stale — the hook blocks the tool call before the file is touched |
| **SKILL discipline (best-effort)** | `SKILL.md` instructs Claude to run `run_logic_check.py` and show `facts/logic_report.txt` verbatim before stating any conclusion | The model is *guided* to surface the engine report; it cannot be *forced* (R10: "cannot fully guarantee") — human review of the raw report is the final verification step |

**Bootstrap exception.** A missing report is not by itself a denial. The hook allows the write when the report is missing **and** the target file does not yet exist on disk. A freshly `factlog init`ed KB seeds neither `facts/logic_report.txt` nor `facts/query.dl`, so the first creation of an engine input cannot possibly be preceded by a report. The test is made **per target file**, not against the KB as a whole — with no report present, a write creating a not-yet-existing `facts/accepted.dl` is allowed even when `facts/query.dl` already exists. If the target itself already exists and no report supersedes it (e.g. the report was deleted), the write to that target is still denied. The hook's predicate is written out in `hooks/gate_check.sh` as three ALLOW branches (A/B/C), and `tests/test_gate_check.sh` pins it in both directions: bootstrap is allowed, and the stale-guard still denies.

**Unreadable payloads.** The hook reads its target from `tool_input.file_path` in the `PreToolUse` payload (a bare top-level `file_path` is still accepted, for callers that pipe a raw tool input). If no target can be read at all, the hook does not simply wave the call through: it scans the raw payload for `facts/accepted.dl` or `facts/query.dl`. Finding neither, it allows — the payload demonstrably names no engine input. Finding one, it treats the write as targeting an engine input of unknown identity, which rules out the bootstrap exception (that needs a *named* file to test on disk) and leaves a fresh report as the only way through. So a payload the gate cannot parse still cannot slip an engine-input write past it, and `/factlog check` remains the way out.

These two levels are complementary: the hook closes the deterministic gap; the SKILL discipline covers the narration layer where engineering enforcement is not possible.

### Scale & performance

**You don't need to empty the KB for performance.** The logic-check cost depends
less on the total number of facts than on the number of **entity-to-entity
relations** (edges where the object of A→B becomes a subject again), because the
engine computes reachability (paths). An attribute-heavy KB — where objects are
mostly literals — scales cheaply to tens or hundreds of thousands of facts, while
a dense entity graph (citation/dependency networks, etc.) can get heavy sooner.
So the metric to watch is not the total fact count but the **entity↔entity edge
count**.

If it does get heavy, the answer is not to "empty" it. Adjust the relation
modeling and manage recurring cost with `factlog ignore` (exclude from
re-extraction) and idempotent ingest. Correctness and de-duplication hold
regardless of scale.
