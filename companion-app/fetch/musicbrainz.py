"""MusicBrainz genre fetch source.

Looks up a track by artist + title and returns its genre tag(s).
Metadata source: found-or-not, so each result carries score = 1
(see ../scoring.py for how source weight and score combine).

MusicBrainz genre data is inconsistently populated - a recording itself
often has none, even when its release or artist do. So this falls back
recording -> release-group -> artist, and stops at the first level that
has anything, noting which level it came from for the review UI.

No API key exists for reads; MusicBrainz instead requires an
identifying User-Agent on every request and asks unauthenticated
clients to stay near 1 request/second.
"""

from __future__ import annotations

import re
import time

import requests

BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = "TrackRecord/0.1 ( https://github.com/sstrubberg/track-record )"
REQUEST_DELAY = 1.05
MATCH_THRESHOLD = 90  # MusicBrainz's own search relevance score (0-100)

# DJ libraries routinely suffix titles with edit/version notes that
# aren't part of the actual release - "(Intro Clean)", "(CLEAN) (MM
# Edit)", "(Dirty)". MusicBrainz's search won't match through those.
# Same problem billboard_tag.py's norm_title already solves for chart
# matching; this is the live-search-API equivalent.
_PAREN_SUFFIX = re.compile(r"\s*[\(\[][^)\]]*[)\]]")


def _strip_edit_suffixes(title: str) -> str:
    stripped = _PAREN_SUFFIX.sub("", title).strip()
    return stripped or title


# DJ libraries also often merge a featured-artist credit into the same
# Artist field the primary artist is stored in - "Nelly Furtado ft
# Timbaland" - but MusicBrainz's own search doesn't match through that
# either. Same ARTIST_SPLIT pattern billboard_tag.py uses for
# chart-artist matching, minus its lowercase/punctuation normalization -
# this only needs to isolate the primary credit, not build a fuzzy key.
_ARTIST_SPLIT = re.compile(
    r"\s+(?:&|x|and|with|feat\.?|ft\.?|featuring|vs\.?|f/)\s+"
    r"|,(?!\s*(?:inc|ltd|llc|co|jr|sr)\b)\s*"
    r"|\s+/\s+", re.I,
)


def _primary_artist(artist: str) -> str:
    primary = _ARTIST_SPLIT.split(artist)[0].strip()
    return primary or artist

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})

_last_request = 0.0


def _throttle() -> None:
    global _last_request
    wait = REQUEST_DELAY - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _escape(term: str) -> str:
    return term.replace("\\", "\\\\").replace('"', '\\"')


def _search_recording(artist: str, title: str) -> tuple[str | None, int | None]:
    query = f'artist:"{_escape(artist)}" AND recording:"{_escape(title)}"'
    _throttle()
    r = _session.get(
        f"{BASE}/recording",
        params={"query": query, "fmt": "json", "limit": 5},
        timeout=15,
    )
    r.raise_for_status()
    recordings = r.json().get("recordings", [])
    if not recordings:
        return None, None
    best = max(recordings, key=lambda rec: rec.get("score", 0))
    score = best.get("score", 0)
    if score < MATCH_THRESHOLD:
        return None, None
    return best["id"], score


def _genres_or_tags(entity: dict) -> list[dict]:
    """Prefer curated genres; fall back to folksonomy tags if that's all
    an entity has - both come back in the same {"name": ...} shape."""
    return entity.get("genres") or entity.get("tags") or []


def _artist_id(recording: dict) -> str | None:
    credits = recording.get("artist-credit") or []
    if credits and credits[0].get("artist"):
        return credits[0]["artist"]["id"]
    return None


def fetch_genres(artist: str, title: str) -> list[dict]:
    """Return candidate genre tags for a track from MusicBrainz.

    Each candidate: {"tag": str, "score": 1.0, "source": "musicbrainz",
    "url": str, "note": str}
    """
    primary_artist = _primary_artist(artist)
    stripped_title = _strip_edit_suffixes(title)
    candidates = _fetch_genres_for_title(primary_artist, stripped_title)
    if not candidates and (primary_artist != artist or stripped_title != title):
        candidates = _fetch_genres_for_title(artist, title)
    return candidates


def _fetch_genres_for_title(artist: str, title: str) -> list[dict]:
    mbid, match_score = _search_recording(artist, title)
    if mbid is None:
        return []

    _throttle()
    r = _session.get(
        f"{BASE}/recording/{mbid}",
        params={"inc": "genres+tags+releases+release-groups", "fmt": "json"},
        timeout=15,
    )
    r.raise_for_status()
    recording = r.json()

    genres = _genres_or_tags(recording)
    level = "recording"
    url = f"https://musicbrainz.org/recording/{mbid}"

    if not genres:
        for release in recording.get("releases") or []:
            rg = release.get("release-group") or {}
            rg_genres = _genres_or_tags(rg)
            if rg_genres:
                genres = rg_genres
                level = "release-group"
                url = f"https://musicbrainz.org/release-group/{rg['id']}"
                break

    if not genres:
        artist_id = _artist_id(recording)
        if artist_id:
            _throttle()
            r = _session.get(
                f"{BASE}/artist/{artist_id}",
                params={"inc": "genres+tags", "fmt": "json"},
                timeout=15,
            )
            r.raise_for_status()
            genres = _genres_or_tags(r.json())
            level = "artist"
            url = f"https://musicbrainz.org/artist/{artist_id}"

    note = (
        f"MB recording match {match_score}%"
        if level == "recording"
        else f"MB {level} genre (recording itself had none)"
    )
    return [
        {"tag": g["name"], "score": 1.0, "source": "musicbrainz", "url": url, "note": note}
        for g in genres
        if g.get("name")
    ]


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        sys.exit("usage: python musicbrainz.py <artist> <title>")
    for candidate in fetch_genres(sys.argv[1], sys.argv[2]):
        print(candidate)
