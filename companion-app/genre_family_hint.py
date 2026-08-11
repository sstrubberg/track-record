"""Suggests a Sub-genre - {Family} category for a brand-new genre/
subgenre tag plan.py is about to propose, based on config/
genre_taxonomy.yaml (Discogs' own 400-style family/subgenre list).

Deliberately narrow - this project shipped a much bigger "Reorganize
Genre Tags" workflow on this same taxonomy file (renaming/moving/
merging *existing* tags, an ambiguous-tag picker, its own screen) and
then removed it entirely on direct feedback that it overcomplicated
the tool's intended use. This isn't that: no renaming, no moving
existing tags, no picker, no new screen. It only ever changes what's
pre-filled in the category dropdown a "create" row already shows in
the main review screen - a smarter default for an element that already
existed, not a new feature. The DJ can always change it, exactly like
new_tag_category's own default already works, and it never creates a
category that doesn't already exist (see resolve_family_category_ids).

A name that genuinely belongs to more than one family (e.g. "Disco" is
both an Electronic style and a Funk/Soul style) is deliberately left
unmatched - the existing new_tag_category default is the right
fallback for that case, not a second resolution UI mirroring the
removed workflow's Ambiguous section.
"""

from __future__ import annotations

from pathlib import Path

import yaml

import lexicon_client

TAXONOMY_FILE = Path(__file__).resolve().parent / "config" / "genre_taxonomy.yaml"
CATEGORY_PREFIX = "Sub-genre - "


def build_family_hints() -> dict[str, str]:
    """Returns {normalized_tag_name: family_canonical} - only for names
    that unambiguously belong to exactly one family (a family's own
    canonical name, or a subgenre's canonical name/any filled-in
    source alias). A name shared by more than one family - the same
    real ambiguity the old Reorganize workflow reported separately -
    is left out of this dict entirely rather than guessed at."""
    taxonomy = yaml.safe_load(TAXONOMY_FILE.read_text())
    seen: dict[str, set[str]] = {}
    for family_key, family in (taxonomy.get("genre_families") or {}).items():
        family_canonical = family.get("canonical") or family_key
        seen.setdefault(lexicon_client._normalize_label(family_canonical), set()).add(family_canonical)
        for subgenre_key, subgenre in (family.get("subgenres") or {}).items():
            subgenre_canonical = subgenre.get("canonical") or subgenre_key
            names = {subgenre_canonical}
            for alias in (subgenre.get("source_aliases") or {}).values():
                if alias:
                    names.add(alias)
            for name in names:
                seen.setdefault(lexicon_client._normalize_label(name), set()).add(family_canonical)
    return {norm: next(iter(families)) for norm, families in seen.items() if len(families) == 1}


def resolve_family_category_ids(hints: dict[str, str], categories: list[dict]) -> dict[str, int]:
    """{family_canonical: category_id}, only for families whose "Sub-genre
    - {Family}" category already exists in this Lexicon library - a
    family with no category yet just gets no suggestion (falls back to
    the normal new_tag_category default), since this never creates a
    category on anyone's behalf."""
    label_to_id = {c["label"]: c["id"] for c in categories}
    result = {}
    for family_canonical in set(hints.values()):
        cat_id = label_to_id.get(f"{CATEGORY_PREFIX}{family_canonical}")
        if cat_id is not None:
            result[family_canonical] = cat_id
    return result
