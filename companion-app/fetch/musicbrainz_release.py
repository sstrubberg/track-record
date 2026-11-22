"""MusicBrainz "original release" lookup, for the Metadata action.

Given a DJ-library artist + title ("Hey Ya (CLEAN) (MM Edit)"), finds the
song's original studio release: the album (or, if it never had one, the
single/EP) it first came out on, and the year that was. That is what
the Album Title and Year fields are meant to hold - not whichever
compilation, DJ-pool disc, karaoke album or reissue the file happened to
be ripped from.

Unlike fetch/musicbrainz.py (genre tags, found-or-not), this is judged
on *which* release, so it works by exclusion:

- only Album / Single / EP release groups, and never one carrying a
  Compilation / Live / Remix / DJ-mix / Mixtape / Demo / Spokenword
  secondary type (Soundtrack is kept - a soundtrack is often the
  song's real first home)
- release titles that read like compilation/karaoke/reissue junk are
  skipped even when MusicBrainz mislabels them as plain albums
- Official releases with a full date only
- the earliest date wins (that is what makes reissues harmless)

Measured against 68 tracks with hand-verified answers (2026-11): about
half came back fully right, a quarter wrong or only defensible (a
single's own title instead of its parent album, a year off by one), a
quarter found nothing. So callers must treat this as a *suggestion*
for a person to review, never something to apply unattended.

MusicBrainz rate-limits and 503s routinely, so requests retry with
backoff, and results (including "found nothing") are cached on disk so
re-scanning the same tracks costs nothing. A request that still fails
after the retries raises LookupUnavailable and is NOT cached - it's
"couldn't ask", which is different from "asked, no answer".
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from fetch import musicbrainz as mb

CACHE_FILE = Path(__file__).resolve().parent.parent / "metadata_cache.json"

# "Found nothing" can become "found something" as MusicBrainz grows, so
# a cached miss expires; a cached hit doesn't (a song's first release
# doesn't change).
NEGATIVE_TTL_DAYS = 30

RETRY_DELAYS = (2, 5, 12, 25)  # seconds between attempts after a 5xx/429/timeout

EXCLUDED_SECONDARY = {
    "Compilation", "Live", "Remix", "DJ-mix", "Mixtape/Street", "Demo",
    "Spokenword", "Interview", "Audiobook",
}

# Recording titles that are a different version of the song, not the
# song. Checked against the recording's own title (parens included) -
# unless the DJ's own title also says it's that version.
_VERSION_WORDS = re.compile(r"\b(live|acoustic|instrumental|karaoke|remix|mix|edit|sped up|demo)\b", re.I)

# Album titles that are really a compilation / pool disc / karaoke /
# repackage, whatever MusicBrainz or the file's own tags call them.
# Shared with metadata_plan.py, which uses it on the track's *current*
# album to decide whether it looks wrong, so the two stay in step.
JUNK_ALBUM_RE = re.compile(
    r"now that'?s what i call|now dance|\bnow \d+\b|\bnu dance|traxx|mighty moves|patreon|"
    r"\bdj\b(?! (khaled|snake|jazzy|shadow|premier))|promo|karaoke|mastermix|funkymix|"
    r"wicked mix|chart video|mp3 collection|compilation|top 40|radio:|hit ?list|just hits|"
    r"smash! hits|\bhits? (of|\d)|best of|house jam|"
    r"sped up|\(remixes\)|\bremixed?\b|^untitled$|^unknown|\(canada version|\(international version|"
    r"\(explicit version\)|\(complete set\)|\(deluxe\)|expanded edition|anniversary|"
    r"super hits|^compilation$|\bmix \d+\b|\bsessions? \d+\b",
    re.I,
)


class LookupUnavailable(Exception):
    """MusicBrainz couldn't be reached (rate limit / outage) after retries."""


def _norm(s: str | None) -> str:
    s = re.sub(r"[\(\[].*?[\)\]]", "", (s or "").lower())
    return re.sub(r"[^a-z0-9]+", "", s)


def _cache_key(artist: str, title: str) -> str:
    return f"{_norm(mb._primary_artist(artist))}|{_norm(mb._strip_edit_suffixes(title))}"


def _load_cache() -> dict:
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, ValueError):
        return {}


def _save_cache(cache: dict) -> None:
    try:
        CACHE_FILE.write_text(json.dumps(cache, indent=1))
    except OSError:
        pass  # a cache that can't be written just means a re-lookup next time


def _get_with_retry(params: dict) -> dict:
    last: Exception | None = None
    for delay in (0, *RETRY_DELAYS):
        if delay:
            time.sleep(delay)
        mb._throttle()
        try:
            r = mb._session.get(f"{mb.BASE}/recording", params=params, timeout=20)
            if r.status_code in (429, 500, 502, 503, 504):
                last = RuntimeError(f"HTTP {r.status_code}")
                continue
            r.raise_for_status()
            return r.json()
        except (requests.ConnectionError, requests.Timeout) as e:
            last = e
    raise LookupUnavailable(str(last))


def _pick(recordings: list[dict], title: str) -> dict | None:
    """Reduce MusicBrainz's search hits to one {album, year, ...} answer,
    or None if no qualifying release exists."""
    wanted = _norm(mb._strip_edit_suffixes(title))
    wants_version = bool(_VERSION_WORDS.search(title))
    candidates: list[tuple[int, str, str, str]] = []  # (year, type, rg title, recording id)
    for rec in recordings:
        if rec.get("score", 0) < mb.MATCH_THRESHOLD or _norm(rec.get("title")) != wanted:
            continue
        rec_title = rec.get("title") or ""
        if _VERSION_WORDS.search(rec_title) and not wants_version:
            continue
        for rel in rec.get("releases") or []:
            rg = rel.get("release-group") or {}
            if rg.get("primary-type") not in ("Album", "Single", "EP"):
                continue
            if set(rg.get("secondary-types") or []) & EXCLUDED_SECONDARY:
                continue
            if rel.get("status") not in (None, "Official"):
                continue
            date = rel.get("date") or ""
            if len(date) < 4 or not date[:4].isdigit():
                continue
            name = rg.get("title") or rel.get("title") or ""
            if not name or JUNK_ALBUM_RE.search(name):
                continue
            candidates.append((int(date[:4]), rg["primary-type"], name, rec["id"]))
    if not candidates:
        return None

    order = {"Album": 0, "EP": 1, "Single": 2}
    candidates.sort(key=lambda c: (c[0], order[c[1]]))
    year = candidates[0][0]
    albums = [c for c in candidates if c[1] == "Album"]
    # Prefer the earliest real album unless it came out long after the
    # first release at all (then the first release - a single - is the
    # more honest "original").
    chosen = albums[0] if albums and albums[0][0] <= year + 2 else candidates[0]
    return {
        "album": chosen[2],
        "year": year,
        "kind": chosen[1],
        "recording_id": chosen[3],
    }


# For "when did the song first come out" only genuinely different
# recordings are excluded - a radio edit or single mix carries the
# original release date, unlike a live take or a remix.
_DIFFERENT_RECORDING = re.compile(r"\b(live|acoustic|instrumental|karaoke|remix|sped up|demo)\b", re.I)


def _first_year(recordings: list[dict], title: str) -> int | None:
    """The earliest `first-release-date` MusicBrainz records for the song
    itself (any release type - this is "when did it first come out
    anywhere", the number the Year field wants). Same title/version
    filtering as _pick, none of its release-type filtering."""
    wanted = _norm(mb._strip_edit_suffixes(title))
    wants_version = bool(_DIFFERENT_RECORDING.search(title))
    years = []
    for rec in recordings:
        if rec.get("score", 0) < mb.MATCH_THRESHOLD or _norm(rec.get("title")) != wanted:
            continue
        if _DIFFERENT_RECORDING.search(rec.get("title") or "") and not wants_version:
            continue
        d = rec.get("first-release-date") or ""
        if len(d) >= 4 and d[:4].isdigit():
            years.append(int(d[:4]))
    return min(years) if years else None


def find_original_release(artist: str, title: str, use_cache: bool = True) -> dict | None:
    """{"album", "year", "kind", "recording_id"} for the song's original
    release, or None if MusicBrainz has no qualifying one. Raises
    LookupUnavailable if MusicBrainz couldn't be reached."""
    return find_release_info(artist, title, use_cache)["release"]


def find_release_info(artist: str, title: str, use_cache: bool = True) -> dict:
    """{"release": <find_original_release's answer or None>,
        "first_year": MusicBrainz's earliest recorded date for the song
                      or None}. One request gives both. Raises
    LookupUnavailable if MusicBrainz couldn't be reached."""
    key = _cache_key(artist, title)
    cache = _load_cache() if use_cache else {}
    hit = cache.get(key)
    # Entries written before first_year existed are refetched.
    if hit is not None and "first_year" in hit:
        if hit.get("result") is not None:
            return {"release": hit["result"], "first_year": hit["first_year"]}
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(hit["when"])).days
        if age < NEGATIVE_TTL_DAYS:
            return {"release": None, "first_year": hit["first_year"]}

    primary = mb._primary_artist(artist)
    stripped = mb._strip_edit_suffixes(title)
    data = _get_with_retry({
        "query": f'artist:"{mb._escape(primary)}" AND recording:"{mb._escape(stripped)}"',
        "fmt": "json",
        "limit": 100,  # popular songs have dozens of recordings; a small window can miss the original
    })
    recordings = data.get("recordings", [])
    result = _pick(recordings, title)
    first_year = _first_year(recordings, title)

    if use_cache:
        cache[key] = {"result": result, "first_year": first_year, "when": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        _save_cache(cache)
    return {"release": result, "first_year": first_year}
