"""Discogs genre/style fetch source.

Searches the public Discogs API by artist + track title and returns
style/genre candidates from the best-matching release. Metadata
source: found-or-not, so each result carries score = 1 (see
../scoring.py for how source weight and score combine).

Requires a personal access token in the DISCOGS_TOKEN environment
variable. Copy ../.env.example to ../.env and fill in your own token
from https://www.discogs.com/settings/developers - .env is gitignored,
so it's never committed and never needs to be pasted anywhere.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

BASE = "https://api.discogs.com"
USER_AGENT = "TrackRecord/0.1 +https://github.com/sstrubberg/track-record"
REQUEST_DELAY = 1.0  # stays well under Discogs' authenticated 60 req/min limit

_session = requests.Session()
_session.headers.update({"User-Agent": USER_AGENT})

_last_request = 0.0


def _throttle() -> None:
    global _last_request
    wait = REQUEST_DELAY - (time.monotonic() - _last_request)
    if wait > 0:
        time.sleep(wait)
    _last_request = time.monotonic()


def _token() -> str:
    token = os.environ.get("DISCOGS_TOKEN")
    if not token:
        raise RuntimeError(
            "DISCOGS_TOKEN is not set. Copy companion-app/.env.example to "
            "companion-app/.env and fill in a token from "
            "https://www.discogs.com/settings/developers."
        )
    return token


def fetch_genres(artist: str, title: str) -> list[dict]:
    """Return candidate genre/style tags for a track from Discogs.

    Each candidate: {"tag": str, "score": 1.0, "source": "discogs",
    "url": str, "note": str}
    """
    _throttle()
    r = _session.get(
        f"{BASE}/database/search",
        params={
            "artist": artist,
            "track": title,
            "type": "release",
            "token": _token(),
        },
        timeout=15,
    )
    r.raise_for_status()
    results = r.json().get("results", [])
    if not results:
        return []

    best = results[0]  # Discogs returns results pre-ranked by relevance
    url = f"https://www.discogs.com{best['uri']}" if best.get("uri") else None
    release_title = best.get("title", "")

    candidates = []
    for field, kind in (("genre", "genre"), ("style", "style")):
        for value in best.get(field) or []:
            candidates.append({
                "tag": value,
                "score": 1.0,
                "source": "discogs",
                "url": url,
                "note": f'Discogs {kind} on "{release_title}"',
            })
    return candidates


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        sys.exit("usage: python discogs.py <artist> <title>")
    for candidate in fetch_genres(sys.argv[1], sys.argv[2]):
        print(candidate)
