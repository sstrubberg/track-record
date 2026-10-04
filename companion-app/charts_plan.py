#!/usr/bin/env python3
"""Charts action: load -> fetch -> score/plan.

Same load -> fetch -> score/plan -> review -> apply shape as plan.py /
mood_plan.py - pulls the Lexicon library, matches each track against
charts/billboard_tag.py's own chart cache, scores candidates via
scoring.py (shared, generic - no changes needed there either: a chart
match's fuzzy score becomes one source's ("chart_match") confidence,
same as Mood/Theme's single-source case), and writes a plan for
review_ui.py's Charts tab / charts_apply.py to act on.

A self-contained copy of mood_plan.py's shape, same reasoning as that
file's own docstring on why (each action's own copy stays simple to
read end-to-end and safe to change without touching the others).

charts/billboard_tag.py itself stays untouched (its own README is
explicit about that - a verbatim copy, not a fork) - this imports it
and reuses its stable matching primitives (key(), fallback_key(),
norm_title(), norm_title_flat(), era_label(), is_excluded(),
track_tag_names()) rather than re-implementing that logic by hand. The
double-A-side and parenthetical-subtitle title-variant indexing
billboard_tag.py's own phase_plan() builds inline is chart-wide
preprocessing, not per-track - ported here as _load_chart_index(),
built once per generate_plan() call, same as phase_plan() does.

Two of billboard_tag.py's own module-level path constants (CACHE,
CHART_MAP_FILE) are bare relative Paths, correct only when run from
inside charts/ itself (as its own README instructs). track_record.py
runs from companion-app/, one level up, so _import_billboard_tag()
below re-points both at their real absolute location right after
import - reassigning the constants on the imported module, not editing
billboard_tag.py's own source, so its "stays untouched" promise still
holds.

Genuinely different from Genre/Subgenre and Mood/Theme in one respect:
a chart match doesn't score each tag independently. One fuzzy
artist/title match against the chart cache implies a whole *set* of
chart tags at once (every chart the matched song ever appeared on),
all sharing that one match's score - there's no second, independent
signal per tag the way Discogs vs. an audio model are for Genre/
Subgenre.

Also genuinely different in scan-mode: no resumable scan_progress
cursor. Chart matching is an in-memory fuzzy-lookup against an
already-loaded cache - no per-track network call or audio inference -
so unlike Genre/Subgenre and Mood/Theme, redoing a whole-library run
costs nothing worth avoiding. "all" always covers every track, same as
"recent"/"incoming" already do with no saved position either.

    python charts_plan.py --limit 5      # try it on the first 5 tracks
    python charts_plan.py --track-id 131 # just one track
    python charts_plan.py                # the whole library
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from rapidfuzz import fuzz, process

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lexicon_client
import scoring

CHARTS_DIR = Path(__file__).resolve().parent / "charts"
PLAN_FILE = Path(__file__).resolve().parent / "charts_plan.json"
WEIGHTS_PATH = Path(__file__).resolve().parent / "config" / "charts_weights.yaml"


def _import_billboard_tag():
    """Import charts/billboard_tag.py as `bt`, then re-point its CACHE
    and CHART_MAP_FILE constants at their real absolute location - see
    this module's own docstring for why. Done once, lazily, rather than
    at charts_plan.py's own import time: importing billboard_tag.py
    eagerly at every `import charts_plan` (e.g. from review_ui.py,
    which imports every action's plan module up front) would mean
    review_ui.py's startup always pays billboard_tag.py's own import
    cost and CHART_MAP load, even on a run that never touches Charts.
    """
    sys.path.insert(0, str(CHARTS_DIR))
    import billboard_tag as bt  # noqa: E402

    bt.CACHE = CHARTS_DIR / "billboard_cache.json"
    bt.CHART_MAP_FILE = CHARTS_DIR / "chart_map.json"
    bt.CHART_MAP = bt.load_chart_map()  # re-read now that CHART_MAP_FILE is correct

    # ARTIST_ALIASES corrections for this library - same mechanism
    # billboard_tag.py already ships two entries for ("hall" ->
    # "daryl hall john oates", "janet jackson" -> "janet"), extended
    # here rather than in billboard_tag.py itself (stays untouched).
    # ARTIST_SPLIT treats both a bare comma and "&"/"and" as hard
    # split points - a band whose own name has both ("Earth, Wind &
    # Fire") reduces to a different fragment depending on which
    # spelling shows up: Billboard's own dataset spells it with the
    # comma ("Earth, Wind & Fire" -> splits at the comma -> "earth"),
    # this library spells it without one ("Earth Wind & Fire" ->
    # splits at "&" -> "earth wind") - two different keys for the same
    # band, confirmed directly: only "Boogie Wonderland" partially
    # matched (fuzzy, 90%) while September/Fantasy/Shining Star/Let's
    # Groove matched nothing at all. Aliasing both reduced fragments to
    # the same canonical string fixes matching going forward, but only
    # takes effect on keys computed *after* this patch - the already-
    # built cache still has the old "earth|<title>" keys baked in from
    # before, so refresh_cache() below always re-runs phase_load after
    # this to rebuild them consistently.
    #
    # Collision risk this accepts: a different, unrelated real artist
    # who also normalizes down to "earth" (a band literally named
    # "Earth" exists, e.g.) would get misattributed to Earth, Wind &
    # Fire's chart history instead. Checked directly against this
    # cache's real data - every "earth|..." key already only ever
    # belongs to Earth, Wind & Fire - but a future dataset addition
    # could reintroduce the collision.
    bt.ARTIST_ALIASES["earth"] = "earth wind fire"
    bt.ARTIST_ALIASES["earth wind"] = "earth wind fire"

    return bt


def _load_chart_index(cache_path: Path | None = None, on_status=None) -> tuple[dict, list[str], dict]:
    """One-time per generate_plan() call: load billboard_cache.json and
    build the same double-A-side + parenthetical-subtitle title-variant
    index billboard_tag.py's own phase_plan() builds inline (see
    charts/billboard_tag.py:857-891) - a straight port of that
    preprocessing, just returning the built index instead of going
    straight into a per-track loop. Returns (chart, chart_keys,
    slug_to_label)."""
    bt = _import_billboard_tag()

    def status(msg):
        if on_status:
            on_status(msg)

    cache_path = cache_path or bt.CACHE
    if not cache_path.exists():
        raise FileNotFoundError(
            f"no {cache_path} - run `python billboard_tag.py load` from "
            f"companion-app/charts/ first (or Settings' Chart Cache card)"
        )
    chart = json.loads(cache_path.read_text())

    extra = 0
    for k, rec in list(chart.items()):
        raw = rec.get("title") or ""
        if "/" not in raw:
            continue
        artist_part = k.partition("|")[0]
        for half in raw.split("/"):
            nt = bt.norm_title(half)
            if len(nt) > 3:
                vk = f"{artist_part}|{nt}"
                if vk not in chart:
                    chart[vk] = rec
                    extra += 1
    if extra:
        status(f"  +{extra} double-A-side title variants indexed")

    extra2 = 0
    for k, rec in list(chart.items()):
        raw = rec.get("title") or ""
        if "(" not in raw and "[" not in raw:
            continue
        artist_part = k.partition("|")[0]
        flat = bt.norm_title_flat(raw)
        if len(flat) > 3:
            vk = f"{artist_part}|{flat}"
            if vk not in chart:
                chart[vk] = rec
                extra2 += 1
    if extra2:
        status(f"  +{extra2} parenthetical-subtitle title variants indexed")

    chart_keys = list(chart.keys())
    slug_to_label: dict[str, str] = {}
    for label, slug in bt.CHART_MAP.items():
        slug_to_label.setdefault(slug, label)  # first label wins per slug
    status(f"  {len(chart_keys)} charted songs in cache")

    return chart, chart_keys, slug_to_label


def _secondary_artist_keys(bt, artist: str, title: str) -> list[str]:
    """Lookup keys for every credited artist after the first one, in
    credit order - "Sting & The Police" gives the key for "The Police"
    (the first piece, "Sting", is what bt.key() already tried).
    Splits the same way billboard_tag.py's own norm_artist() does, so
    "The" prefixes and aliases normalize identically to how the cache's
    own keys were built."""
    parts = bt.ARTIST_SPLIT.split(bt.PAREN.sub("", bt._pre(artist)))[1:]
    primary = bt.norm_artist(artist)
    norm_t = bt.norm_title(title)
    keys = []
    for part in parts:
        a = bt.norm_artist(part)
        if a and a != primary:
            k = f"{a}|{norm_t}"
            if k not in keys:
                keys.append(k)
    return keys


def fetch_candidates(
    track: dict, current_tag_names: list[str], chart: dict, chart_keys: list[str], slug_to_label: dict,
) -> tuple[list[dict], str | None]:
    """The actual per-track matching logic billboard_tag.py's own
    phase_plan() has inline (charts/billboard_tag.py:911-943), factored
    out here since this project's shared pipeline shape needs it
    callable per track: exact key lookup -> fuzzy lookup -> fallback
    key exact -> fallback key fuzzy, same order, same
    FUZZ_THRESHOLD (88) cutoff. On a hit, every chart the matched song
    ever appeared on becomes one candidate tag, all sharing that one
    match's score - there's no independent per-tag score the way
    Genre/Subgenre's sources have.

    Returns (candidates, reason) - reason is None when candidates is
    non-empty, otherwise a short machine-readable code
    ("excluded"/"no_match") explaining why, so plan_track() can surface
    *why* a track got nothing rather than a DJ just seeing an empty
    Charts sub-group and wondering if the feature is broken. Unlike
    Genre/Subgenre's sources (which almost always find *something*, if
    only a low-confidence guess), most tracks genuinely have no chart
    appearance at all - that's the normal case here, not the
    exception, so it's worth explaining rather than leaving silent."""
    bt = _import_billboard_tag()

    artist, title = track.get("artist") or "", track.get("title") or ""
    if bt.is_excluded(current_tag_names, title):
        return [], "excluded"

    k = bt.key(artist, title)
    if not k.strip("|"):
        return [], "no_match"

    match_key, score = None, 0
    if k in chart:
        match_key, score = k, 100
    else:
        hit = process.extractOne(k, chart_keys, scorer=fuzz.ratio, score_cutoff=bt.FUZZ_THRESHOLD)
        if hit:
            match_key, score = hit[0], round(hit[1])
        else:
            alt = bt.fallback_key(artist, title)
            if alt and alt in chart:
                match_key, score = alt, 100
            elif alt:
                hit = process.extractOne(alt, chart_keys, scorer=fuzz.ratio, score_cutoff=bt.FUZZ_THRESHOLD)
                if hit:
                    match_key, score = hit[0], round(hit[1])

    if not match_key:
        # Last resort: the *other* credited artists. billboard_tag.py's
        # key() only ever keeps the first piece of a multi-artist credit
        # ("Sting & The Police" -> "sting"), which is right when the
        # first name is the act Billboard filed the song under and wrong
        # when it isn't - Billboard has "Don't Stand So Close To Me"
        # under The Police alone, so a library credit of "Sting & The
        # Police" never looked it up under "police" at all. Aliasing
        # one name to the other (what ARTIST_ALIASES is for) can't fix
        # this one: Sting has plenty of solo entries in the same cache,
        # so "sting" -> "police" would hand them The Police's chart
        # history. Only reached after every lookup above found
        # nothing, and the title still has to match, so a different
        # artist's same-named song is the only way to get this wrong.
        for alt in _secondary_artist_keys(bt, artist, title):
            if alt in chart:
                match_key, score = alt, 100
                break
            hit = process.extractOne(alt, chart_keys, scorer=fuzz.ratio, score_cutoff=bt.FUZZ_THRESHOLD)
            if hit:
                match_key, score = hit[0], round(hit[1])
                break

    if not match_key:
        return [], "no_match"

    rec = chart[match_key]
    labels = set()
    for slug, info in rec["charts"].items():
        label = bt.era_label(slug, info["first"], slug_to_label)
        if label:
            labels.add(label)

    return [{"tag": label, "score": score / 100, "source": "chart_match"} for label in sorted(labels)], None


def plan_track(
    track: dict, by_id: dict, by_label: dict, weights: dict, suggested_category_id: int | None,
    chart: dict, chart_keys: list[str], slug_to_label: dict,
) -> dict:
    bt = _import_billboard_tag()
    current_tag_names = bt.track_tag_names(track, by_id)
    candidates, no_match_reason = fetch_candidates(track, current_tag_names, chart, chart_keys, slug_to_label)
    scored = scoring.score_track(candidates, weights)
    current_tag_ids = set(track.get("tags") or [])

    auto_cfg = weights.get("auto_include", {})
    min_conf = auto_cfg.get("min_confidence", 1.0)
    min_sources = auto_cfg.get("min_agreeing_sources", 99)
    low_conf_threshold = weights.get("low_confidence_threshold", 0)

    auto, review, create = [], [], []
    already_tagged: list[str] = []
    for entry in scored:
        tag_id = lexicon_client.resolve_tag_id(entry["tag"], by_label)

        if tag_id is None:
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
            already_tagged.append(entry["tag"])
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

    # Nothing new to propose - but *why* matters, since (unlike Genre/
    # Subgenre or Mood/Theme) a genuine "this track just isn't on any
    # chart" is the normal outcome here, not a sign something's broken.
    # Surfaced in the review screen as a small note next to a track
    # that has candidates from another action but none from Charts -
    # see review_ui.py's use of plan["no_match"].
    no_match = None
    if not auto and not review and not create:
        if no_match_reason == "excluded":
            reason = "title looks like a mashup/transition/blend - skipped"
        elif already_tagged:
            reason = f"already tagged: {', '.join(sorted(set(already_tagged)))}"
        else:
            reason = "no chart match found"
        no_match = {"track_id": track["id"], "artist": track.get("artist"), "title": track.get("title"), "reason": reason}

    return {"auto": auto, "review": review, "create": create, "no_match": no_match}


def _resolve_suggested_category(weights: dict, on_status=None) -> int | None:
    """Same as mood_plan.py's own - purely a convenience default for
    the review screen's category dropdown."""
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
    """Runs the whole load(cache) -> match -> score pipeline in-process
    and writes the plan to disk. Used by both the CLI below and
    review_ui.py's Charts tab's "Generate Plan" button.

    scan_mode picks which tracks: "all" (default, whole library,
    optionally capped by `limit`), "recent" (the `limit` most recently
    added tracks, defaulting to DEFAULT_RECENT_COUNT), or "incoming"
    (everything in Lexicon's Incoming bin, optionally capped).

    Deliberately no since_track_id / resumable-cursor support, unlike
    plan.py's/mood_plan.py's own generate_plan() - see this module's
    own docstring for why a saved scan position isn't worth it here.

    on_status(message), if given, is called for one-off progress lines.
    on_track_planned(i, total, track, result), if given, is called once
    per track, after it's been planned - result is plan_track()'s
    return value for that track.

    should_stop(), if given, is checked before starting each track.
    Whatever was already planned is still written out and returned as
    a normal, smaller plan rather than discarded.
    """
    if scan_mode not in SCAN_MODES:
        raise ValueError(f"scan_mode must be one of {SCAN_MODES}, got {scan_mode!r}")

    def status(msg):
        if on_status:
            on_status(msg)

    weights = scoring.load_weights(WEIGHTS_PATH)

    status("loading chart cache...")
    chart, chart_keys, slug_to_label = _load_chart_index(on_status=on_status)

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
    auto_all, review_all, create_all, no_match_all = [], [], [], []
    stopped = False
    for i, track in enumerate(tracks, 1):
        if should_stop and should_stop():
            stopped = True
            status(f"\nstopped after {i - 1}/{len(tracks)} track(s)")
            break
        result = plan_track(track, by_id, by_label, weights, suggested_category_id, chart, chart_keys, slug_to_label)
        auto_all.extend(result["auto"])
        review_all.extend(result["review"])
        create_all.extend(result["create"])
        if result["no_match"]:
            no_match_all.append(result["no_match"])
        if on_track_planned:
            on_track_planned(i, len(tracks), track, result)

    plan = {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stopped_early": stopped,
        "auto": auto_all,
        "review": review_all,
        "create": create_all,
        # One entry per scanned track that got nothing - not shown as
        # its own row (there's no tag to check), just why, for a track
        # that already shows in the review list via another action.
        # See review_ui.py's use of this.
        "no_match": no_match_all,
    }
    path = Path(out_path) if out_path else PLAN_FILE
    path.write_text(json.dumps(plan, indent=1))

    status(
        f"\n{len(auto_all)} auto-include, {len(review_all)} need review, "
        f"{len(create_all)} propose a new tag (pick its category in review_ui.py), "
        f"{len(no_match_all)} no chart data at all"
    )
    status(f"plan -> {path}")

    return plan


# How far back a non-full-history fetch checks. Exists because of a
# real quirk in billboard_tag.py's own week_dates() (untouched, so
# worked around here rather than fixed there): it always generates its
# list of weeks-to-check by counting backward from *today* in fixed
# STEP_WEEKS steps, with no awareness of when a previous fetch actually
# ran. Unless today happens to be an exact STEP_WEEKS-multiple away
# from the last run, none of the newly generated dates line up with
# what billboard_cache.progress.json already has recorded as done -
# confirmed directly: a fetch four weeks after a real one, when the
# cadence didn't land exactly on a 2-week boundary, showed literally
# zero overlap between the two date lists, meaning phase_fetch would
# treat the entire 1958-forward history as still needed every time,
# regardless of how recently it last ran. Capping the start year to
# recent history sidesteps the mismatch (any real new chart weeks are
# recent by definition) for a small fraction of a full pass - measured
# directly: full history 30,050 requests/~28 hours vs. 2-years-back
# 626 requests/~35 min, on this project's own cache.
RECENT_FETCH_YEARS_BACK = 2


def _fetch_start_year(bt, full_history: bool) -> int:
    return bt.START_YEAR if full_history else date.today().year - RECENT_FETCH_YEARS_BACK


def estimate_fetch(only_slugs: str | None = None, full_history: bool = False) -> dict:
    """Read-only preview of what refresh_cache(fetch=True, ...) would
    actually have to do right now - no network calls, no confirm
    needed to run this part. Mirrors phase_fetch's own
    weeks-still-needed computation (charts/billboard_tag.py:704-717)
    by calling its real functions (week_dates, _load_progress) rather
    than reimplementing the logic by hand - just stops short of
    actually fetching anything.

    full_history=False (the default) only checks back to
    RECENT_FETCH_YEARS_BACK years ago - see that constant's own comment
    for why. full_history=True checks the real 1958-forward range, a
    full cold-start-equivalent pass - only actually worth it right
    after adding a brand-new chart to chart_map.json that's never been
    fetched at all, not for a routine "it's been a few weeks" update.

    Returns {"slugs": int, "total_requests": int, "estimated_seconds": float}.
    """
    bt = _import_billboard_tag()
    progress = bt._load_progress(bt.CACHE)
    start_year = _fetch_start_year(bt, full_history)

    if only_slugs:
        wanted = {x.strip() for x in only_slugs.split(",")}
        slugs = sorted(wanted)
    else:
        slugs = sorted(set(bt.CHART_MAP.values()) - set(bt.DATASET_SOURCES))

    def weeks_still_needed(slug):
        step = bt.STEP_WEEKS_BY_SLUG.get(slug, bt.STEP_WEEKS)
        weeks = bt.week_dates(start_year, step)
        done = progress.get(slug, set())
        return sum(1 for d in weeks if d.isoformat() not in done)

    total = sum(weeks_still_needed(slug) for slug in slugs)
    seconds = total * bt.SECONDS_PER_WEEK / max(1, bt.WORKERS * 0.55)
    return {"slugs": len(slugs), "total_requests": total, "estimated_seconds": seconds}


def refresh_cache(fetch: bool = False, full_history: bool = False, on_status=None) -> None:
    """What Settings' "Update Chart Cache" button calls via
    run.io_bound. `fetch=False` (the default) runs billboard_tag.py's
    own phase_load - seconds, dataset-backed charts only. `fetch=True`
    instead runs phase_fetch - a real scrape of Billboard.com directly.
    `full_history` (only meaningful with fetch=True) picks how far back
    it checks - see estimate_fetch()'s own docstring; review_ui.py
    shows both estimates and only ever calls this after an explicit
    confirm, never on its own.

    Temporarily chdirs into charts/ for the duration of the call:
    phase_load's own dataset-CSV caching uses bare relative filenames
    (Path(f"dataset_{slug}.csv")), correct only relative to charts/
    itself - see this module's own docstring on why billboard_tag.py
    stays untouched rather than being edited to take an explicit path.
    """
    import os

    def status(msg):
        if on_status:
            on_status(msg)

    bt = _import_billboard_tag()
    cwd = os.getcwd()
    try:
        os.chdir(CHARTS_DIR)
        if fetch:
            start_year = _fetch_start_year(bt, full_history)
            status(f"scraping Billboard.com directly ({start_year} onward)...")
            bt.phase_fetch(cache_path=bt.CACHE, start_year=start_year)
        else:
            status("loading bulk chart datasets...")
            bt.phase_load(cache_path=bt.CACHE)
    finally:
        os.chdir(cwd)
    status("chart cache refreshed" + (" (fetch)" if fetch else " (load)"))


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
        if result["no_match"]:
            print(f"    ---     {result['no_match']['reason']}")

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
