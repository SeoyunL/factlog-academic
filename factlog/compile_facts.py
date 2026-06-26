#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Compile confirmed factlog facts into a Datalog-like fact file."""

from __future__ import annotations

from factlog.common import FACTS_DIR, dl_atom, engine_facts, ensure_dirs, load_facts


def main() -> None:
    ensure_dirs()
    facts = load_facts()
    accepted = engine_facts(facts)
    lines = [
        "// generated from facts/candidates.csv",
        "// only confirmed/accepted facts become engine input",
        "",
    ]
    for row in accepted:
        lines.append(dl_atom(row))

    out = FACTS_DIR / "accepted.dl"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"engine facts: {len(accepted)} / {len(facts)}")
    for row in accepted:
        print(
            "  - "
            f"{row['subject']} / {row['relation']} / {row['object']} "
            f"(confidence={row['confidence']}, source={row['source']})"
        )
    print(f"written: {out}")


if __name__ == "__main__":
    from factlog.common import run_cli

    raise SystemExit(run_cli(main))
