"""Weighted noisy-OR scoring for candidate tags.

confidence = 1 - Pi(1 - weight_i * score_i)   for each source i that found the tag

- Metadata sources (MusicBrainz, Discogs): score = 1 (found it or didn't).
- Model sources (discogs-maest, LLM web-search): score = that model's own
  reported probability for the specific tag.
- weight_i comes from config/source_weights.yaml, editable per-DJ without
  touching code.
"""

from __future__ import annotations

import re
from functools import reduce
from pathlib import Path
from typing import Iterable

import yaml

DEFAULT_WEIGHTS_PATH = Path(__file__).parent / "config" / "source_weights.yaml"

# Sources that use the actual Discogs-400 taxonomy (discogs.py's own
# API results, and both audio_model.py's discogs-maest classes and
# audio_model_genre_effnet.py's genre_discogs400 classes, which are
# both trained on that same taxonomy) - preferred as the displayed
# spelling when a tag also came from a source that doesn't share it.
# Concretely, back when MusicBrainz was still a source: its genre/tag
# data was lowercase folksonomy text ("hip hop", "dance-pop") where
# Discogs' is Title Case ("Hip Hop", "Dance-pop") - without
# normalizing, group_by_tag() below would treat those as two unrelated
# tags, both losing the noisy-OR confidence boost the other should have
# given it, not just showing as an odd-looking duplicate. Kept even
# with MusicBrainz gone - llm_web_search, if it's ever wired in, has no
# guarantee of matching this casing either.
_PREFERRED_TAG_SOURCES = ("discogs", "audio_model", "audio_model_genre_effnet")

# Same case/hyphen/whitespace-insensitive comparison lexicon_client.py's
# _normalize_label() already applies when matching a fetched tag
# against the Lexicon library's own tags - duplicated here rather than
# imported, since it's two lines and each module's own copy stays
# simple to read without a cross-module coupling for something this
# small.
_NORMALIZE_RE = re.compile(r"[\s\-]+")


def _normalize_tag(s: str) -> str:
    return _NORMALIZE_RE.sub(" ", s.strip().lower())


def load_weights(path: Path = DEFAULT_WEIGHTS_PATH) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def score_tag(candidates: Iterable[dict], weights: dict) -> float:
    """Combine same-tag candidates from multiple sources into one confidence.

    `candidates` are the subset of fetch results for a single tag on a
    single track, each shaped {"source": str, "score": float, ...}.
    """
    source_weights = weights.get("sources", {})
    complement = reduce(
        lambda acc, c: acc * (1 - source_weights.get(c["source"], 0) * c["score"]),
        candidates,
        1.0,
    )
    return 1 - complement


def group_by_tag(candidates: Iterable[dict]) -> dict[str, list[dict]]:
    """Groups candidates whose tags are the same aside from case/hyphen/
    whitespace differences into one entry - a Discogs "Hip Hop" and a
    MusicBrainz "hip hop" for the same track should combine into one
    higher-confidence result via noisy-OR, not sit side by side as two
    separate, weaker ones that both undercount how many sources
    actually agree. The merged group's displayed tag string prefers
    whichever candidate came from a _PREFERRED_TAG_SOURCES source (the
    actual Discogs-400 taxonomy this project is otherwise built
    around) over other sources' own casing, falling back to whichever
    candidate was seen first if none of them did.
    """
    by_key: dict[str, list[dict]] = {}
    for c in candidates:
        by_key.setdefault(_normalize_tag(c["tag"]), []).append(c)

    grouped: dict[str, list[dict]] = {}
    for members in by_key.values():
        preferred = next((m for m in members if m["source"] in _PREFERRED_TAG_SOURCES), None)
        display_tag = (preferred or members[0])["tag"]
        grouped[display_tag] = members
    return grouped


def score_track(candidates: Iterable[dict], weights: dict | None = None) -> list[dict]:
    """Score every candidate tag for a track.

    Returns a list of {"tag": str, "confidence": float, "sources": [...]}
    sorted by descending confidence.
    """
    weights = weights or load_weights()
    grouped = group_by_tag(candidates)
    scored = [
        {"tag": tag, "confidence": score_tag(group, weights), "sources": group}
        for tag, group in grouped.items()
    ]
    return sorted(scored, key=lambda s: s["confidence"], reverse=True)
