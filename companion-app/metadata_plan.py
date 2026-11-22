#!/usr/bin/env python3
"""Metadata action: plan - Album Title, Year and Genre fixes.

The fourth action next to Genre/Subgenre, Mood/Theme and Charts, but a
different kind of thing: those *add tags* and are scored by confidence;
this *replaces a field's value* (album, year, genre) with a better one,
so there is no auto-include bucket - every row is a suggestion for a
person to accept, edit or skip, and nothing is pre-checked.

What it looks for, per track:

- Album / Year. DJ-pool and streaming-rip libraries are full of albums
  that are really a compilation, a karaoke disc, a "(Sped Up)" single or
  the literal "Untitled", and of years to match (1952 is the classic
  placeholder). The convention this checks against: Year is the year the
  song *first came out*, and Album is the original studio album - or the
  song's own single/EP where it never had one.

  Year is the field that matters, and no single database reliably knows
  it (fetch/release_years.py: iTunes, MusicBrainz and Discogs are each
  right about 7 times in 10, and tend to be wrong in different ways). So
  it is judged by agreement. The current year is left alone if ANY
  source is within a year of it - one source missing the original (B4-4
  "Get Down": filed 2000, MusicBrainz 2000, iTunes 2003) is not evidence
  the year is wrong. A year row appears only when two or more sources
  answered and none supports the current year, or the year is implausible
  / a placeholder. Its suggestion is the year at least two sources agree
  on (the row says how strongly); if they disagree with each other, or
  the title is a remix/version (the databases describe the original
  song), the row has no suggestion and the box is left for you.

  Album is MusicBrainz's original album (fetch/musicbrainz_release.py),
  suggested only when the current one is empty or reads like junk
  (JUNK_ALBUM_RE). An album that merely differs in name from
  MusicBrainz's is left alone - both are defensible, and it is the
  commonest false alarm. A junk album with no MusicBrainz match gets a
  row with no suggestion (new = None) so it can be typed by hand.

- Genre. Purely local - no lookup. Genre is a loose field (this library
  has ~45 distinct values), so only two things are flagged: a value that
  looks like a pasted tag list rather than a genre ("2000s RnB Pop MSmid
  CBmid"), and a value that is out of line with the *same artist's other
  tracks already filed in the library* (Usher: 14 R&B/Soul/Funk, this
  one "Hip-Hop/Rap"). The suggestion is that artist's usual genre.

Every row carries `field` (the Lexicon field name), `old`, `new`, and
`tag` (== field - the review screen keys its per-row state on
(track_id, kind, tag), and this lets metadata rows share that scheme).
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

import lexicon_client
from fetch import musicbrainz as mb
from fetch import musicbrainz_release as mbr
from fetch import release_years as ry

PLAN_FILE = Path(__file__).resolve().parent / "metadata_plan.json"
SCAN_MODES = ("all", "recent", "incoming")
DEFAULT_RECENT_COUNT = 20

FIELD_LABELS = {"albumTitle": "Album", "year": "Year", "genre": "Genre"}

# A source "supports" the current Year if it is within this many years of
# it: single-vs-album release dates and regional releases differ by a
# year constantly, and that is no reason to rewrite a year.
YEAR_TOLERANCE = 1

# Genre: an artist needs at least this many *filed* tracks, with at
# least this share of them in one genre, before "out of line with their
# other tracks" counts as evidence.
ARTIST_MIN_TRACKS = 2
ARTIST_MIN_SHARE = 0.7

_DECADE_RE = re.compile(r"\b(?:19|20)?\d0s\b", re.I)
# Words that show up in pasted tag-dump "genres" but not real ones.
_TAGDUMP_WORDS = re.compile(r"\b(?:msmid|cbmid|cbhigh|yachtrk|nudisco|electropop|rnb|softrock)\b", re.I)

# Fallback when a junk genre string's own artist has no filed tracks to
# learn from: the first keyword here found in the string picks the genre
# (rap listed before R&B/funk, since a tag dump naming both is usually a
# rapper with a funky sound, not the other way round).
_KEYWORD_GENRES = [
    (re.compile(r"\brap\b|hip.?hop", re.I), "Hip-Hop/Rap"),
    (re.compile(r"\brnb\b|r&b|\bsoul\b|\bfunk\b", re.I), "R&B/Soul/Funk"),
    (re.compile(r"\bcountry\b", re.I), "Country"),
    (re.compile(r"\brock\b", re.I), "Rock"),
    (re.compile(r"\bhouse\b", re.I), "House"),
    (re.compile(r"\bpop\b", re.I), "Pop"),
]


def _artist_key(artist: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", mb._primary_artist(artist or "").lower())


def build_genre_stats(library: list[dict]) -> dict:
    """What the filed library says about genres, built once per run
    from tracks NOT in the Incoming bin (those are the ones being
    fixed - they can't vouch for themselves)."""
    overall: Counter = Counter()
    per_artist: dict[str, Counter] = defaultdict(Counter)
    for t in library:
        if t.get("incoming"):
            continue
        g = (t.get("genre") or "").strip()
        if not g:
            continue
        overall[g] += 1
        per_artist[_artist_key(t.get("artist"))][g] += 1
    return {"overall": overall, "per_artist": per_artist}


def _looks_like_tag_dump(genre: str, stats: dict) -> bool:
    if stats["overall"].get(genre, 0) >= 3:
        return False  # an established value, however odd it looks
    return bool(_DECADE_RE.search(genre) or _TAGDUMP_WORDS.search(genre) or len(genre.split()) >= 4)


# Spellings this library already uses interchangeably (counts from the
# filed library: Hip-Hop 211 / Hip-Hop/Rap 91, R&B 161 / R&B/Soul/Funk
# 197, Funk 41) - not a mismatch when one is compared with another.
_FAMILIES = {
    "hip-hop": "rap", "hip-hop/rap": "rap", "rap": "rap",
    "r&b": "rnb", "r&b/soul/funk": "rnb", "soul": "rnb", "funk": "rnb",
}


def _family(genre: str) -> str:
    return _FAMILIES.get(genre.strip().lower(), genre.strip().lower())


def _usual_genre(usual: Counter) -> tuple[str, float, int]:
    """(most common raw value of the artist's most common family, that
    family's share of their filed tracks, their filed-track count)."""
    n = sum(usual.values())
    fam_counts: Counter = Counter()
    for g, c in usual.items():
        fam_counts[_family(g)] += c
    top_family, top_count = fam_counts.most_common(1)[0]
    raw = max((g for g in usual if _family(g) == top_family), key=lambda g: usual[g])
    return raw, top_count / n, n


def suggest_genre(track: dict, stats: dict) -> dict | None:
    """A genre row, or None if the current value is fine / nothing to go on."""
    current = (track.get("genre") or "").strip()
    usual = stats["per_artist"].get(_artist_key(track.get("artist")))
    mode, share, n = (_usual_genre(usual) if usual else (None, 0.0, 0))

    if current and _looks_like_tag_dump(current, stats):
        if mode and share >= 0.5:
            new, why = mode, f"looks like a tag list, not a genre; this artist's other {n} filed track(s) are mostly {mode}"
        else:
            new = next((g for rx, g in _KEYWORD_GENRES if rx.search(current)), None)
            why = "looks like a tag list, not a genre" + (f" - suggesting {new} from its keywords" if new else " - type the genre")
        return _row(track, "genre", current, new, 0.6 if new else 0.0, why, "library")

    if not current:
        if mode:
            return _row(track, "genre", current, mode, min(0.9, share), f"empty; this artist's {n} filed track(s) are mostly {mode}", "library")
        return None

    if mode and n >= ARTIST_MIN_TRACKS and share >= ARTIST_MIN_SHARE and _family(current) != _family(mode):
        return _row(
            track, "genre", current, mode, share * 0.8,
            f"out of line with this artist's other filed tracks ({round(share * n)} of {n} are {mode}-ish)", "library",
        )
    return None


def _row(track: dict, field: str, old, new, confidence: float, reason: str, source: str, url: str | None = None,
         sources: list[dict] | None = None) -> dict:
    return {
        "track_id": track["id"],
        "artist": track.get("artist") or "",
        "title": track.get("title") or "",
        "field": field,
        "tag": field,  # review_ui keys per-row state on (track_id, kind, tag)
        "old": old,
        "new": new,
        "confidence": round(confidence, 3),
        "reason": reason,
        "sources": sources if sources is not None else [{"source": source, "note": reason, "url": url}],
    }


# Years that are placeholders in this library, not real release years
# (the "Untitled" / 1952 rips).
PLACEHOLDER_YEARS = {1952}


def _year_implausible(year) -> bool:
    try:
        y = int(year)
    except (TypeError, ValueError):
        return True
    return y < 1900 or y > date.today().year + 1 or y in PLACEHOLDER_YEARS


def suggest_album_year(track: dict, info: dict | None) -> list[dict]:
    """Album/Year rows from the track's current values + what the year
    sources found. `info` is fetch/release_years.lookup()'s answer
    ({"years", "release", "recording_id", "unavailable"}) or None. See the
    module docstring for the rules."""
    rows: list[dict] = []
    info = info or {}
    release = info.get("release")
    years = info.get("years") or {}
    cons = ry.consensus(years)
    values = cons["values"]
    album = (track.get("albumTitle") or "").strip()
    year = track.get("year")
    title = track.get("title") or ""

    album_junk = (not album) or bool(mbr.JUNK_ALBUM_RE.search(album))
    year_junk = year in (None, "", 0) or _year_implausible(year)

    mb_url = f"https://musicbrainz.org/recording/{info['recording_id']}" if info.get("recording_id") else None
    sources = [
        {"source": name, "note": f"{ry.SOURCE_LABELS[name]}: {years[name] if years[name] is not None else 'nothing'}",
         "url": mb_url if name == "musicbrainz" else None}
        for name in ry.SOURCES if name in years
    ]
    seen = ry.describe(years)

    # ---- Album: MusicBrainz's original album, only when the current one looks wrong.
    if album_junk:
        if release:
            why = f"album looks wrong - MusicBrainz: original release is \"{release['album']}\" ({release['year']})"
            # An album from well after the song's likely year is probably a
            # later album the song was moved onto, not where it started.
            later = cons["year"] and release["year"] - cons["year"] >= 2
            if later:
                why += f" - but the song dates from about {cons['year']}, so that may be a later album: check"
            base = 0.7 if release["kind"] == "Album" else 0.5
            rows.append(_row(track, "albumTitle", track.get("albumTitle"), release["album"], 0.35 if later else base,
                             why, "musicbrainz", mb_url, sources))
        else:
            rows.append(_row(track, "albumTitle", track.get("albumTitle"), None, 0.0,
                             "album looks like a compilation/placeholder and MusicBrainz had no match - type the album",
                             "musicbrainz", None, sources))

    # ---- Year: judged by agreement between sources, never by one of them.
    if year_junk:
        if cons["year"] and not ry.is_version_title(title):
            conf = 0.85 if cons["strength"] == "strong" else 0.65
            rows.append(_row(track, "year", year, cons["year"], conf,
                             f"year looks wrong - {cons['strength']} agreement on {cons['year']} ({seen})", "years", None, sources))
        else:
            why = f"year looks wrong; the sources don't settle it ({seen or 'no source had this song'}) - type the year"
            rows.append(_row(track, "year", year, None, 0.0, why, "years", None, sources))
    elif len(values) >= 2 and not any(abs(v - int(year)) <= YEAR_TOLERANCE for v in values):
        # Two or more sources answered and not one is within a year of
        # the current value. (If even one supports it - B4-4 "Get Down":
        # MusicBrainz 2000, iTunes 2003, filed 2000 - the year stands:
        # the other source may just be missing the original.)
        version = ry.is_version_title(title)
        if cons["year"] and not version:
            conf = 0.85 if cons["strength"] == "strong" else 0.65
            why = f"no source supports {year}; {cons['strength']} agreement on {cons['year']} ({seen})"
            new = cons["year"]
        else:
            conf, new = 0.0, None
            why = f"no source supports {year} ({seen})"
            why += " - this is a remix/version, so they may be describing the original song" if version else " - they don't agree with each other, so no suggestion"
        rows.append(_row(track, "year", year, new, conf, why, "years", None, sources))
    return rows


def plan_track(track: dict, genre_stats: dict, lookup=ry.lookup) -> dict:
    """One track's rows, shaped like the other actions' plan_track() so
    review_ui's progress preview can treat them alike: auto/create are
    always empty (nothing here is ever pre-approved), review holds every
    row. `lookup_failed` is set when a year source couldn't be reached
    (the suggestions are then based on fewer sources than usual)."""
    rows: list[dict] = []
    info = lookup(track.get("artist") or "", track.get("title") or "")
    rows.extend(suggest_album_year(track, info))
    g = suggest_genre(track, genre_stats)
    if g:
        rows.append(g)
    return {"auto": [], "review": rows, "create": [], "no_match": None, "lookup_failed": bool(info.get("unavailable"))}


def generate_plan(
    limit: int | None = None,
    track_id: int | None = None,
    scan_mode: str = "incoming",
    out_path: str | Path | None = None,
    on_status=None,
    on_track_planned=None,
    should_stop=None,
) -> dict:
    """Same signature shape as charts_plan.generate_plan() so review_ui
    drives all four actions the same way. Never resumes from a saved
    position (no since_track_id): MusicBrainz lookups are cached on disk
    (fetch/musicbrainz_release.py), so re-scanning the same tracks is
    already cheap, and Incoming self-narrows as tracks are filed."""
    if scan_mode not in SCAN_MODES:
        raise ValueError(f"scan_mode must be one of {SCAN_MODES}, got {scan_mode!r}")

    def status(msg):
        if on_status:
            on_status(msg)

    status("reading library...")
    library = lexicon_client.fetch_library()
    status(f"  {len(library)} tracks")
    genre_stats = build_genre_stats(library)

    if scan_mode == "recent":
        tracks = lexicon_client.fetch_library(
            sort=[{"field": "dateAdded", "dir": "desc"}], limit=limit or DEFAULT_RECENT_COUNT,
        )
    elif scan_mode == "incoming":
        tracks = [t for t in library if t.get("incoming")]
        tracks.sort(key=lambda t: t["id"])
    else:
        tracks = library
    if track_id is not None:
        tracks = [t for t in tracks if t["id"] == track_id]
    if limit and scan_mode != "recent":
        tracks = tracks[:limit]
    status(f"planning {len(tracks)} track(s) - three year sources, rate-limited; uncached tracks take a few seconds each")

    review_all: list[dict] = []
    lookup_failed: list[dict] = []
    stopped = False
    for i, track in enumerate(tracks, 1):
        if should_stop and should_stop():
            stopped = True
            status(f"stopped after {i - 1}/{len(tracks)} track(s)")
            break
        result = plan_track(track, genre_stats)
        review_all.extend(result["review"])
        if result["lookup_failed"]:
            lookup_failed.append({"track_id": track["id"], "artist": track.get("artist"), "title": track.get("title")})
        if on_track_planned:
            on_track_planned(i, len(tracks), track, result)

    plan = {
        "version": 1,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "stopped_early": stopped,
        "auto": [],
        "review": review_all,
        "create": [],
        "no_match": [],
        # Tracks MusicBrainz couldn't be reached for (rate limit/outage
        # after retries) - their album/year weren't checked at all, as
        # opposed to checked-and-fine. Scanning again later fills them in.
        "lookup_failed": lookup_failed,
    }
    path = Path(out_path) if out_path else PLAN_FILE
    path.write_text(json.dumps(plan, indent=1))
    n_tracks = len({r["track_id"] for r in review_all})
    status(f"{len(review_all)} suggestion(s) across {n_tracks} track(s); {len(lookup_failed)} couldn't be looked up")
    status(f"plan -> {path}")
    return plan


def main():
    p = argparse.ArgumentParser(description="Plan Album/Year/Genre fixes (writes metadata_plan.json; changes nothing in Lexicon).")
    p.add_argument("--limit", type=int, default=None)
    p.add_argument("--track-id", type=int, default=None)
    p.add_argument("--mode", choices=SCAN_MODES, default="incoming")
    p.add_argument("--out", default=None)
    args = p.parse_args()

    def on_track_planned(i, total, track, result):
        print(f"[{i}/{total}] {track.get('artist')} - {track.get('title')}" + ("  (a year source was unavailable)" if result["lookup_failed"] else ""))
        for r in result["review"]:
            new = r["new"] if r["new"] is not None else "(type one)"
            print(f"    {FIELD_LABELS[r['field']]:5} {r['old']!s:32.32} -> {new}")

    generate_plan(limit=args.limit, track_id=args.track_id, scan_mode=args.mode, out_path=args.out,
                  on_status=print, on_track_planned=on_track_planned)


if __name__ == "__main__":
    main()
