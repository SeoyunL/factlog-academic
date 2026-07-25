# Bundled deterministic engine

The deterministic scripts the skill calls live here (migrated in plan T1).
**Python 3.11+ required** (the engine dependency `pyrewire` needs 3.11+; see `requires-python` in `pyproject.toml`).

## Main entry points

Not an inventory — `ls tools/*.py` is (the directory has grown well past the eight
rows below, and the old "Scripts (8 files)" heading had been wrong for some time).
These eight are the long-standing core set, which is not the same list as "what the
skill calls": `SKILL.md` also invokes `ask_router.py`, `corroboration.py`,
`entity_audit.py`, `finalize.py` and `source_coverage.py` by name.

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

## Which tree are you actually running? (#553)

`${CLAUDE_PLUGIN_ROOT}/tools/` is the **bundled copy** that ships with a plugin
release. Four files here — `compile_facts.py`, `common.py`, `literal_types.py`,
`factlog_config.py` — put their own distribution root at `sys.path[0]` before any
`factlog` import can happen, and every other script in this directory reaches the
package through one of them. So a bundled run imports the **bundled** `factlog`
package even when a contributor has a newer working tree installed.

That is the right default for a user: a release must be self-contained. It is the
wrong default for a contributor, and it has cost real time — #208, #491, #527 and
#547 were each re-diagnosed as live bugs from reports produced by code that did not
contain the fix.

### Verifying a working tree

Run the checkout's own entry points. This is the only form that puts **both** the
script and the package under your control:

```bash
python3 -m factlog check                     # active/installed factlog package
python3 /path/to/checkout/tools/run_logic_check.py
```

### `FACTLOG_PREFER_INSTALLED=1`

Set it and the four wrappers check whether a `factlog` package is already importable.
If one is, they leave `sys.path` untouched and it wins:

```bash
FACTLOG_PREFER_INSTALLED=1 "${CLAUDE_PLUGIN_ROOT}/tools/run_logic_check.py"
```

**Why a check and not just "append instead of prepend".** `pip install -e .` on this
project makes setuptools emit a `_TopLevelFinder`: the checkout is reachable only
through a finder appended to `sys.meta_path`, *behind* the builtin `PathFinder`.
Appending the bundle root still leaves it on `sys.path`, so `PathFinder` answers with
the bundle and the editable finder never gets asked — the opt-out would do nothing at
all, silently, in the shape most contributors actually have.

If nothing is installed anywhere, the wrappers fall back to appending their own root,
so a bare checkout still runs exactly as before. Only the literal value `1` opts in —
unset, `0`, `""` and `true` all leave the default behaviour untouched.

**Its limit, which matters as much as what it does:** `FACTLOG_PREFER_INSTALLED=1`
guarantees only that **the `factlog` package comes from the installed tree**. It does
**not** guarantee that **your working tree is what runs** — the script body executing
is still the bundled file, at the bundle's version. If you need the script too, run
the checkout's `tools/` directly or use `python3 -m factlog`.

A concrete way that limit shows up: bundled scripts now run against a package of a
different version, so an import the bundle expects may not exist —
`ModuleNotFoundError: No module named 'factlog.runtime'` when the installed tree
predates that module. **That is the documented limit doing what it says, not a new
bug.** Match the two trees, or run the checkout's `tools/`.

When the script tree and the package tree differ, `run_logic_check.py`,
`merge_candidates.py`, `source_coverage.py` and `compile_facts.py` print a warning to
**stderr** naming both paths. It is stderr on purpose: stdout's first two lines are a
positional contract (`factlog: …` then `<tool>: target KB …`), pinned by
`tests/unit/test_report_factlog_provenance.py`.

### When the warning does *not* fire

Two silent states, both by construction:

* **Variable unset.** The wrapper prepends the bundle root, so the script tree and the
  package tree are the same tree and the check returns nothing. A user on the default
  configuration — precisely the situation that produced #208/#491/#527/#547 — gets no
  warning from this. What identifies the running code there is #554's `factlog:` line
  in `logic_report.txt` and on stdout, not this warning.
* **`=1` with the bundle root already on `sys.path`.** `export
  PYTHONPATH="${CLAUDE_PLUGIN_ROOT}"` is enough. The wrappers only act when their root
  is *not* already on `sys.path`, so the whole block — probe included — is skipped, the
  bundle wins on `sys.path` order, and no warning is printed. The opt-out is a silent
  no-op in that environment (measured).

**One thing this cannot fix:** a released bundle's `tools/` is a release artifact, so
an already-installed plugin keeps the old bootstrap until the next release. Until
then, running the repo's `tools/` (or `python3 -m factlog`) is the working answer.

## Intentionally absent scripts

`02_translate_question.py` and `04_self_correct.py` from the workshop source
(`llmwiki-ops`) are **not migrated** as runnable scripts.  Their LLM loops
(subprocess calls to the Claude CLI) are inherently Claude-native and are
implemented directly in the skill (`skills/factlog/SKILL.md`).  The deterministic
core of `04_self_correct.py` (`validate_candidate_query`) was promoted into
`common.py` in u1 so all deterministic steps remain in this directory.

## Engine decoding note

`common.decode_wirelog_value` no longer touches `session._intern`; it passes an
already-decoded value through (#323).  Reading a value cannot tell a symbol id
from a genuine `int64` scalar, and guessing rewrote small scalars into unrelated
symbols.

Decoding is the engine's job, but it only works because we feed it:
`run_wirelog` pre-interns every policy literal, accepted-fact value and canonical
atom through the public `session.intern()`, and pyrewire's `_decode_row` resolves
each STRING column against that table.  A lookup miss does **not** raise — it
falls back silently to the raw `int`, so an un-interned symbol renders as a bare
number instead of text.  That makes the pre-interning load-bearing rather than
dead code: measured on pyrewire 1.0.3, the same program yields
`[('int', 0), ('int', 3)]` without pre-interning and
`[('str', 'alpha'), ('str', 'needs review')]` with it.

The other half of that contract is the schema.  `step()` decodes a row against a
side-program `EasySession` builds by re-parsing the source; if that re-parse
fails, pyrewire keeps `None` and runs on, and `_decode_row` then returns **every**
column as a raw id — a report would print `flagged: 0 (3)`, asserting a subject
the KB does not contain, with a clean exit.  `run_wirelog` therefore checks
`session._schema_program is None` right after constructing the session and refuses
to run.  This is the one private attribute factlog still reads: the facade exposes
no public way to ask whether decoding is live, and the failure is silent, so the
check cannot be replaced by a version constraint.

The dependency stays pinned `pyrewire>=1.0.3,<2.0` in `pyproject.toml` to guard
against silent breakage if that decoding contract (or its raw-int fallback)
changes in a future major release.  The pin is a ceiling, not a substitute for the
checks above: a 1.x **minor** may legally introduce a parser disagreement, and
nothing about that failure is loud on its own.
