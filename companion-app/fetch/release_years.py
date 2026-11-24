"""Where did a song's release year come from? Ask several sources, compare.

The Year field should hold the year the song *first came out*, and no
single database reliably knows it: each describes the releases it happens
to catalogue, and when the original is missing from one the "earliest"
it reports is a later reissue or album. (Measured 2026-11 on 112 tracks
against a hand-checked library: iTunes 73% exact, MusicBrainz's first-
release date 67%, Discogs masters 71%; the databases mostly err
*differently*, which is what makes comparing them useful.)

So this reports each source's answer separately and lets metadata_plan.py
judge by agreement instead of trusting any one:

- iTunes Search API       - no key; earliest release of the song by that
                            artist (compilations/karaoke/live skipped)
- MusicBrainz             - the recording's "first release date"
                            (fetch/musicbrainz_release.py, same request
                            that finds the original album)
- Discogs master releases - needs DISCOGS_TOKEN (companion-app/.env);
                            skipped quietly if there isn't one
- TheAudioDB              - free key "123" works out of the box (30
                            requests/min; set AUDIODB_API_KEY for a
                            premium key). Gives the *album's* year, so
                            like iTunes it can run a year late for a
                            single. Live albums are skipped.

Wikipedia was measured too and left out: its infobox dates describe
whichever release the article is about, and it added wrong "support" for
wrong years without catching extra errors.

Each source also reports the album names it lists the song on (kept in the
same cache; entries saved before that are refetched only when an album is
being proposed), so metadata_plan.py can have the sources vote on the
album the same way they vote on the year.

Results are cached on disk (year_cache.json): a year found is kept
forever, "found nothing" for 30 days. A source that couldn't be reached
after retries is listed in `unavailable` and NOT cached.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

from fetch import musicbrainz as mb
from fetch import musicbrainz_release as mbr

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

CACHE_FILE = Path(__file__).resolve().parent.parent / "year_cache.json"
NEGATIVE_TTL_DAYS = 30
SOURCES = ("itunes", "musicbrainz", "discogs", "audiodb")
SOURCE_LABELS = {"itunes": "iTunes", "musicbrainz": "MusicBrainz", "discogs": "Discogs", "audiodb": "TheAudioDB"}

USER_AGENT = "TrackRecord/0.1 +https://github.com/sstrubberg/track-record"
_ITUNES_GAP = 2.0   # seconds between iTunes requests (its limit is ~20/min)
_DISCOGS_GAP = 1.1  # Discogs allows 60/min with a token
_AUDIODB_GAP = 2.1  # free key: 30/min, and each track costs two requests
AUDIODB_FREE_KEY = "123"  # TheAudioDB's published free key ("2" no longer works)
_last_call: dict[str, float] = {}


class SourceUnavailable(Exception):
    """A source couldn't be reached (rate limit / outage) after retries."""


# DJ-pool tags in a title that say nothing about *which recording* it is.
# A title with any OTHER parenthetical that names a mix/version/remix is
# a different recording than the song the databases describe, so its
# year can't be judged from them.
_VERSION_MARKER = re.compile(r"\b(remix|mix|version|rework|dub|bootleg|flip|club|extended|acoustic|live|instrumental)\b", re.I)
_PAREN = re.compile(r"[\(\[]([^)\]]*)[)\]]")


def is_version_title(title: str) -> bool:
    """True if the title's brackets name a remix/mix/version (not just a
    DJ-pool tag like "(CLEAN)" or "(MM Edit)")."""
    return any(_VERSION_MARKER.search(p) for p in _PAREN.findall(title or ""))


def _norm(s: str | None) -> str:
    s = re.sub(r"[\(\[].*?[\)\]]", "", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def _throttle(name: str, gap: float) -> None:
    wait = _last_call.get(name, 0.0) + gap - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_call[name] = time.monotonic()


def _get(name: str, gap: float, url: str, **kwargs) -> requests.Response:
    last: Exception | None = None
    for delay in (0, 4, 10, 25):
        if delay:
            time.sleep(delay)
        _throttle(name, gap)
        try:
            r = requests.get(url, timeout=25, **kwargs)
            if r.status_code in (403, 429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return r
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
    raise SourceUnavailable(f"{name}: {last}")


def itunes_info(artist: str, title: str) -> dict:
    """{"year": earliest release year or None, "albums": [{"name","year"}...]
    oldest first} for the song as iTunes sells it."""
    a = mb._primary_artist(artist)
    t = mb._strip_edit_suffixes(title)
    r = _get("itunes", _ITUNES_GAP, "https://itunes.apple.com/search",
             params={"term": f"{a} {t}", "entity": "song", "limit": 50})
    years, albums = [], []
    for x in r.json().get("results", []):
        if _norm(x.get("trackName")) != _norm(t):
            continue
        if _norm(mb._primary_artist(x.get("artistName"))) != _norm(a):
            continue
        if mbr.JUNK_ALBUM_RE.search(x.get("collectionName") or ""):
            continue
        if re.search(r"\b(live|karaoke|remix|sped up|instrumental)\b", x.get("trackName") or "", re.I):
            continue
        d = x.get("releaseDate") or ""
        if d[:4].isdigit():
            years.append(int(d[:4]))
            if x.get("collectionName"):
                albums.append({"name": x["collectionName"], "year": int(d[:4])})
    return {"year": min(years) if years else None, "albums": _oldest_first(albums)}


def discogs_info(artist: str, title: str) -> dict:
    token = os.environ.get("DISCOGS_TOKEN")
    if not token:
        raise KeyError("no DISCOGS_TOKEN")
    a = mb._primary_artist(artist)
    t = mb._strip_edit_suffixes(title)
    r = _get("discogs", _DISCOGS_GAP, "https://api.discogs.com/database/search",
             headers={"User-Agent": USER_AGENT, "Authorization": f"Discogs token={token}"},
             params={"artist": a, "track": t, "type": "master", "per_page": 50})
    years, albums = [], []
    for x in r.json().get("results", []):
        ttl = x.get("title") or ""
        if _norm(mb._primary_artist(ttl.split(" - ")[0])) != _norm(a):
            continue
        if re.search(r"live|karaoke|remix|compilation|greatest|best of|hits|collection", ttl, re.I):
            continue
        try:
            y = int(x.get("year"))
        except (TypeError, ValueError):
            continue
        years.append(y)
        name = ttl.split(" - ", 1)[-1].strip()
        if name:
            albums.append({"name": name, "year": y})
    return {"year": min(years) if years else None, "albums": _oldest_first(albums)}


def audiodb_info(artist: str, title: str) -> dict:
    """TheAudioDB: find the track, then read its album's release year (the
    track record itself carries none). Looks at up to 3 matching tracks and
    returns the earliest studio-album year; live albums are skipped."""
    key = os.environ.get("AUDIODB_API_KEY") or AUDIODB_FREE_KEY
    base = f"https://www.theaudiodb.com/api/v1/json/{key}"
    a = mb._primary_artist(artist)
    t = mb._strip_edit_suffixes(title)
    try:
        r = _get("audiodb", _AUDIODB_GAP, f"{base}/searchtrack.php", params={"s": a, "t": t})
    except SourceUnavailable:
        raise
    except requests.HTTPError as e:
        # A key the service doesn't recognise answers 404 {"Message":"Not found"}.
        raise KeyError(f"TheAudioDB rejected the key: {e}")
    tracks = (r.json() or {}).get("track") or []
    years, albums = [], []
    seen_albums = set()
    for x in tracks:
        if _norm(x.get("strTrack")) != _norm(t):
            continue
        if _norm(mb._primary_artist(x.get("strArtist"))) != _norm(a):
            continue
        album_id = x.get("idAlbum")
        if not album_id or album_id in seen_albums or mbr.JUNK_ALBUM_RE.search(x.get("strAlbum") or ""):
            continue
        seen_albums.add(album_id)
        if len(seen_albums) > 3:
            break
        try:
            ar = _get("audiodb", _AUDIODB_GAP, f"{base}/album.php", params={"m": album_id})
        except requests.HTTPError:
            continue
        for alb in (ar.json() or {}).get("album") or []:
            if re.search(r"live|compilation|bootleg", alb.get("strReleaseFormat") or "", re.I):
                continue
            if re.search(r"\b(live|karaoke)\b|\d{4}-\d\d-\d\d", alb.get("strAlbum") or "", re.I):
                continue
            try:
                y = int(alb.get("intYearReleased"))
            except (TypeError, ValueError):
                continue
            years.append(y)
            albums.append({"name": alb.get("strAlbum") or x.get("strAlbum") or "", "year": y})
    return {"year": min(years) if years else None, "albums": _oldest_first([a_ for a_ in albums if a_["name"]])}


def _oldest_first(albums: list[dict]) -> list[dict]:
    seen, out = set(), []
    for a in sorted(albums, key=lambda x: x["year"]):
        k = (canon_album(a["name"]), a["year"])
        if k not in seen:
            seen.add(k)
            out.append(a)
    return out


def canon_album(name: str | None) -> str:
    """An album name boiled down for comparing across sources: no
    bracketed bits, no "- Single" / "- EP" / "(Deluxe Edition)" suffixes
    or punctuation. An album called just "Version" keeps its name."""
    s = (name or "").lower()
    plain = re.sub(r"[^a-z0-9]+", "", s)
    s = re.sub(r"[\(\[].*?[\)\]]", "", s)
    s = re.sub(r"\s+-\s+(single|ep|e\.p\.)\s*$", "", s)
    s = re.sub(r"\b(deluxe|edition|expanded|remastered|remaster|anniversary|explicit|bonus|special)\b", "", s)
    s = re.sub(r"\b(ep|single)\b\s*$", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s or plain


def same_album(a: str | None, b: str | None) -> bool:
    ca, cb = canon_album(a), canon_album(b)
    return bool(ca and cb and (ca == cb or (min(len(ca), len(cb)) >= 4 and (ca in cb or cb in ca))))


def album_support(albums: dict, name: str) -> list[str]:
    """Sources (in SOURCES order) with an album matching `name`."""
    return [n for n in SOURCES if any(same_album(name, c["name"]) for c in albums.get(n) or [])]


def describe_albums(albums: dict, name: str, title: str = "") -> str:
    """'MusicBrainz ✓ · iTunes ✓ · TheAudioDB none · Discogs "Collage"'."""
    parts = []
    for n in SOURCES:
        if n not in albums:
            continue
        cands = albums[n]
        if not cands:
            parts.append(f"{SOURCE_LABELS[n]} none")
        elif any(same_album(name, c["name"]) for c in cands):
            parts.append(f"{SOURCE_LABELS[n]} ✓")
        else:
            parts.append(f'{SOURCE_LABELS[n]} "{cands[0]["name"]}"')
    return " · ".join(parts)


def is_single_named(album: str | None, title: str | None) -> bool:
    """True if the 'album' is just the song's own title - a single, not
    the album the song came from."""
    return bool(canon_album(album)) and canon_album(album) == canon_album(mb._strip_edit_suffixes(title or ""))


def _first_picks(albums: dict, title: str) -> dict:
    """Each source's earliest album that isn't just the song's own title
    (a single named after the song says nothing about the album)."""
    picks = {}
    for n in SOURCES:
        for c in albums.get(n) or []:
            if canon_album(c["name"]) and canon_album(c["name"]) != canon_album(title):
                picks[n] = c["name"]
                break
    return picks


def _agreeing(picks: dict, skip: str | None = None) -> tuple[str, list[str]] | None:
    groups: dict[str, list] = {}
    for n, name in picks.items():
        if skip and same_album(skip, name):
            continue
        groups.setdefault(canon_album(name), [name, set()])[1].add(n)
    best = max(groups.values(), key=lambda g: len(g[1]), default=None)
    if best and len(best[1]) >= 2:
        return best[0], [n for n in SOURCES if n in best[1]]
    return None


def rival_album(albums: dict, name: str, title: str) -> tuple[str, list[str]] | None:
    """An album two or more sources each list FIRST that is not `name`:
    (that album, sources)."""
    return _agreeing(_first_picks(albums, title), skip=name)


def agreed_album(albums: dict, title: str) -> tuple[str, list[str]] | None:
    """When MusicBrainz has no album: one that two or more other sources
    list first: (album, sources)."""
    return _agreeing(_first_picks(albums, title))


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(cache, indent=1))
    except OSError:
        pass


def _cached(cache: dict, name: str, artist: str, title: str, fetch, need_albums: bool = False) -> dict:
    """A source's {"year", "albums"} from the cache when fresh ("albums" is
    None for an entry saved before album names were kept; it is refetched
    only when `need_albums`). Raises SourceUnavailable without caching."""
    key = f"{name}|{_norm(mb._primary_artist(artist))}|{_norm(mb._strip_edit_suffixes(title))}"
    hit = cache.get(key)
    if hit is not None:
        fresh = hit.get("year") is not None or (
            datetime.now(timezone.utc) - datetime.fromisoformat(hit["when"])
        ).days < NEGATIVE_TTL_DAYS
        if fresh and ("albums" in hit or not need_albums):
            return {"year": hit.get("year"), "albums": hit.get("albums")}
    info = fetch(artist, title)
    cache[key] = {**info, "when": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return info


def lookup(artist: str, title: str, need_albums: bool = False) -> dict:
    """Ask every source.

    {"years": {"itunes": int|None, "musicbrainz": int|None, "discogs": int|None, "audiodb": int|None},
     "release": MusicBrainz's original-album answer or None (for the Album field),
     "albums": {source: [{"name","year"}, ...]} - every source's album names, only
               when `need_albums` (older cache entries lack them and cost a refetch),
     "recording_id": MusicBrainz recording id or None,
     "unavailable": [source names that couldn't be reached]}

    A source skipped because it isn't configured (no Discogs token) is
    simply absent from "years"."""
    years: dict[str, int | None] = {}
    albums: dict[str, list] = {}
    unavailable: list[str] = []
    release = None

    try:
        info = mbr.find_release_info(artist, title)
        release = info["release"]
        years["musicbrainz"] = info["first_year"]
        albums["musicbrainz"] = [{"name": release["album"], "year": release["year"]}] if release else []
    except mbr.LookupUnavailable:
        unavailable.append("musicbrainz")

    cache = _load_cache()
    before = json.dumps(cache, sort_keys=True)
    for name, fetch in (("itunes", itunes_info), ("discogs", discogs_info), ("audiodb", audiodb_info)):
        try:
            got = _cached(cache, name, artist, title, fetch, need_albums)
            years[name] = got["year"]
            if got.get("albums") is not None:
                albums[name] = got["albums"]
        except SourceUnavailable:
            unavailable.append(name)
        except KeyError:
            pass  # not configured
    if json.dumps(cache, sort_keys=True) != before:
        _save_cache(cache)

    return {
        "years": years,
        "release": release,
        "albums": albums,
        "recording_id": (release or {}).get("recording_id"),
        "unavailable": unavailable,
    }


def consensus(years: dict) -> dict:
    """Judge by agreement.

    {"year": the agreed year or None, "strength": "strong" (3+ agree) /
     "medium" (exactly 2 agree, no rival pair) / None, "values": [all
     answers]}. One source alone, or all disagreeing, or a tie between
    two years, gives no year: nobody should be trusted on their own."""
    values = [y for y in years.values() if isinstance(y, int)]
    if not values:
        return {"year": None, "strength": None, "values": []}
    ranked = Counter(values).most_common()
    top_year, top_count = ranked[0]
    tied = len(ranked) > 1 and ranked[1][1] == top_count
    if top_count >= 3 and not tied:
        return {"year": top_year, "strength": "strong", "values": values}
    if top_count == 2 and not tied:
        return {"year": top_year, "strength": "medium", "values": values}
    return {"year": None, "strength": None, "values": values}


def describe(years: dict) -> str:
    """'iTunes 2003 · MusicBrainz 2000 · Discogs none · TheAudioDB 2001'."""
    return " · ".join(
        f"{SOURCE_LABELS[n]} {years[n] if years[n] is not None else 'none'}" for n in SOURCES if n in years
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        sys.exit('usage (from companion-app/): python -m fetch.release_years "Artist" "Title"')
    res = lookup(sys.argv[1], sys.argv[2])
    print(describe(res["years"]), "| unavailable:", res["unavailable"] or "none")
    print("consensus:", consensus(res["years"]))
