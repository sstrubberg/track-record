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

# A release's `format` array can include these. Both mean its genre/style
# describes a whole various-songs release, not the one track we asked
# about - a bootleg comp of 20 unrelated hits tagged "Pop Rap" doesn't
# make the one Gwen Stefani song on it Pop Rap. "unofficial release" is
# Discogs' own label for pirate/bootleg pressings; confirmed against a
# real bad match (a Russian bootleg comp crediting "Hollaback Girl"'s
# genre/style to the whole disc's mixed Beyonce/Gwen Stefani/Pink tracklist).
UNRELIABLE_FORMATS = {"compilation", "unofficial release"}

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


def _is_reliable_release(result: dict) -> bool:
    formats = {f.lower() for f in (result.get("format") or [])}
    return formats.isdisjoint(UNRELIABLE_FORMATS)


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

    # Discogs pre-ranks by relevance, but "most relevant" still includes
    # bootleg/various-artist compilations that happen to contain the
    # track - skip those rather than trust a match whose genre/style
    # describes dozens of other songs too. No reliable release found ->
    # no candidates, not a guess.
    best = next((r for r in results if _is_reliable_release(r)), None)
    if best is None:
        return []

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
