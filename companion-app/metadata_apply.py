#!/usr/bin/env python3
"""Metadata action: apply (and undo).

Unlike the tag actions, which *merge* tags and can never remove
anything, this overwrites a field (album, year, genre). So it is
careful in ways they don't need to be:

- Each row is checked against Lexicon *right now* before writing. If
  the track's artist no longer matches, or the field's current value is
  no longer what the plan saw (you edited it in Lexicon after
  generating), that row is refused rather than overwriting something the
  reviewer never looked at. A field already holding the new value
  counts as done.
- Every write is recorded, old value and new, in
  metadata_applied_log.json, in batches - the backup. `undo_batch()`
  (or `python metadata_apply.py --undo`) puts the old values back.
- After writing, the tracks are re-read from Lexicon and each field
  compared with what was meant to be written; one that didn't stick is
  reported as failed, not assumed fine.

Returns the same {"entries", "resolved_pairs", "failed"} shape as the tag
actions' apply functions (resolved_pairs here is (track_id, field)), so
review_ui's save-and-prune logic can treat all four alike.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import lexicon_client

LOG_FILE = Path(__file__).resolve().parent / "metadata_applied_log.json"
FIELDS = ("albumTitle", "year", "genre")


def _load_log() -> list[dict]:
    try:
        return json.loads(LOG_FILE.read_text())
    except (OSError, ValueError):
        return []


def _save_log(entries: list[dict]) -> None:
    LOG_FILE.write_text(json.dumps(entries, indent=1))


def clean_value(field: str, value):
    """The value as it should be written, or raises ValueError saying why not."""
    if field == "year":
        try:
            y = int(str(value).strip())
        except (TypeError, ValueError):
            raise ValueError(f"year {value!r} isn't a number")
        if not 1900 <= y <= date.today().year + 1:
            raise ValueError(f"year {y} is outside 1900-{date.today().year + 1}")
        return y
    text = str(value).strip() if value is not None else ""
    if not text:
        raise ValueError(f"{field} is empty")
    return text


def apply_decisions(approved_rows: list[dict]) -> dict:
    """approved_rows: plan rows (track_id, artist, title, field, old, new)
    whose `new` is already the reviewer's final value (edited or not)."""
    by_track: dict[int, list[dict]] = defaultdict(list)
    for row in approved_rows:
        by_track[row["track_id"]].append(row)

    library = {t["id"]: t for t in lexicon_client.fetch_library()}
    batch = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entries: list[dict] = []
    resolved: set[tuple[int, str]] = set()
    failed: list[dict] = []
    intended: dict[int, dict] = {}

    def fail(first: dict, error: str, fields: list[str]) -> None:
        failed.append({"track_id": first["track_id"], "artist": first["artist"], "title": first["title"],
                       "error": error, "fields": fields})

    for tid, rows in by_track.items():
        first = rows[0]
        track = library.get(tid)
        if track is None:
            fail(first, "track not found in Lexicon", [r["field"] for r in rows])
            continue
        if (track.get("artist") or "") != first["artist"]:
            fail(first, "artist no longer matches the plan - regenerate it", [r["field"] for r in rows])
            continue

        edits: dict = {}
        changes: dict = {}
        for row in rows:
            field = row["field"]
            try:
                new = clean_value(field, row["new"])
            except ValueError as e:
                fail(row, str(e), [field])
                continue
            current = track.get(field)
            if current == new:
                resolved.add((tid, field))  # already there
            elif row.get("old") != current and not (row.get("old") in (None, "") and current in (None, "")):
                fail(row, f"{field} changed in Lexicon since the plan was made (now {current!r}) - regenerate", [field])
            else:
                edits[field] = new
                changes[field] = {"old": current, "new": new}
        if not edits:
            continue
        ok, detail = lexicon_client.write_track_fields(tid, edits)
        if not ok:
            fail(first, detail or "write failed", list(edits))
            continue
        intended[tid] = edits
        entries.append({"batch": batch, "track_id": tid, "artist": first["artist"], "title": first["title"], "changes": changes})

    # Verify with a fresh read: written is not the same as stuck.
    if intended:
        fresh = {t["id"]: t for t in lexicon_client.fetch_library()}
        verified = []
        for entry in entries:
            tid = entry["track_id"]
            bad = [f for f, v in intended[tid].items() if (fresh.get(tid) or {}).get(f) != v]
            if bad:
                fail(entry, "Lexicon accepted the write but the value didn't change", bad)
            else:
                verified.append(entry)
                resolved.update((tid, f) for f in intended[tid])
        entries = verified

    if entries:
        log = _load_log()
        log.extend(entries)
        _save_log(log)
    return {"entries": entries, "resolved_pairs": resolved, "failed": failed}


def undo_batch(batch: str | None = None) -> dict:
    """Put the old values back for one batch (default: the most recent).
    A field whose value is no longer what that batch wrote is left alone -
    it was changed again since, and undoing would clobber that."""
    log = _load_log()
    if not log:
        return {"restored": [], "skipped": [], "batch": None}
    batch = batch or log[-1]["batch"]
    target = [e for e in log if e["batch"] == batch]
    library = {t["id"]: t for t in lexicon_client.fetch_library()}
    restored, skipped = [], []
    for e in target:
        track = library.get(e["track_id"]) or {}
        edits = {}
        for field, ch in e["changes"].items():
            if track.get(field) == ch["new"]:
                edits[field] = ch["old"]
            else:
                skipped.append({**e, "field": field, "reason": "changed since this batch"})
        # An old value that was empty can't be written back as a PATCH
        # of None reliably; leave those to be cleared by hand.
        writable = {f: v for f, v in edits.items() if v not in (None, "")}
        for f in set(edits) - set(writable):
            skipped.append({**e, "field": f, "reason": "old value was empty - clear it by hand"})
        if not writable:
            continue
        ok, detail = lexicon_client.write_track_fields(e["track_id"], writable)
        (restored if ok else skipped).append({**e, "fields": list(writable), "reason": detail} if not ok else {**e, "fields": list(writable)})
    if restored:
        undone = {(r["batch"], r["track_id"]) for r in restored}
        _save_log([e for e in log if (e["batch"], e["track_id"]) not in undone])
    return {"restored": restored, "skipped": skipped, "batch": batch}


def main():
    p = argparse.ArgumentParser(description="Undo the last Metadata apply batch.")
    p.add_argument("--undo", action="store_true", help="restore the old values from the most recent batch")
    p.add_argument("--batch", default=None, help="a specific batch timestamp from metadata_applied_log.json")
    args = p.parse_args()
    if not args.undo:
        p.print_help()
        return
    r = undo_batch(args.batch)
    print(f"batch {r['batch']}: restored {len(r['restored'])} track(s), skipped {len(r['skipped'])}")
    for s in r["skipped"]:
        print(f"  skipped {s['artist']} - {s['title']} ({s.get('field')}): {s['reason']}")


if __name__ == "__main__":
    main()
