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


def lexicon_get(path: str, json_body: dict | None = None, **params):
    """`json_body` is for parameters the query-string can't express
    cleanly (e.g. `sort`, an array of {field, dir} objects) - Lexicon's
    docs confirm query params also work as a JSON body on GET."""
    r = requests.get(f"{LEXICON}{path}", params=params, json=json_body, timeout=60)
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


def fetch_categories() -> list[dict]:
    """Return every Custom Tag category as [{"id": int, "label": str}, ...],
    original casing preserved (for display in a picker)."""
    payload = lexicon_get("/tags")
    return [
        {"id": c["id"], "label": c["label"]}
        for c in payload.get("categories", [])
        if c.get("label")
    ]


def fetch_tags_with_categories() -> tuple[list[dict], dict[int, str]]:
    """Return (tags, category_labels) - tags as every Custom Tag with its
    current categoryId ([{"id", "categoryId", "label"}, ...], original
    casing), category_labels as {id: label} for the categories those
    tags currently belong to. Lexicon's categories are flat (no
    nesting/parent field, confirmed directly against a live instance) -
    reorganize_genres.py is the one caller that needs a tag's *current*
    category alongside its label, to compute whether it needs to move;
    fetch_categories() alone (just the category list) and
    fetch_tag_index() alone (just id<->label) don't carry that link."""
    payload = lexicon_get("/tags")
    tags = [
        {"id": t["id"], "categoryId": t.get("categoryId"), "label": t.get("label") or t.get("name") or ""}
        for t in payload.get("tags", [])
    ]
    category_labels = {c["id"]: c["label"] for c in payload.get("categories", []) if c.get("label")}
    return tags, category_labels


def create_category(label: str) -> int:
    """POST /tag-category - creates a new, empty Custom Tag category and
    returns its id. Flat body ({"label": ...}), not wrapped in "edits" -
    confirmed directly against a live instance, same shape create_tag()
    already uses for /tag. Response is flat too: {"id", "label",
    "position", "tags": []} - not wrapped in a "data" key.

    Purely additive and reversible on its own: an empty category has no
    tags in it, so deleting one right back out (DELETE /tag-category,
    not implemented here - this project doesn't delete anything on a
    DJ's behalf) loses nothing. That stops being true once tags actually
    get moved into it - Lexicon's own delete warns "This will delete
    all Custom Tags in this category" - but that's a property of
    Lexicon's category deletion in general, not something this function
    introduces; the move step itself is a separate, already-confirmed
    action (see reorganize_genres.py's apply_moves()).

    Callers are responsible for not creating a category that already
    exists by this exact label - reorganize_genres.py's own
    create_categories() only ever calls this for families plan_moves()
    has already confirmed have no matching category yet.
    """
    r = requests.post(f"{LEXICON}/tag-category", json={"label": label}, timeout=30)
    r.raise_for_status()
    return r.json()["id"]


def create_tag(label: str, category_id: int) -> int:
    """POST /tag - creates a new Custom Tag in an existing category and
    returns its id. Never call this to create a category - that's
    create_category() above, /tag-category, a separate endpoint. The
    Genre/Subgenre and Mood/Theme review flow (apply.py/genre_apply,
    mood_apply) never creates a category as a side effect of approving
    a "propose a new tag" row - it only ever files into a category the
    DJ picked (or new_tag_category resolved) from what already exists.
    create_category() is reserved for reorganize_genres.py's own
    explicit, previewed, DJ-confirmed "Create Categories" action - a
    different, deliberate action, not something this function or the
    main review screen does on anyone's behalf.

    The response is a flat object - {"id": ..., "categoryId": ...,
    "label": ..., "position": ...} - not wrapped in a "data" key like
    /tracks and /tags are (confirmed directly against a live Lexicon
    instance). Unlike lexicon_get(), this doesn't unwrap a "data" key
    at all, since there isn't one to unwrap.
    """
    r = requests.post(
        f"{LEXICON}/tag",
        json={"categoryId": category_id, "label": label},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["id"]


def fetch_library(
    source: str | None = None,
    sort: list[dict] | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Fetch tracks. With no arguments, pages through the whole library
    at Lexicon's own default source ("non-archived"), database order -
    same as always.

    Pass `limit` for a single capped fetch instead of paginating - e.g.
    combined with `sort=[{"field": "dateAdded", "dir": "desc"}]` for
    "the N most recently added tracks". `source` selects which pool:
    "incoming" for just the Incoming bin, "all" for archived and
    non-archived together, "archived", or omit for the default.
    """
    query = {"source": source} if source is not None else {}
    body = {"sort": sort} if sort else None

    if limit is not None:
        page = lexicon_get("/tracks", json_body=body, limit=limit, offset=0, **query)
        return page.get("tracks", []) if isinstance(page, dict) else page

    out, offset = [], 0
    while True:
        page = lexicon_get("/tracks", json_body=body, limit=1000, offset=offset, **query)
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
