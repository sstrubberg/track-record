#!/usr/bin/env python3
"""Mood/Theme action: load -> fetch -> score/plan.

Same load -> fetch -> score/plan -> review -> apply shape as plan.py
(the Genre/Subgenre action's own version of this file) - pulls the
Lexicon library, queries the mood/theme fetch source per track, scores
candidates via scoring.py (shared, generic - no changes needed there),
and writes a plan for mood_review_ui.py / mood_apply.py to act on.

A self-contained copy of plan.py's shape rather than a shared module
plan.py imports from - same call this project already made for
billboard_tag.py (see lexicon_client.py's docstring): each action's
own copy stays simple to read end-to-end and safe to change without
touching the others, at the cost of some duplication between this file
and plan.py. Both are short enough that the tradeoff favors reading
simplicity.

Only one fetch source exists for Mood/Theme (see
fetch/audio_model_mood.py) - MusicBrainz/Discogs are genre-and-catalog
databases with essentially no reliable mood data, and LLM web search
stays on hold over API cost for the same reason it does for
Genre/Subgenre. No source-toggle picker here as a result (unlike
review_ui.py's) - there's nothing yet to toggle between.

    python mood_plan.py --limit 5      # try it on the first 5 tracks
    python mood_plan.py --track-id 131 # just one track
    python mood_plan.py                # the whole library
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lexicon_client
import scoring
from fetch import audio_model_mood

PLAN_FILE = Path(__file__).resolve().parent / "mood_plan.json"
WEIGHTS_PATH = Path(__file__).resolve().parent / "config" / "mood_weights.yaml"


def fetch_candidates(track: dict) -> list[dict]:
    location = track.get("location")
    if not location:
        return []
    try:
        return audio_model_mood.fetch_moods(location)
    except Exception as e:
        print(f"    audio_model_mood failed: {e}")
        return []


def plan_track(track: dict, by_label: dict, weights: dict, suggested_category_id: int | None) -> dict:
    candidates = fetch_candidates(track)
    scored = scoring.score_track(candidates, weights)
    current_tag_ids = set(track.get("tags") or [])

    auto_cfg = weights.get("auto_include", {})
    min_conf = auto_cfg.get("min_confidence", 1.0)
    min_sources = auto_cfg.get("min_agreeing_sources", 99)
    low_conf_threshold = weights.get("low_confidence_threshold", 0)

    auto, review, create = [], [], []
    for entry in scored:
        tag_id = lexicon_client.resolve_tag_id(entry["tag"], by_label)

        if tag_id is None:
            # Doesn't exist in this Lexicon library at all - always a
            # review-screen decision, never auto, same rule as Genre/
            # Subgenre. Expect this branch a lot here: the 56 MTG-Jamendo
            # labels are plain English words, not something a library's
            # existing tags are likely to already match exactly.
            create.append({
                "track_id": track["id"],
                "artist": track.get("artist"),
                "title": track.get("title"),
                "tag": entry["tag"],
                "suggested_category_id": suggested_category_id,
                "confidence": round(entry["confidence"], 3),
                "sources": entry["sources"],
                "low_confidence": entry["confidence"] < low_conf_threshold,
            })
            continue

        if tag_id in current_tag_ids:
            continue  # already tagged - nothing to do

        n_sources = len({c["source"] for c in entry["sources"]})
        row = {
            "track_id": track["id"],
            "artist": track.get("artist"),
            "title": track.get("title"),
            "tag": entry["tag"],
            "tag_id": tag_id,
            "confidence": round(entry["confidence"], 3),
            "sources": entry["sources"],
        }

        if entry["confidence"] >= min_conf or n_sources >= min_sources:
            auto.append(row)
        else:
            row["low_confidence"] = entry["confidence"] < low_conf_threshold
            review.append(row)

    return {"auto": auto, "review": review, "create": create}


def _resolve_suggested_category(weights: dict, on_status=None) -> int | None:
    """Purely a convenience default for the review screen's category
    dropdown - every new-tag proposal is always shown there for a
    human decision, regardless of whether this resolves to anything."""
    name = (weights.get("new_tag_category") or "").strip()
    if not name:
        return None
    categories = lexicon_client.fetch_categories()
    match = next((c for c in categories if c["label"].lower() == name.lower()), None)
    if match is None and on_status:
        on_status(
            f"  note: new_tag_category '{name}' not found in Lexicon - no "
            f"default category will be pre-selected in review"
        )
    return match["id"] if match else None


SCAN_MODES = ("all", "recent", "incoming")
DEFAULT_RECENT_COUNT = 20


def generate_plan(
    limit: int | None = None,
    track_id: int | None = None,
    scan_mode: str = "all",
    out_path: str | Path | None = None,
    on_status=None,
    on_track_planned=None,
    should_stop=None,
) -> dict:
    """Runs the whole load -> fetch -> score pipeline in-process and
    writes the plan to disk. Used by both the CLI below and
    mood_review_ui.py's "Generate Plan" button.

    scan_mode picks which tracks: "all" (default, whole library,
    optionally capped by `limit`), "recent" (the `limit` most recently
    added tracks, defaulting to DEFAULT_RECENT_COUNT), or "incoming"
    (everything in Lexicon's Incoming bin, optionally capped).

    on_status(message), if given, is called for one-off progress lines.
    on_track_planned(i, total, track, result), if given, is called once
    per track, after it's been planned - result is plan_track()'s
    return value for that track.

    should_stop(), if given, is checked before starting each track. A
    fetch already in flight for the current track (local audio
    inference) isn't interrupted mid-call - stopping takes effect
    after that track finishes, not instantly. Whatever was already
    planned is still written out and returned as a normal, smaller
    plan rather than discarded.
    """
    if scan_mode not in SCAN_MODES:
        raise ValueError(f"scan_mode must be one of {SCAN_MODES}, got {scan_mode!r}")

    def status(msg):
        if on_status:
            on_status(msg)

    weights = scoring.load_weights(WEIGHTS_PATH)

    status("reading tag index...")
    by_id, by_label = lexicon_client.fetch_tag_index()
    status(f"  {len(by_id)} tags in Lexicon")

    suggested_category_id = _resolve_suggested_category(weights, on_status)

    status("reading library...")
    if scan_mode == "recent":
        tracks = lexicon_client.fetch_library(
            sort=[{"field": "dateAdded", "dir": "desc"}],
            limit=limit or DEFAULT_RECENT_COUNT,
        )
        status(f"  {len(tracks)} most recently added track(s)")
    elif scan_mode == "incoming":
        tracks = lexicon_client.fetch_library(source="incoming")
        status(f"  {len(tracks)} incoming track(s)")
    else:
        tracks = lexicon_client.fetch_library()
        status(f"  {len(tracks)} tracks")

    if track_id is not None:
        tracks = [t for t in tracks if t["id"] == track_id]
    if limit:
        tracks = tracks[:limit]

    status(f"\nplanning {len(tracks)} track(s)...\n")
    auto_all, review_all, create_all = [], [], []
    stopped = False
    for i, track in enumerate(tracks, 1):
        if should_stop and should_stop():
            stopped = True
            status(f"\nstopped after {i - 1}/{len(tracks)} track(s)")
            break
        result = plan_track(track, by_label, weights, suggested_category_id)
        auto_all.extend(result["auto"])
        review_all.extend(result["review"])
        create_all.extend(result["create"])
        if on_track_planned:
            on_track_planned(i, len(tracks), track, result)

    plan = {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stopped_early": stopped,
        "auto": auto_all,
        "review": review_all,
        "create": create_all,
    }
    path = Path(out_path) if out_path else PLAN_FILE
    path.write_text(json.dumps(plan, indent=1))

    status(
        f"\n{len(auto_all)} auto-include, {len(review_all)} need review, "
        f"{len(create_all)} propose a new tag (pick its category in mood_review_ui.py)"
    )
    status(f"plan -> {path}")

    return plan


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None, help="only plan the first N tracks")
    p.add_argument("--track-id", type=int, default=None, help="only plan this one track id")
    p.add_argument(
        "--mode", choices=SCAN_MODES, default="all",
        help="'recent' = --limit most recently added tracks (default 20); "
             "'incoming' = everything in Lexicon's Incoming bin",
    )
    p.add_argument("--out", default=None, help="alternate plan output path")
    args = p.parse_args()

    def on_track_planned(i, total, track, result):
        print(f"[{i}/{total}] {track.get('artist')} - {track.get('title')}")
        for row in result["auto"]:
            print(f"    AUTO    {row['tag']}  ({row['confidence']:.0%})")
        for row in result["review"]:
            flag = " [low confidence]" if row["low_confidence"] else ""
            print(f"    REVIEW  {row['tag']}  ({row['confidence']:.0%}){flag}")
        for row in result["create"]:
            print(f"    CREATE  {row['tag']}  ({row['confidence']:.0%}) - new tag, needs review")

    generate_plan(
        limit=args.limit,
        track_id=args.track_id,
        scan_mode=args.mode,
        out_path=args.out,
        on_status=print,
        on_track_planned=on_track_planned,
    )


if __name__ == "__main__":
    main()
