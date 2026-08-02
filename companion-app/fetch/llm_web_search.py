"""LLM + web-search genre/subgenre classification source.

Given artist + title + the track's current genre, prompts an LLM
(Claude, or Gemini as fallback) with web search enabled to return
*every* defensible subgenre tag, each with its own source URL and
the model's self-reported confidence for that specific tag - not a
single best-guess answer. This is a model source, so each result's
score is that reported confidence, not a fixed 1.0 (see ../scoring.py).
"""

from __future__ import annotations


def fetch_genres(artist: str, title: str, current_genre: str | None) -> list[dict]:
    """Return candidate subgenre tags with per-tag confidence and source URL.

    Each candidate: {"tag": str, "score": float, "source": "llm_web_search",
    "url": str | None, "note": str | None}
    """
    raise NotImplementedError
