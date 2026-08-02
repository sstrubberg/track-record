"""Discogs genre/style fetch source.

Searches the public Discogs API by artist + title and returns
style/genre candidates. Metadata source: found-or-not, so each
result carries score = 1 (see ../scoring.py).
"""

from __future__ import annotations


def fetch_genres(artist: str, title: str) -> list[dict]:
    """Return candidate genre/style tags for a track from Discogs.

    Each candidate: {"tag": str, "score": 1.0, "source": "discogs",
    "url": str | None, "note": str | None}
    """
    raise NotImplementedError
