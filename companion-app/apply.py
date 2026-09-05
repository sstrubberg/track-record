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

Every function here that touches the applied log takes an optional
`log_file` - defaults to this action's own genre_applied_log.json, but
mood_apply.py (a thin wrapper, not a fork) imports and calls these
same functions with its own log/plan paths instead of duplicating the
create/merge logic. The genre vs. mood *plan* files stay genuinely
separate structures (different taxonomies, different scoring config);
the apply-side logic underneath doesn't know or care which produced
what it's writing.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import lexicon_client

PLAN_FILE = Path(__file__).resolve().parent / "genre_plan.json"
LOG_FILE = Path(__file__).resolve().parent / "genre_applied_log.json"


def _load_log(log_file: Path = LOG_FILE) -> list[dict]:
    if log_file.exists():
        return json.loads(log_file.read_text())
    return []


def _save_log(entries: list[dict], log_file: Path = LOG_FILE) -> None:
    log_file.write_text(json.dumps(entries, indent=1))


def _merge_rows(rows: list[dict], live_tags: dict[int, list[int]]) -> dict:
    """Group rows by track, merge new tag ids into each track's live
    tags, write only tracks that actually change.

    Returns {"entries": [...] (per track actually written - tags_added
    is only the genuinely-new tag names), "resolved_pairs": {(track_id,
    tag), ...} (every row that's now correctly reflected in Lexicon,
    whether this call just wrote it or it was already there before this
    call - safe for a caller to stop treating as still needing a
    decision), "failed": [{"track_id", "artist", "title", "error"}]
    (tracks that genuinely could not be written - not found in the
    library, or Lexicon rejected every PATCH shape - a caller should
    keep these flagged, not silently drop them)}.

    Used to just print() a failure and move on, with nothing at all
    telling the caller it happened - review_ui.py's save() had no way
    to surface it, and no way to tell "genuinely failed" apart from
    "nothing needed writing," so a row belonging to either case stayed
    checked and un-prunable forever, endlessly re-triggering "N checked
    tag(s) haven't been saved yet" even after a DJ had, in fact, just
    applied everything Lexicon would accept (confirmed: this is exactly
    what a real DJ hit - a write failure or an already-applied tag
    sitting in an otherwise-successful save, invisible either way).
    """
    by_track: dict[int, list[dict]] = {}
    for row in rows:
        by_track.setdefault(row["track_id"], []).append(row)

    entries = []
    resolved_pairs: set[tuple[int, str]] = set()
    failed: list[dict] = []
    shape = None  # negotiated once, reused across every write this call
    for track_id, track_rows in by_track.items():
        artist, title = track_rows[0].get("artist"), track_rows[0].get("title")
        existing = live_tags.get(track_id)
        if existing is None:
            print(f"  track {track_id} not found in library - skipping")
            failed.append({"track_id": track_id, "artist": artist, "title": title, "error": "track not found in library"})
            continue

        add_ids = sorted({r["tag_id"] for r in track_rows if r["tag_id"] not in existing})
        if not add_ids:
            # Every checked tag on this track was already there before
            # this call - a previous partial save, or the DJ tagged it
            # manually in Lexicon in between. Nothing to write, but
            # every one of these rows is genuinely resolved: the tag
            # really is on the track, just not because of this call.
            resolved_pairs.update((track_id, r["tag"]) for r in track_rows)
            continue

        merged = existing + add_ids
        ok, used_shape, detail = lexicon_client.write_track_tags(track_id, merged, shape=shape)
        if not ok:
            print(f"  failed on track {track_id}: {detail}")
            failed.append({"track_id": track_id, "artist": artist, "title": title, "error": detail})
            continue
        shape = used_shape
        live_tags[track_id] = merged

        entries.append({
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "track_id": track_id,
            "artist": artist,
            "title": title,
            "tags_added": sorted({r["tag"] for r in track_rows if r["tag_id"] in add_ids}),
        })
        # Every row on a successfully-written track is resolved, not
        # just the ones that triggered a new write this time - a row
        # whose own tag_id happened to already be present, sitting
        # alongside others on the same track that weren't, is just as
        # done as the ones that just got added.
        resolved_pairs.update((track_id, r["tag"]) for r in track_rows)

    return {"entries": entries, "resolved_pairs": resolved_pairs, "failed": failed}


def apply_auto(plan: dict, log_file: Path = LOG_FILE) -> dict:
    """Apply every row in plan['auto'] immediately. Returns _merge_rows'
    own result dict ("entries", "resolved_pairs", "failed") - "auto"
    means no review screen ever shows this, and the caller (review_ui.py,
    or the __main__ block below) needs something to display in its
    place, including anything that failed."""
    rows = plan.get("auto", [])
    if not rows:
        return {"entries": [], "resolved_pairs": set(), "failed": []}
    live_tags = {t["id"]: list(t.get("tags") or []) for t in lexicon_client.fetch_library()}
    result = _merge_rows(rows, live_tags)
    if result["entries"]:
        log = _load_log(log_file)
        log.extend(result["entries"])
        _save_log(log, log_file)
    return result


def apply_decisions(
    approved_review: list[dict], approved_create: list[dict], log_file: Path = LOG_FILE,
) -> dict:
    """Called by review_ui.py once a DJ has checked boxes.

    `approved_create` rows don't have a tag_id yet - the tag doesn't
    exist until this creates it. The same new tag name is only ever
    created once per call, even if several tracks proposed it.

    A single tag failing to create (Lexicon rejects the label, a
    network hiccup) used to raise straight out of this function,
    aborting everything else in the same save - including plain
    review rows that need no creation at all and had nothing to do
    with the failure. Now it's caught per-tag and reported instead;
    every other approved row still goes through `_merge_rows` below.

    Also checks whether a proposed label already exists before
    creating it. Two reasons that matters, not just one: a DJ can
    retry a save after a partial failure (this function doesn't
    memoize anything across calls), and - the case this was actually
    caught by - create_tag() itself used to mis-parse a successful
    response as a failure (see lexicon_client.create_tag's docstring),
    so a label could already be sitting in Lexicon, created but never
    attached to a track, from a run that looked like it failed
    entirely. Either way, re-creating the same label rather than
    reusing it would leave a duplicate Custom Tag behind.

    Returns _merge_rows' own result dict ("entries", "resolved_pairs",
    "failed" - see its docstring), plus "failed_creates":
    [{"tag": str, "error": str}, ...] for a proposed tag that couldn't
    even be created in the first place, kept separate from "failed"
    since it's a different failure mode (a brand-new tag Lexicon
    rejected, vs. an existing tag Lexicon wouldn't let this write
    attach to a track).
    """
    live_tags = {t["id"]: list(t.get("tags") or []) for t in lexicon_client.fetch_library()}
    _, by_label = lexicon_client.fetch_tag_index()

    created_ids: dict[str, int] = {}
    failed_tags: set[str] = set()
    failed_creates: list[dict] = []
    resolved_create = []
    for row in approved_create:
        key = row["tag"].lower()
        if key in failed_tags:
            continue
        if key not in created_ids:
            existing_id = lexicon_client.resolve_tag_id(row["tag"], by_label)
            if existing_id is not None:
                created_ids[key] = existing_id
                print(f"  '{row['tag']}' already exists (id {existing_id}) - reusing it")
            else:
                try:
                    created_ids[key] = lexicon_client.create_tag(row["tag"], row["category_id"])
                    print(f"  created tag '{row['tag']}' (id {created_ids[key]})")
                except Exception as e:
                    failed_tags.add(key)
                    failed_creates.append({"tag": row["tag"], "error": str(e)})
                    print(f"  failed to create tag '{row['tag']}': {e}")
                    continue
        resolved_create.append({**row, "tag_id": created_ids[key]})

    result = _merge_rows(approved_review + resolved_create, live_tags)
    if result["entries"]:
        log = _load_log(log_file)
        log.extend(result["entries"])
        _save_log(log, log_file)
    return {**result, "failed_creates": failed_creates}


def main():
    p = argparse.ArgumentParser(description="Apply the 'auto' rows from a genre plan immediately.")
    p.add_argument("--plan", default=None, help="alternate plan JSON path")
    args = p.parse_args()

    plan_path = Path(args.plan) if args.plan else PLAN_FILE
    if not plan_path.exists():
        raise SystemExit(f"no {plan_path} - run plan.py first")
    plan = json.loads(plan_path.read_text())

    result = apply_auto(plan)
    print(f"applied {len(result['entries'])} track(s) from the auto bucket")
    for e in result["entries"]:
        print(f"  {e['artist']} - {e['title']}: {', '.join(e['tags_added'])}")
    for f in result["failed"]:
        print(f"  FAILED {f['artist']} - {f['title']}: {f['error']}")
    print(
        f"{len(plan.get('review', []))} review row(s) and "
        f"{len(plan.get('create', []))} create row(s) still need review_ui.py"
    )


if __name__ == "__main__":
    main()
