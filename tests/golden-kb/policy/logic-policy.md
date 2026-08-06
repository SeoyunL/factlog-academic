# Logic policy

Compiled to `logic-policy.dl` by `tools/generate_logic_policy.py`; the golden
harness re-derives it with `--check` on every run.

## Rules

- [maintainer_check] Facts with the `maintained_by` relation require review to confirm the maintainer is documented in sources.
- [alias_check] {canonical} Facts with the `maintained_by` relation require review to confirm the maintainer is documented in sources.
