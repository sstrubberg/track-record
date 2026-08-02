"""Shared Lexicon Local API client for the companion app's actions.

Talks to the same Local API billboard_tag.py already uses
(localhost:48624), factored out here since more than one action needs
it - Genre/Subgenre now, Mood/Theme in v2. charts/billboard_tag.py
keeps its own copy of this logic inline; it's a verbatim port and
stays untouched rather than being refactored to import this.
"""

from __future__ import annotations

import os
import re

import requests

LEXICON = os.environ.get("LEXICON_URL", "http://localhost:48624/v1")


def lexicon_get(path: str, **params):
    r = requests.get(f"{LEXICON}{path}", params=params, timeout=60)
    r.raise_for_status()
    body = r.json()
    return body.get("data", body)


def fetch_tag_index() -> tuple[dict, dict]:
    """Return ({id: label}, {label_lower: id})."""
    payload = lexicon_get("/tags")
    by_id, by_label = {}, {}
    for t in payload.get("tags", []):
        label = t.get("label") or t.get("name") or ""
        by_id[t["id"]] = label
        by_label[label.lower()] = t["id"]
    return by_id, by_label


def _normalize_label(s: str) -> str:
    """Collapse hyphens/whitespace so 'Nu-Disco' and 'Nu Disco' compare
    equal - fetch sources and a DJ's own tag list punctuate the same
    subgenre name differently often enough that an exact match alone
    produces false 'this tag doesn't exist' negatives."""
    return re.sub(r"[\s\-]+", " ", s.strip().lower())


def resolve_tag_id(tag: str, by_label: dict) -> int | None:
    """Case-insensitive exact match first (cheap, the common case),
    falling back to a punctuation-insensitive match against every
    label. Returns None if the tag genuinely isn't in this library."""
    key = tag.lower()
    if key in by_label:
        return by_label[key]
    normalized = _normalize_label(tag)
    for label_lower, tag_id in by_label.items():
        if _normalize_label(label_lower) == normalized:
            return tag_id
    return None


def fetch_library() -> list[dict]:
    out, offset = [], 0
    while True:
        page = lexicon_get("/tracks", limit=1000, offset=offset)
        rows = page.get("tracks", []) if isinstance(page, dict) else page
        if not rows:
            break
        out.extend(rows)
        offset += len(rows)
        if len(rows) < 1000:
            break
    return out


def _patch_shapes(track_id, tags):
    """Candidate PATCH bodies - the API requires an 'edits' wrapper but
    the exact nesting isn't documented; billboard_tag.py discovered
    this by trying each once. Same approach here."""
    return [
        ("id+edits", {"id": track_id, "edits": {"tags": tags}}),
        ("edits list", {"edits": [{"id": track_id, "tags": tags}]}),
        ("edits object", {"edits": {"id": track_id, "tags": tags}}),
        ("ids+edits", {"ids": [track_id], "edits": {"tags": tags}}),
    ]


def write_track_tags(track_id, tags: list, shape: str | None = None):
    """Returns (ok, shape_name, detail). Negotiates the body shape once
    per process - pass the returned shape back in on subsequent calls
    to skip re-negotiating."""
    candidates = _patch_shapes(track_id, tags)
    if shape:
        candidates = [c for c in candidates if c[0] == shape]
    errors = []
    for name, body in candidates:
        r = requests.patch(f"{LEXICON}/track", json=body, timeout=30)
        if r.ok:
            return True, name, None
        errors.append(f"{name}: HTTP {r.status_code} {r.text[:160]}")
    return False, None, " | ".join(errors)
