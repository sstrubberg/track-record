#!/usr/bin/env python3
"""Genre/Subgenre action: load -> fetch -> score/plan.

Pulls the Lexicon library, queries every available fetch source per
track (Discogs, two independent local audio models - llm_web_search is
on hold, see its own module), scores the combined candidates via
scoring.py, and writes a plan for review_ui.py / apply.py to act on.
Mirrors billboard_tag.py's load/fetch/plan/apply shape, but as
separate files instead of one script - see charts/README.md.

MusicBrainz was a fetch source here too, through 2026-08-07 - dropped
after this project's own DJ found its suggestions consistently
disappointing in day-to-day use (fetch/musicbrainz.py itself is
untouched and still works standalone; it's just no longer wired in
here). audio_model_genre_effnet (genre_discogs400, see that module's
own docstring) took its slot as the third source instead - same
Discogs 400-style taxonomy as audio_model (discogs-maest), but a
genuinely different architecture (EfficientNet vs. transformer), so it
corroborates rather than just re-asking the same model. With
min_agreeing_sources: 2, "2 sources agree" can now mean either audio
model agreeing with Discogs, or the two audio models agreeing with
each other - not tied to Discogs the way it would be with only one
audio source in the mix.

    python plan.py --limit 5              # try it on the first 5 tracks
    python plan.py --track-id 131         # just one track
    python plan.py                        # the whole library
    python plan.py --sources discogs,audio_model  # skip the effnet genre model
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import genre_family_hint
import lexicon_client
import scan_progress
import scoring
from fetch import audio_model, audio_model_genre_effnet, discogs

PLAN_FILE = Path(__file__).resolve().parent / "genre_plan.json"

# llm_web_search isn't in here - it's a stub, on hold over API cost, and
# was never wired into fetch_candidates() below in the first place.
# These names are also what review_ui.py's source-toggle checkboxes key
# off of, and what --sources on the CLI accepts.
SOURCES = ("discogs", "audio_model", "audio_model_genre_effnet")


def fetch_candidates(track: dict, enabled_sources: set[str] | None = None) -> list[dict]:
    """`enabled_sources`, if given, restricts which fetch sources actually
    run for this track - None (the default) means all of them, same as
    before this existed. Letting a DJ turn a source off entirely (not
    just down-weight it in source_weights.yaml) is useful on its own -
    e.g. skipping both audio models for a fast metadata-only pass, since
    local inference is by far the slowest part of a run."""
    if enabled_sources is None:
        enabled_sources = set(SOURCES)

    artist, title, location = track.get("artist") or "", track.get("title") or "", track.get("location")
    candidates = []

    if (artist or title) and "discogs" in enabled_sources:
        try:
            candidates.extend(discogs.fetch_genres(artist, title))
        except Exception as e:
            print(f"    discogs failed: {e}")

    if location and "audio_model" in enabled_sources:
        try:
            candidates.extend(audio_model.fetch_genres(location))
        except Exception as e:
            print(f"    audio_model failed: {e}")

    if location and "audio_model_genre_effnet" in enabled_sources:
        try:
            candidates.extend(audio_model_genre_effnet.fetch_genres(location))
        except Exception as e:
            print(f"    audio_model_genre_effnet failed: {e}")

    return candidates


def plan_track(
    track: dict,
    by_label: dict,
    weights: dict,
    suggested_category_id: int | None,
    enabled_sources: set[str] | None = None,
    family_hints: dict[str, str] | None = None,
    family_category_ids: dict[str, int] | None = None,
) -> dict:
    candidates = fetch_candidates(track, enabled_sources)
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
            # Doesn't exist in this Lexicon library at all. Always a
            # review-screen decision, never auto - creating a tag is a
            # bigger action than adding an existing one. Which category
            # it goes in is picked in the review UI, not here - this is
            # only ever a pre-filled default. A per-tag family match
            # (e.g. "P.Funk" -> Sub-genre - Funk / Soul) wins over the
            # flat new_tag_category default when one's available; falls
            # back to that default otherwise (no match, ambiguous name,
            # or the matched family has no category in Lexicon yet -
            # see genre_family_hint.py).
            family = (family_hints or {}).get(lexicon_client._normalize_label(entry["tag"]))
            family_category_id = (family_category_ids or {}).get(family) if family else None
            create.append({
                "track_id": track["id"],
                "artist": track.get("artist"),
                "title": track.get("title"),
                "tag": entry["tag"],
                "suggested_category_id": (
                    family_category_id if family_category_id is not None else suggested_category_id
                ),
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


def _resolve_suggested_category(weights: dict, categories: list[dict], on_status=None) -> int | None:
    """Purely a convenience default for the review screen's category
    dropdown - every new-tag proposal is always shown there for a
    human decision, regardless of whether this resolves to anything.
    The fallback when a tag's name doesn't match anything in
    genre_family_hint.py's per-family lookup (or matches a family with
    no category in Lexicon yet)."""
    name = (weights.get("new_tag_category") or "").strip()
    if not name:
        return None
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
    enabled_sources: set[str] | None = None,
    out_path: str | Path | None = None,
    on_status=None,
    on_track_planned=None,
    should_stop=None,
    since_track_id: int | None = None,
) -> dict:
    """Runs the whole load -> fetch -> score pipeline in-process and
    writes the plan to disk. Used by both the CLI below and
    review_ui.py's "Generate Plan" button - one implementation either
    way calls into.

    scan_mode picks which tracks: "all" (default, whole library,
    optionally capped by `limit`), "recent" (the `limit` most recently
    added tracks, defaulting to DEFAULT_RECENT_COUNT), or "incoming"
    (everything in Lexicon's Incoming bin, optionally capped).

    enabled_sources restricts which fetch sources run at all (see
    fetch_candidates()) - None (the default) means every source in
    SOURCES, same as always. An empty set is allowed (every track plans
    to nothing) rather than rejected - the caller's problem to guard
    against if that's not wanted, same as any other empty scan.

    since_track_id, if given, drops every track with id <= this value
    before applying `limit` - the mechanism behind resuming a batched
    whole-library scan (see scan_progress.py) without this function
    knowing anything about that module itself; a caller working through
    a big library 100 tracks at a time passes the id the last run
    finished at, and gets the next 100 instead of the same first 100
    again. Ignored when `track_id` is also given - an explicit single-
    track lookup always runs regardless of scan position. Meaningful
    for any scan_mode, but only worth using with "all" in practice -
    "recent"/"incoming" are naturally-scoped pools with no analogous
    idea of a saved position.

    on_status(message), if given, is called for one-off progress lines
    (tag index size, library size, ...). on_track_planned(i, total,
    track, result), if given, is called once per track, after it's been
    planned - result is plan_track()'s return value for that track.

    should_stop(), if given, is checked before starting each track. A
    fetch already in flight for the current track (a network call, or
    audio-model inference) isn't interrupted mid-call - stopping takes
    effect after that track finishes, not instantly. Whatever was
    already planned is still written out and returned as a normal,
    smaller plan rather than discarded.
    """
    if scan_mode not in SCAN_MODES:
        raise ValueError(f"scan_mode must be one of {SCAN_MODES}, got {scan_mode!r}")
    if enabled_sources is not None and not set(enabled_sources) <= set(SOURCES):
        raise ValueError(f"enabled_sources must be a subset of {SOURCES}, got {enabled_sources!r}")

    def status(msg):
        if on_status:
            on_status(msg)

    weights = scoring.load_weights()

    status("reading tag index...")
    by_id, by_label = lexicon_client.fetch_tag_index()
    status(f"  {len(by_id)} tags in Lexicon")

    categories = lexicon_client.fetch_categories()
    suggested_category_id = _resolve_suggested_category(weights, categories, on_status)
    # Per-tag family suggestion (see genre_family_hint.py's own
    # docstring for how this differs from the removed Reorganize
    # workflow) - built once per run, same as the flat default above,
    # not recomputed per track.
    family_hints = genre_family_hint.build_family_hints()
    family_category_ids = genre_family_hint.resolve_family_category_ids(family_hints, categories)

    active = sorted(enabled_sources) if enabled_sources is not None else list(SOURCES)
    status(f"  sources: {', '.join(active) if active else '(none)'}")

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
        # Explicit ascending-id order rather than relying on Lexicon's
        # implicit "database order" (which the caller can't rely on
        # staying consistent) - since_track_id only means "the next N
        # tracks after this one" if every run agrees on what "next"
        # means.
        tracks.sort(key=lambda t: t["id"])
        status(f"  {len(tracks)} tracks")

    if track_id is not None:
        tracks = [t for t in tracks if t["id"] == track_id]
    elif since_track_id is not None:
        tracks = [t for t in tracks if t["id"] > since_track_id]
    if limit:
        tracks = tracks[:limit]

    status(f"\nplanning {len(tracks)} track(s)...\n")
    auto_all, review_all, create_all = [], [], []
    stopped = False
    last_scanned_track_id = None
    for i, track in enumerate(tracks, 1):
        if should_stop and should_stop():
            stopped = True
            status(f"\nstopped after {i - 1}/{len(tracks)} track(s)")
            break
        result = plan_track(
            track, by_label, weights, suggested_category_id, enabled_sources,
            family_hints, family_category_ids,
        )
        auto_all.extend(result["auto"])
        review_all.extend(result["review"])
        create_all.extend(result["create"])
        last_scanned_track_id = track["id"]
        if on_track_planned:
            on_track_planned(i, len(tracks), track, result)

    plan = {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stopped_early": stopped,
        "auto": auto_all,
        "review": review_all,
        "create": create_all,
        # Highest id actually finished this run (None if nothing was) -
        # scan_progress.py's cursor advances to exactly this, not to
        # wherever the run merely intended to reach, so a stop partway
        # through still leaves the untouched remainder to show up next
        # time. Only meaningful for scan_mode "all"; harmless either way
        # for "recent"/"incoming" since nothing consumes it there.
        "last_scanned_track_id": last_scanned_track_id,
    }
    path = Path(out_path) if out_path else PLAN_FILE
    path.write_text(json.dumps(plan, indent=1))

    status(
        f"\n{len(auto_all)} auto-include, {len(review_all)} need review, "
        f"{len(create_all)} propose a new tag (pick its category in review_ui.py)"
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
    p.add_argument(
        "--sources", default=None,
        help=f"comma-separated subset of {{{','.join(SOURCES)}}} to query "
             f"(default: all of them)",
    )
    p.add_argument("--out", default=None, help="alternate plan output path")
    p.add_argument(
        "--resume", action="store_true",
        help="skip tracks a previous --mode all run already covered (see "
             "scan_progress.py) and save how far this run gets, so the next "
             "--resume run picks up after it; ignored for --mode recent/incoming",
    )
    p.add_argument(
        "--reset-progress", action="store_true",
        help="clear the saved --resume position for this action before running "
             "(combine with --resume to start a fresh pass); does not touch tags "
             "already applied to any track",
    )
    args = p.parse_args()

    if args.reset_progress:
        scan_progress.reset("genre")

    enabled_sources = set(args.sources.split(",")) if args.sources else None

    def on_track_planned(i, total, track, result):
        print(f"[{i}/{total}] {track.get('artist')} - {track.get('title')}")
        for row in result["auto"]:
            print(f"    AUTO    {row['tag']}  ({row['confidence']:.0%})")
        for row in result["review"]:
            flag = " [low confidence]" if row["low_confidence"] else ""
            print(f"    REVIEW  {row['tag']}  ({row['confidence']:.0%}){flag}")
        for row in result["create"]:
            print(f"    CREATE  {row['tag']}  ({row['confidence']:.0%}) - new tag, needs review")

    since_track_id = scan_progress.get_cursor("genre") if args.resume and args.mode == "all" else None

    plan = generate_plan(
        limit=args.limit,
        track_id=args.track_id,
        scan_mode=args.mode,
        enabled_sources=enabled_sources,
        out_path=args.out,
        on_status=print,
        on_track_planned=on_track_planned,
        since_track_id=since_track_id,
    )

    if args.resume and args.mode == "all":
        scan_progress.advance("genre", plan.get("last_scanned_track_id"))


if __name__ == "__main__":
    main()
