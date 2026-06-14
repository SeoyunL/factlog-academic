#!/usr/bin/env python3
"""Interactively review candidate facts that still need human judgment."""

from __future__ import annotations

import argparse
import csv
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from common import CANDIDATES_CSV, DECISIONS_DIR, FACT_HEADER, ROOT, ensure_dirs, normalize_confidence


REVIEW_STATUSES = {"needs_review", "candidate"}


@dataclass
class ReviewDecision:
    action: str
    before: dict[str, str]
    after: dict[str, str] | None


def read_candidate_rows() -> list[dict[str, str]]:
    if not CANDIDATES_CSV.is_file():
        raise SystemExit(f"missing {CANDIDATES_CSV.relative_to(ROOT)}")
    with CANDIDATES_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = []
        for row in reader:
            clean = {field: str(row.get(field, "")).strip() for field in FACT_HEADER}
            clean["confidence"] = normalize_confidence(clean["confidence"])
            rows.append(clean)
    return rows


def write_candidate_rows(rows: list[dict[str, str]]) -> None:
    CANDIDATES_CSV.parent.mkdir(parents=True, exist_ok=True)
    with CANDIDATES_CSV.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FACT_HEADER)
        writer.writeheader()
        writer.writerows(rows)


def fact_key(row: dict[str, str]) -> str:
    return f"{row['subject']} / {row['relation']} / {row['object']}"


def print_fact(row: dict[str, str], line_no: int | None = None) -> None:
    prefix = f"line {line_no}: " if line_no else ""
    print(f"{prefix}{fact_key(row)}")
    print(f"  status: {row['status']}")
    print(f"  confidence: {row['confidence']}")
    print(f"  source: {row['source']}")
    print(f"  note: {row['note'] or '(none)'}")


def prompt_text(label: str, current: str) -> str:
    value = input(f"{label} [{current}]: ").strip()
    return value if value else current


def edit_fact(row: dict[str, str]) -> dict[str, str]:
    edited = dict(row)
    print("Enter a replacement structure. Press Enter to keep the current value.")
    print("confidence, source, and note are evidence fields; they are shown but not edited here.")
    edited["subject"] = prompt_text("subject", edited["subject"])
    edited["relation"] = prompt_text("relation", edited["relation"])
    edited["object"] = prompt_text("object", edited["object"])
    return edited


def toggle_status(row: dict[str, str]) -> dict[str, str]:
    toggled = dict(row)
    toggled["status"] = "needs_review" if toggled["status"] == "confirmed" else "confirmed"
    return toggled


def review_rows(rows: list[dict[str, str]], limit: int | None = None) -> tuple[list[dict[str, str]], list[ReviewDecision]]:
    updated = [dict(row) for row in rows]
    review_indexes = [idx for idx, row in enumerate(updated) if row["status"] in REVIEW_STATUSES]
    if limit is not None:
        review_indexes = review_indexes[:limit]

    decisions: list[ReviewDecision] = []
    total = len(review_indexes)
    for position, idx in enumerate(review_indexes, start=1):
        row = updated[idx]
        before = dict(row)
        print("")
        print(f"Review {position} / {total}")
        print("----------------")
        print_fact(row, line_no=idx + 2)
        print("")
        print("Choose: [t] toggle status, [e] edit structure, [s] skip, [d] delete row, [q] quit")

        while True:
            action = input("> ").strip().lower() or "s"
            if action in {"t", "toggle"}:
                updated[idx] = toggle_status(row)
                decisions.append(ReviewDecision("toggled", before, dict(updated[idx])))
                break
            if action in {"e", "edit"}:
                edited = edit_fact(row)
                answer = input(f"Toggle status {edited['status']} -> confirmed? [y/N] ").strip().lower()
                if answer in {"y", "yes"}:
                    edited["status"] = "confirmed"
                updated[idx] = edited
                decision_action = "edited+toggled" if edited["status"] != before["status"] else "edited"
                decisions.append(ReviewDecision(decision_action, before, dict(edited)))
                break
            if action in {"s", "skip"}:
                decisions.append(ReviewDecision("skipped", before, dict(row)))
                break
            if action in {"d", "delete"}:
                updated[idx]["__delete__"] = "1"
                decisions.append(ReviewDecision("deleted", before, None))
                break
            if action in {"q", "quit"}:
                return [row for row in updated if row.get("__delete__") != "1"], decisions
            print("Use t, e, s, d, or q.")

    return [row for row in updated if row.get("__delete__") != "1"], decisions


def print_summary(decisions: list[ReviewDecision]) -> None:
    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision.action] = counts.get(decision.action, 0) + 1
    print("")
    print("Review Summary")
    print("==============")
    if not decisions:
        print("no decisions")
        return
    for action, count in sorted(counts.items()):
        print(f"{action}: {count}")
    print("")
    for decision in decisions:
        print(f"- {decision.action}: {fact_key(decision.before)}")
        if decision.after and decision.after != decision.before:
            print(f"  -> {fact_key(decision.after)} [{decision.after['status']}, confidence={decision.after['confidence']}]")


def write_review_log(decisions: list[ReviewDecision]) -> None:
    if not decisions:
        return
    DECISIONS_DIR.mkdir(parents=True, exist_ok=True)
    path = DECISIONS_DIR / "fact-review-log.md"
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    lines = [f"## {timestamp}", ""]
    for decision in decisions:
        lines.append(f"- {decision.action}: {fact_key(decision.before)}")
        lines.append(f"  - before: status={decision.before['status']}, confidence={decision.before['confidence']}, source={decision.before['source']}")
        if decision.after:
            lines.append(f"  - after: {fact_key(decision.after)}")
            lines.append(f"  - after_status: {decision.after['status']}, confidence={decision.after['confidence']}, source={decision.after['source']}")
        else:
            lines.append("  - after: deleted")
    lines.append("")
    previous = path.read_text(encoding="utf-8") if path.exists() else "# Fact Review Log\n\n"
    path.write_text(previous.rstrip() + "\n\n" + "\n".join(lines), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Review needs_review facts in facts/candidates.csv.")
    parser.add_argument("--dry-run", action="store_true", help="only list facts that need review")
    parser.add_argument("--yes", action="store_true", help="write changes without the final confirmation prompt")
    parser.add_argument("--limit", type=int, help="review only the first N rows")
    args = parser.parse_args()

    ensure_dirs()
    rows = read_candidate_rows()
    review_rows_only = [(idx, row) for idx, row in enumerate(rows) if row["status"] in REVIEW_STATUSES]
    if not review_rows_only:
        print("No needs_review or candidate facts found.")
        return 0

    print(f"Facts needing review: {len(review_rows_only)}")
    if args.dry_run:
        for idx, row in review_rows_only[: args.limit]:
            print("")
            print_fact(row, line_no=idx + 2)
        return 0

    updated_rows, decisions = review_rows(rows, args.limit)
    print_summary(decisions)
    changed = [decision for decision in decisions if decision.action in {"toggled", "edited", "edited+toggled", "deleted"}]
    if not changed:
        print("No changes to write.")
        return 0

    if not args.yes:
        answer = input("Write changes to facts/candidates.csv? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            print("Aborted; no files changed.")
            return 0

    write_candidate_rows(updated_rows)
    write_review_log(changed)
    print(f"written: {CANDIDATES_CSV}")
    print(f"review log: {DECISIONS_DIR / 'fact-review-log.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
