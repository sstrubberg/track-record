"""MusicBrainz genre fetch source.

Looks up a track's MBID and returns its associated genre tag(s).
Metadata source: found-or-not, so each result carries score = 1
(see ../scoring.py for how source weight and score combine).
"""

from __future__ import annotations


def fetch_genres(artist: str, title: str) -> list[dict]:
    """Return candidate genre tags for a track from MusicBrainz.

    Each candidate: {"tag": str, "score": 1.0, "source": "musicbrainz",
    "url": str | None, "note": str | None}
    """
    raise NotImplementedError
