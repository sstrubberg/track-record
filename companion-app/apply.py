#!/usr/bin/env python3
"""Genre/Subgenre action: apply.

Two ways in:
- `python apply.py` applies every "auto" row from the plan immediately -
  no review needed, per the shared pipeline's rule that a tag clearing
  the auto-include bar goes straight to the applied log.
- review_ui.py imports apply_decisions() directly once a DJ has checked
  boxes in the review screen, for "review" and "create" rows.

Same rule as billboard_tag.py: merge, never replace - reads each
track's live tag array and appends, since Lexicon's `tags` field is
flat and a bare overwrite wipes unrelated tags.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import lexicon_client

PLAN_FILE = Path(__file__).resolve().parent / "genre_plan.json"
LOG_FILE = Path(__file__).resolve().parent / "genre_applied_log.json"


def _load_log() -> list[dict]:
    if LOG_FILE.exists():
        return json.loads(LOG_FILE.read_text())
    return []


def _save_log(entries: list[dict]) -> None:
    LOG_FILE.write_text(json.dumps(entries, indent=1))


def _merge_rows(rows: list[dict], live_tags: dict[int, list[int]]) -> list[dict]:
    """Group rows by track, merge new tag ids into each track's live
    tags, write only tracks that actually change. Returns log entries
    for whatever was actually applied."""
    by_track: dict[int, list[dict]] = {}
    for row in rows:
        by_track.setdefault(row["track_id"], []).append(row)

    entries = []
    shape = None  # negotiated once, reused across every write this call
    for track_id, track_rows in by_track.items():
        existing = live_tags.get(track_id)
        if existing is None:
            print(f"  track {track_id} not found in library - skipping")
            continue
        add_ids = sorted({r["tag_id"] for r in track_rows if r["tag_id"] not in existing})
        if not add_ids:
            continue

        merged = existing + add_ids
        ok, used_shape, detail = lexicon_client.write_track_tags(track_id, merged, shape=shape)
        if not ok:
            print(f"  failed on track {track_id}: {detail}")
            continue
        shape = used_shape
        live_tags[track_id] = merged

        entries.append({
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "track_id": track_id,
            "artist": track_rows[0].get("artist"),
            "title": track_rows[0].get("title"),
            "tags_added": sorted({r["tag"] for r in track_rows if r["tag_id"] in add_ids}),
        })
    return entries


def apply_auto(plan: dict) -> int:
    """Apply every row in plan['auto'] immediately. Returns how many
    tracks were actually changed."""
    rows = plan.get("auto", [])
    if not rows:
        return 0
    live_tags = {t["id"]: list(t.get("tags") or []) for t in lexicon_client.fetch_library()}
    entries = _merge_rows(rows, live_tags)
    if entries:
        log = _load_log()
        log.extend(entries)
        _save_log(log)
    return len(entries)


def apply_decisions(approved_review: list[dict], approved_create: list[dict]) -> int:
    """Called by review_ui.py once a DJ has checked boxes.

    `approved_create` rows don't have a tag_id yet - the tag doesn't
    exist until this creates it. The same new tag name is only ever
    created once per call, even if several tracks proposed it.
    """
    live_tags = {t["id"]: list(t.get("tags") or []) for t in lexicon_client.fetch_library()}

    created_ids: dict[str, int] = {}
    resolved_create = []
    for row in approved_create:
        key = row["tag"].lower()
        if key not in created_ids:
            created_ids[key] = lexicon_client.create_tag(row["tag"], row["category_id"])
            print(f"  created tag '{row['tag']}' (id {created_ids[key]})")
        resolved_create.append({**row, "tag_id": created_ids[key]})

    entries = _merge_rows(approved_review + resolved_create, live_tags)
    if entries:
        log = _load_log()
        log.extend(entries)
        _save_log(log)
    return len(entries)


def main():
    p = argparse.ArgumentParser(description="Apply the 'auto' rows from a genre plan immediately.")
    p.add_argument("--plan", default=None, help="alternate plan JSON path")
    args = p.parse_args()

    plan_path = Path(args.plan) if args.plan else PLAN_FILE
    if not plan_path.exists():
        raise SystemExit(f"no {plan_path} - run plan.py first")
    plan = json.loads(plan_path.read_text())

    n = apply_auto(plan)
    print(f"applied {n} track(s) from the auto bucket")
    print(
        f"{len(plan.get('review', []))} review row(s) and "
        f"{len(plan.get('create', []))} create row(s) still need review_ui.py"
    )


if __name__ == "__main__":
    main()
