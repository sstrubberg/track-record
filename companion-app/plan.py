#!/usr/bin/env python3
"""Genre/Subgenre action: load -> fetch -> score/plan.

Pulls the Lexicon library, queries every available fetch source per
track (MusicBrainz, Discogs, the local audio model - llm_web_search is
on hold, see its own module), scores the combined candidates via
scoring.py, and writes a plan for review_ui.py / apply.py to act on.
Mirrors billboard_tag.py's load/fetch/plan/apply shape, but as
separate files instead of one script - see charts/README.md.

    python plan.py --limit 5      # try it on the first 5 tracks
    python plan.py --track-id 131 # just one track
    python plan.py                # the whole library
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
from fetch import audio_model, discogs, musicbrainz

PLAN_FILE = Path(__file__).resolve().parent / "genre_plan.json"


def fetch_candidates(track: dict) -> list[dict]:
    artist, title, location = track.get("artist") or "", track.get("title") or "", track.get("location")
    candidates = []

    if artist or title:
        for name, fn in (("musicbrainz", musicbrainz.fetch_genres), ("discogs", discogs.fetch_genres)):
            try:
                candidates.extend(fn(artist, title))
            except Exception as e:
                print(f"    {name} failed: {e}")

    if location:
        try:
            candidates.extend(audio_model.fetch_genres(location))
        except Exception as e:
            print(f"    audio_model failed: {e}")

    return candidates


def plan_track(track: dict, by_label: dict, weights: dict) -> dict:
    candidates = fetch_candidates(track)
    scored = scoring.score_track(candidates, weights)
    current_tag_ids = set(track.get("tags") or [])

    auto_cfg = weights.get("auto_include", {})
    min_conf = auto_cfg.get("min_confidence", 1.0)
    min_sources = auto_cfg.get("min_agreeing_sources", 99)
    low_conf_threshold = weights.get("low_confidence_threshold", 0)

    auto, review, unresolved = [], [], []
    for entry in scored:
        tag_id = lexicon_client.resolve_tag_id(entry["tag"], by_label)
        if tag_id is None:
            unresolved.append(entry)
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

    return {"auto": auto, "review": review, "unresolved": unresolved}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--limit", type=int, default=None, help="only plan the first N tracks")
    p.add_argument("--track-id", type=int, default=None, help="only plan this one track id")
    p.add_argument("--out", default=None, help="alternate plan output path")
    args = p.parse_args()

    weights = scoring.load_weights()

    print("reading tag index...")
    by_id, by_label = lexicon_client.fetch_tag_index()
    print(f"  {len(by_id)} tags in Lexicon")

    print("reading library...")
    tracks = lexicon_client.fetch_library()
    print(f"  {len(tracks)} tracks")

    if args.track_id is not None:
        tracks = [t for t in tracks if t["id"] == args.track_id]
    if args.limit:
        tracks = tracks[: args.limit]

    print(f"\nplanning {len(tracks)} track(s)...\n")
    auto_all, review_all, unresolved_all = [], [], []
    for i, track in enumerate(tracks, 1):
        print(f"[{i}/{len(tracks)}] {track.get('artist')} - {track.get('title')}")
        result = plan_track(track, by_label, weights)
        auto_all.extend(result["auto"])
        review_all.extend(result["review"])
        unresolved_all.extend(result["unresolved"])
        for row in result["auto"]:
            print(f"    AUTO    {row['tag']}  ({row['confidence']:.0%})")
        for row in result["review"]:
            flag = " [low confidence]" if row["low_confidence"] else ""
            print(f"    REVIEW  {row['tag']}  ({row['confidence']:.0%}){flag}")

    out_path = Path(args.out) if args.out else PLAN_FILE
    out_path.write_text(
        json.dumps(
            {
                "version": 1,
                "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "auto": auto_all,
                "review": review_all,
            },
            indent=1,
        )
    )

    print(f"\n{len(auto_all)} auto-include, {len(review_all)} need review")
    print(f"plan -> {out_path}")

    unresolved_tags = sorted({e["tag"] for e in unresolved_all})
    if unresolved_tags:
        print(
            f"\n{len(unresolved_tags)} tag(s) suggested but not in your Lexicon "
            f"library (create them yourself if you want them; this tool never "
            f"creates tags):"
        )
        for t in unresolved_tags:
            print(f"  {t}")


if __name__ == "__main__":
    main()
