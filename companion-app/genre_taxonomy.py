"""Loads config/genre_taxonomy.yaml (Discogs 400-style genre families ->
subgenres - see that file's own _meta block for provenance and schema)
and builds the label-matching lookup reorganize_genres.py needs.

Deliberately does NOT read or write the file's own "active" flags.
Every family/subgenre in the file ships with active: false - the DJ
this was built for hasn't hand-curated a working set in it, and
reorganize_genres.py doesn't need one: "active" there means "has at
least one matching tag already in this Lexicon library," computed
fresh from the live library every run (see that module's docstring),
not something curated by hand in this file ahead of time.
"""

from __future__ import annotations

from pathlib import Path

import yaml

# Reuses lexicon_client's own case/hyphen/whitespace-insensitive
# normalization (the same rule it already applies when matching a
# proposed tag name against an existing Lexicon label) rather than
# introducing a second normalization scheme - a taxonomy canonical name
# or source alias is exactly the same kind of "does this label already
# exist under a slightly different spelling" question.
from lexicon_client import _normalize_label

TAXONOMY_FILE = Path(__file__).resolve().parent / "config" / "genre_taxonomy.yaml"


def load_taxonomy(path: Path | None = None) -> dict:
    return yaml.safe_load((path or TAXONOMY_FILE).read_text())


def list_families(taxonomy: dict) -> list[tuple[str, str]]:
    """Return every (family_key, family_canonical) in the taxonomy,
    sorted by canonical name - all 15, not just ones with a matching
    tag already in the library ("active", per build_lookup()'s own
    docstring). review_ui.py's manual-assignment picker for a tag that
    matched nothing needs the full list: a DJ placing a tag by hand is
    exactly the case where a family might not have any other matching
    tag yet."""
    families = taxonomy.get("genre_families") or {}
    return sorted(
        ((key, f.get("canonical") or key) for key, f in families.items()),
        key=lambda kv: kv[1],
    )


def build_lookup(taxonomy: dict) -> tuple[dict[str, tuple[str, str, str, str]], dict[str, list[tuple[str, str]]]]:
    """Return (lookup, ambiguous).

    lookup: {normalized_name: (family_key, family_canonical,
    subgenre_key, subgenre_canonical)} - one entry per name a real
    Lexicon tag might plausibly carry for a given subgenre: its own
    canonical spelling plus every non-null source_alias
    (discogs/discogs_maest/beatport/spotify/musicbrainz). discogs and
    discogs_maest are always identical to canonical in the source file
    (same taxonomy, per its own _meta notes) so those two rarely add a
    new key on their own - this mostly exists for whichever of
    beatport/spotify/musicbrainz have been filled in.

    ambiguous: {normalized_name: [(family_key, family_canonical, subgenre_key,
    subgenre_canonical), ...]} - same 4-tuple shape as a `lookup` value,
    not just the display pair, so a DJ's explicit choice among these
    candidates (see reorganize_genres.plan_moves()'s resolved_ambiguous
    param) can be dropped straight into `lookup` with no reshaping.
    A real, non-buggy property of Discogs' own taxonomy, not a data
    error to paper over: the same style name genuinely appears under
    more than one family (e.g. "Disco" is both an Electronic style and
    a Funk/Soul style; "Electro" is both Electronic and Hip Hop).
    Silently picking one via dict overwrite would move a tag into a
    family the DJ never actually meant - these names are excluded from
    `lookup` entirely and reported separately instead, left for a human
    decision rather than an automatic (and possibly wrong) one.
    """
    seen: dict[str, list[tuple[str, str, str, str]]] = {}
    for family_key, family in (taxonomy.get("genre_families") or {}).items():
        family_canonical = family.get("canonical") or family_key
        # A bare tag whose name matches a family's own canonical name
        # is itself a candidate, registered the same way a subgenre
        # entry is - e.g. a tag simply named "Hip Hop" most naturally
        # means the genre Hip Hop, but Discogs' own real taxonomy also
        # genuinely lists "Hip Hop" as a style tag under Electronic
        # (not a data error - a real overlap, same shape as "Disco"
        # being both an Electronic and a Funk/Soul style). Without
        # this, that second, buried entry was the *only* candidate
        # "Hip Hop" ever matched, so it silently won by being the only
        # option found - reported directly (moved into Sub-genre -
        # Electronic, which a DJ correctly didn't expect). Registering
        # the family-self match here lets it compete through the exact
        # same ambiguity detection as any other genuinely-shared name,
        # subgenre_key "__self__" marking that this candidate points at
        # the family's own category rather than one of its subgenres.
        seen.setdefault(_normalize_label(family_canonical), []).append(
            (family_key, family_canonical, "__self__", family_canonical)
        )
        for subgenre_key, subgenre in (family.get("subgenres") or {}).items():
            subgenre_canonical = subgenre.get("canonical") or subgenre_key
            names = {subgenre_canonical}
            for alias in (subgenre.get("source_aliases") or {}).values():
                if alias:
                    names.add(alias)
            for name in names:
                entry = (family_key, family_canonical, subgenre_key, subgenre_canonical)
                seen.setdefault(_normalize_label(name), []).append(entry)

    lookup, ambiguous = {}, {}
    for norm_name, entries in seen.items():
        # More than one *distinct family* claiming this name is the real
        # ambiguity worth blocking on - the same subgenre showing up
        # twice (e.g. matched by both its canonical spelling and an
        # identical discogs_maest alias) is not.
        distinct_families = {e[1] for e in entries}
        if len(distinct_families) > 1:
            ambiguous[norm_name] = sorted(set(entries), key=lambda e: (e[1], e[3]))
        else:
            lookup[norm_name] = entries[0]
    return lookup, ambiguous


if __name__ == "__main__":
    taxonomy = load_taxonomy()
    lookup, ambiguous = build_lookup(taxonomy)
    n_families = len(taxonomy.get("genre_families") or {})
    n_subgenres = sum(len(f.get("subgenres") or {}) for f in (taxonomy.get("genre_families") or {}).values())
    print(f"{n_families} families, {n_subgenres} subgenres, {len(lookup)} matchable name(s), {len(ambiguous)} ambiguous name(s)")
    for norm_name, options in ambiguous.items():
        print(f"  ambiguous: {norm_name!r} -> {options}")
