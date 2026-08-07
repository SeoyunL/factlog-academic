# Determinism & limitations

> 🌐 **English** | [한국어](determinism.md)

A skill is a prompt, so the model is *guided*, not *forced*, to run each step. factlog keeps every step that must be reliable — fact compilation, the wirelog logic check, policy compilation, validation — as **bundled scripts the skill is instructed to run and trust**, never as model judgment. The logic check report is always produced by the engine, never narrated by the model.

### AC4 — stale-edit guard (two levels)

factlog enforces freshness through two distinct mechanisms:

| Level | Mechanism | What it guarantees |
|-------|-----------|-------------------|
| **Hook-enforced** | `PreToolUse` hook denies any `Write`/`Edit` to `facts/accepted.dl` or `facts/query.dl` when `facts/logic_report.txt` is missing, older than those files, or records a run in which the engine never ran (run `/factlog check` → `run_logic_check.py` to refresh) | The engine's compiled inputs cannot be overwritten when the logic report is stale — the hook blocks the tool call before the file is touched |
| **SKILL discipline (best-effort)** | `SKILL.md` instructs Claude to run `run_logic_check.py` and show `facts/logic_report.txt` verbatim before stating any conclusion | The model is *guided* to surface the engine report; it cannot be *forced* (R10: "cannot fully guarantee") — human review of the raw report is the final verification step |

These two levels are complementary: the hook closes the deterministic gap; the SKILL discipline covers the narration layer where engineering enforcement is not possible.

> **An existing KB needs one `/factlog check` first.** In earlier versions the
> hook could not read Claude Code's real payload, so this deny never fired. From
> this fix on, a KB that already has `facts/accepted.dl` or `facts/query.dl`
> while `facts/logic_report.txt` is missing or older must be refreshed with
> `/factlog check` before its engine inputs can be edited.

The hook denies for **one more reason** besides staleness. It reads the target
path out of the tool payload Claude Code sends; if the call is a `Write`/`Edit`
and its `tool_input` did arrive as an object but carries no readable path key —
neither inside it nor at the top level — the hook denies rather than letting a
write through that cannot be shown *not* to target an engine input. An empty
`file_path` string lands here too.

That condition is narrow on purpose. If the `tool_input` envelope key **itself**
disappears or gets renamed, the call is allowed, not denied: that shape of change
hits every `Write`/`Edit` in every session at once, so denying would make the
hook a global write outage rather than a gate. That branch reports itself
instead, with a note saying the check was skipped.

`FACTLOG_GATE_ALLOW_UNREADABLE_PAYLOAD=1` releases **only** the deny above. It
does **not** release the staleness deny, nor the Python-availability deny. It is
not something the model can do from inside the session: a hook inherits the
Claude Code process environment and that environment is fixed when Claude Code
starts, so **a human** has to set it — in the `env` block of `settings.json`, or
exported before launching Claude Code — and then start a new session. Please
also report the payload shape upstream.

### When the deny will not lift because the KB cannot produce a report

The staleness deny points at `/factlog check`, but `/factlog check` itself fails
in some KBs. For example, if `facts/query.dl` exists but `facts/accepted.dl` does
not, the logic check stops before it can start the engine — as it also does when
the `pyrewire` engine is missing or too old.

In that case the check usually still writes `facts/logic_report.txt`, and the
report records the failure. Its first lines look like this:

```
Logic Check Report
==================
status: engine-did-not-run
engine: wirelog / pyrewire
input: facts/accepted.dl
reason: missing facts/accepted.dl; run tools/compile_facts.py first
reason type: FactlogError
...
```

Read the `status:` line as "this report says nothing about the KB". It carries
no counts at all — not `engine facts: 0`, which would mean the engine ran and
found nothing. Where a report from an earlier successful run existed, this
replaces it, so an older result cannot be mistaken for this one.

"Usually" covers two cases. If the check dies before it starts — failing to load
the engine package at all, say — it never reaches the code that writes the
report. And if `facts/` cannot be written, the check gives up on the report and
shows the original error instead, because the diagnosis matters more than the
file. In both cases the previous report is still there, so read `/factlog
check`'s own output alongside it to know whether the report is this run's.

**That report does not lift the deny.** A run that never reached the engine is
not evidence that editing engine inputs is safe, so the gate keeps denying while
the `status:` line is there — it just names the cause instead of pointing back at
the command that failed. One thing it does not change: creating `facts/query.dl`
or `facts/accepted.dl` for the first time in a KB stays allowed, exactly as when
no report exists.

The hook only matches `Write` and `Edit`, so recovery runs through **Bash**.
Compiling first — which produces `facts/accepted.dl` — is what clears it. Note
that if `facts/candidates.csv` is absent the compile itself stops with `missing
facts/candidates.csv`, so seed a header row when the file is missing. `factlog
init` does not recreate it.

```bash
cd <KB root>
[ -f facts/candidates.csv ] || \
  echo 'subject,relation,object,source,status,confidence,note' > facts/candidates.csv
"${CLAUDE_PLUGIN_ROOT}"/tools/factlog_python.sh "${CLAUDE_PLUGIN_ROOT}"/tools/compile_facts.py
"${CLAUDE_PLUGIN_ROOT}"/tools/factlog_python.sh "${CLAUDE_PLUGIN_ROOT}"/tools/run_logic_check.py
```

Compiling an empty `candidates.csv` produces an `accepted.dl` with zero facts.
The logic check then runs, a report appears, and the deny lifts — no existing
fact is discarded. If `candidates.csv` already exists it compiles as-is.

If the draft query is expendable, moving `facts/query.dl` aside works too: with
no engine input present, the deny has nothing to fire on.

If the logic check fails for some other reason, that failure has to be fixed
first. Editing engine inputs around the gate is the exact behaviour this deny
exists to prevent, so it has no escape hatch.

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
