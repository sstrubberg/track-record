"""Weighted noisy-OR scoring for candidate tags.

confidence = 1 - Pi(1 - weight_i * score_i)   for each source i that found the tag

- Metadata sources (MusicBrainz, Discogs): score = 1 (found it or didn't).
- Model sources (discogs-maest, LLM web-search): score = that model's own
  reported probability for the specific tag.
- weight_i comes from config/source_weights.yaml, editable per-DJ without
  touching code.
"""

from __future__ import annotations

from functools import reduce
from pathlib import Path
from typing import Iterable

import yaml

DEFAULT_WEIGHTS_PATH = Path(__file__).parent / "config" / "source_weights.yaml"


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
    grouped: dict[str, list[dict]] = {}
    for c in candidates:
        grouped.setdefault(c["tag"], []).append(c)
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
