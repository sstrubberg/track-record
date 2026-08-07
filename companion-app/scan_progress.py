"""Tracks how far a "whole library" Genre/Subgenre or Mood/Theme scan
has gotten, so working through a big library in batches (e.g. 100
tracks at a time, review, repeat) resumes where the last run left off
instead of re-covering the same tracks - and so the review screen can
show real progress ("12,400 of 30,000 tracks scanned").

Deliberately scoped to "whole library" scanning only - "recent" is
already a moving window (the N most recently added) and "incoming"
self-narrows as tracks leave that bin, so neither needs a saved
position of its own.

This is purely about which tracks get *scanned* at all (i.e. whether
the slow, rate-limited fetch step even runs for a track this time). It
has nothing to do with, and doesn't change, plan.py's/mood_plan.py's
existing per-tag check - a candidate tag already applied to a track is
always skipped regardless of scan position. That per-tag check stays
the real safety net; this is purely a scan-time optimization plus a
progress readout.

Gitignored, like the plan JSON files - this is disposable working
state, not something to commit or share between machines.
"""

from __future__ import annotations

import json
from pathlib import Path

PROGRESS_FILE = Path(__file__).resolve().parent / "scan_progress.json"

# Matches the "genre"/"mood" kind strings already used throughout
# review_ui.py (KIND_LABELS) and plan.py/mood_plan.py's own docstrings.
ACTIONS = ("genre", "mood")


def _load() -> dict:
    if not PROGRESS_FILE.exists():
        return {}
    try:
        return json.loads(PROGRESS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        # Corrupt/unreadable progress file is worth starting fresh from
        # rather than crashing the whole app over - worst case a scan
        # re-covers ground it didn't need to, which is exactly what
        # happens today anyway (no progress file at all).
        return {}


def _save(data: dict) -> None:
    PROGRESS_FILE.write_text(json.dumps(data, indent=1))


def get_cursor(action: str) -> int | None:
    """Highest track id a prior whole-library scan for `action`
    ("genre" or "mood") actually finished, or None if there isn't one
    yet (first run, or progress was reset) - in which case the next
    scan should start from the beginning, same as always."""
    return _load().get(action, {}).get("last_track_id")


def advance(action: str, last_track_id: int | None) -> None:
    """Record that whole-library scanning for `action` now covers
    every track up to and including `last_track_id`. Pass the id of
    the last track a run actually *finished* - not the id of the last
    track it meant to reach - so a run stopped partway only advances
    as far as real progress, and whatever it didn't get to still shows
    up in the next scan. A None `last_track_id` (nothing was actually
    scanned, e.g. an empty pool or a stop before the first track) is a
    no-op rather than clobbering an existing cursor with nothing.
    """
    if last_track_id is None:
        return
    data = _load()
    data.setdefault(action, {})["last_track_id"] = last_track_id
    _save(data)


def reset(action: str) -> None:
    """Clear the saved position for `action` - the next whole-library
    scan starts over from the beginning. Doesn't touch tags already
    applied to any track; only changes which tracks a future scan
    considers."""
    data = _load()
    if data.pop(action, None) is not None:
        _save(data)
