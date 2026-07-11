# tests

- Skill smoke: install into a sample KB, run the bridge, assert the four contract
  artifacts and that the deterministic logic check ran (plan T11).
- Deterministic golden regression for the engine steps (plan T12).
- `setup.sh` — one-shot `factlog setup` orchestration (u18): on an env where
  pyrewire is already present, asserts `setup` performs doctor + init, exits 0,
  creates the KB layout, and is idempotent on re-run. Network/pip-independent;
  run with the venv python (`/tmp/factlog-venv`) so the engine check passes.

## Harness conventions

- **`XDG_CONFIG_HOME` must be isolated** in every harness that runs `init`, `use`,
  or `setup`. The active KB lives in a config file under it, so an un-isolated
  harness rewrites the developer's own active KB.
- **A second `init` in the same harness does NOT change the active KB** (#210).
  `init` adopts its target only when no usable active KB is configured yet. A
  harness that scaffolds a second KB and then relies on it being active will
  fail; pass `--target`/`--wiki` explicitly (what every existing harness does),
  call `factlog use`, or scaffold with `init --activate`.
- **Do not drive `factlog setup` from a `tests/test_*.sh` harness.** CI's shell
  job installs no dependencies by design, so `setup` would run `pip install` and
  reach the network there. Pin setup's behaviour with unit tests instead — see
  `tests/unit/test_active_kb_adoption.py`.
