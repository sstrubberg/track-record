#!/usr/bin/env python3
"""Mood/Theme action: apply.

Thin wrapper, not a fork - apply.py's merge-never-replace write logic
(_merge_rows, the create-tag-if-it-doesn't-already-exist handling, the
per-tag failure isolation) has nothing genre-specific in it; only the
plan/log file paths differ per action. See apply.py's own docstring
for why those functions take an explicit `log_file` rather than this
just importing and monkeypatching apply.LOG_FILE.

    python mood_apply.py   # applies the plan's auto-include rows immediately
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import apply

PLAN_FILE = Path(__file__).resolve().parent / "mood_plan.json"
LOG_FILE = Path(__file__).resolve().parent / "mood_applied_log.json"


def apply_auto(plan: dict) -> list[dict]:
    return apply.apply_auto(plan, log_file=LOG_FILE)


def apply_decisions(approved_review: list[dict], approved_create: list[dict]) -> dict:
    return apply.apply_decisions(approved_review, approved_create, log_file=LOG_FILE)


def main():
    p = argparse.ArgumentParser(description="Apply the 'auto' rows from a mood/theme plan immediately.")
    p.add_argument("--plan", default=None, help="alternate plan JSON path")
    args = p.parse_args()

    plan_path = Path(args.plan) if args.plan else PLAN_FILE
    if not plan_path.exists():
        raise SystemExit(f"no {plan_path} - run mood_plan.py first")
    plan = json.loads(plan_path.read_text())

    entries = apply_auto(plan)
    print(f"applied {len(entries)} track(s) from the auto bucket")
    for e in entries:
        print(f"  {e['artist']} - {e['title']}: {', '.join(e['tags_added'])}")
    print(
        f"{len(plan.get('review', []))} review row(s) and "
        f"{len(plan.get('create', []))} create row(s) still need review_ui.py"
    )


if __name__ == "__main__":
    main()
