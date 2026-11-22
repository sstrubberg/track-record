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

Wikipedia was measured too and left out: its infobox dates describe
whichever release the article is about, and it added wrong "support" for
wrong years without catching extra errors.

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
SOURCES = ("itunes", "musicbrainz", "discogs")
SOURCE_LABELS = {"itunes": "iTunes", "musicbrainz": "MusicBrainz", "discogs": "Discogs"}

USER_AGENT = "TrackRecord/0.1 +https://github.com/sstrubberg/track-record"
_ITUNES_GAP = 2.0   # seconds between iTunes requests (its limit is ~20/min)
_DISCOGS_GAP = 1.1  # Discogs allows 60/min with a token
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


def itunes_year(artist: str, title: str) -> int | None:
    a = mb._primary_artist(artist)
    t = mb._strip_edit_suffixes(title)
    r = _get("itunes", _ITUNES_GAP, "https://itunes.apple.com/search",
             params={"term": f"{a} {t}", "entity": "song", "limit": 50})
    years = []
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
    return min(years) if years else None


def discogs_year(artist: str, title: str) -> int | None:
    token = os.environ.get("DISCOGS_TOKEN")
    if not token:
        raise KeyError("no DISCOGS_TOKEN")
    a = mb._primary_artist(artist)
    t = mb._strip_edit_suffixes(title)
    r = _get("discogs", _DISCOGS_GAP, "https://api.discogs.com/database/search",
             headers={"User-Agent": USER_AGENT, "Authorization": f"Discogs token={token}"},
             params={"artist": a, "track": t, "type": "master", "per_page": 50})
    years = []
    for x in r.json().get("results", []):
        ttl = x.get("title") or ""
        if _norm(mb._primary_artist(ttl.split(" - ")[0])) != _norm(a):
            continue
        if re.search(r"live|karaoke|remix|compilation|greatest|best of|hits|collection", ttl, re.I):
            continue
        try:
            years.append(int(x.get("year")))
        except (TypeError, ValueError):
            pass
    return min(years) if years else None


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


def _cached(cache: dict, name: str, artist: str, title: str, fetch):
    """A source's year (or None), from the cache when fresh. Raises
    SourceUnavailable without caching anything."""
    key = f"{name}|{_norm(mb._primary_artist(artist))}|{_norm(mb._strip_edit_suffixes(title))}"
    hit = cache.get(key)
    if hit is not None:
        if hit.get("year") is not None:
            return hit["year"]
        if (datetime.now(timezone.utc) - datetime.fromisoformat(hit["when"])).days < NEGATIVE_TTL_DAYS:
            return None
    year = fetch(artist, title)
    cache[key] = {"year": year, "when": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return year


def lookup(artist: str, title: str) -> dict:
    """Ask every source.

    {"years": {"itunes": int|None, "musicbrainz": int|None, "discogs": int|None},
     "release": MusicBrainz's original-album answer or None (for the Album field),
     "recording_id": MusicBrainz recording id or None,
     "unavailable": [source names that couldn't be reached]}

    A source skipped because it isn't configured (no Discogs token) is
    simply absent from "years"."""
    years: dict[str, int | None] = {}
    unavailable: list[str] = []
    release = None

    try:
        info = mbr.find_release_info(artist, title)
        release = info["release"]
        years["musicbrainz"] = info["first_year"]
    except mbr.LookupUnavailable:
        unavailable.append("musicbrainz")

    cache = _load_cache()
    before = json.dumps(cache, sort_keys=True)
    for name, fetch in (("itunes", itunes_year), ("discogs", discogs_year)):
        try:
            years[name] = _cached(cache, name, artist, title, fetch)
        except SourceUnavailable:
            unavailable.append(name)
        except KeyError:
            pass  # not configured
    if json.dumps(cache, sort_keys=True) != before:
        _save_cache(cache)

    return {
        "years": years,
        "release": release,
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
    """'iTunes 2003 · MusicBrainz 2000 · Discogs none'."""
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
