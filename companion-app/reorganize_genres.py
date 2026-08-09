#!/usr/bin/env python3
"""Reorganize existing genre/subgenre Custom Tags into per-family
Lexicon categories, based on config/genre_taxonomy.yaml.

Lexicon's categories are flat - no nesting/parent field (confirmed
directly against a live instance). This project's own DJ had already
been hand-building a family-like structure using that flat system
before this script existed - categories named "Sub-genre - Electronic",
"Sub-genre - Rock", "Sub-genre - Pop", "Sub-genre - R&B", a flat
"Genre" catch-all, and a standalone "Reggae" category. This script
looks for that same convention ("Sub-genre - {Family}") for the rest
of the Discogs 400-style family list too. A family with matching tags
but no such category yet is reported separately ("needs a category"),
not silently created as a side effect of a move - but unlike the main
Genre/Subgenre review flow's rule (apply.py never creates a category
as a side effect of approving a new tag), this module can create one
via create_categories(), a separate, explicit, previewed, confirmed
action (review_ui.py's "Create Categories" button, or plan a category
list yourself and call it directly). That's deliberate, not an
oversight: an empty category is safe and fully reversible on its own
(see lexicon_client.create_category()'s own docstring) - the actual
risk is generic to Lexicon's own category deletion once tags have
moved into it, already gated behind apply_moves()'s own confirmation,
unrelated to who created the category in the first place.

Deliberately scoped to *moving tags between categories* (and, given
explicit confirmation, creating the categories to move them into) -
never renames a tag's label and never merges two tags into one. Both
of those touch more than a tag's own metadata (a rename changes what a
DJ sees everywhere that tag is applied; a merge means re-pointing
every track that carries the old tag and then deleting it) - a bigger,
separate decision than "which category does this already-correct tag
live in," left for later if it's ever wanted.

"Active" (which families/subgenres this run actually considers) is
computed fresh from the live library every run, not read from the
taxonomy file's own active: false defaults (see genre_taxonomy.py's
docstring) - a family/subgenre counts as active here if at least one
existing Lexicon tag's label matches it (via genre_taxonomy.build_lookup(),
case/hyphen/whitespace-insensitive, canonical name or any filled-in
source alias).

A subgenre name that genuinely belongs to more than one family in the
Discogs taxonomy itself (e.g. "Disco" is both an Electronic style and
a Funk/Soul style - see genre_taxonomy.build_lookup()'s docstring for
the full list) is reported separately as ambiguous, never auto-resolved
- plan_moves()'s resolved_ambiguous param takes a DJ's explicit pick
instead (review_ui.py's Ambiguous section offers this as clickable
choices).

Always a dry run - reports what WOULD move, changes nothing - unless
--apply is passed, same convention as billboard_tag.py/apply.py's own
--dry-run-by-default write scripts. The move itself: PATCH /tag
{"id": tag_id, "categoryId": new_id} - a flat body, no "edits" wrapper
(confirmed directly against a live instance; /tag does NOT take the
same {"id", "edits": {...}} shape /track does - a 400 "'edits' is not
allowed" is what a wrapped body gets back).

    python reorganize_genres.py              # report only, writes nothing
    python reorganize_genres.py --apply       # actually move tags
"""

from __future__ import annotations

import argparse

import requests

import genre_taxonomy
import lexicon_client

CATEGORY_PREFIX = "Sub-genre - "


def plan_moves(resolved_ambiguous: dict[str, tuple[str, str, str, str]] | None = None) -> dict:
    """Read-only: fetches the live library's tags/categories and the
    taxonomy, and returns everything a report or an apply pass needs.
    Never writes anything itself.

    resolved_ambiguous, if given: {normalized_tag_name: (family_key,
    family_canonical, subgenre_key, subgenre_canonical)} - a DJ's
    explicit pick among an ambiguous name's candidate families (see
    genre_taxonomy.build_lookup()'s own docstring for why some names
    are ambiguous in the first place). Merged straight into the
    unambiguous lookup before matching runs, so a resolved name flows
    through the exact same moves/already_correct/needs_category
    bucketing as any other match - resolving isn't a separate code
    path, just a DJ-supplied answer to a question the taxonomy alone
    couldn't answer. Keyed by normalized name (not a specific Lexicon
    tag id), since the ambiguity is a taxonomy-level fact - "Disco
    belongs to Electronic" applies to that name everywhere it shows up.
    """
    taxonomy = genre_taxonomy.load_taxonomy()
    lookup, ambiguous_names = genre_taxonomy.build_lookup(taxonomy)
    for norm_name, choice in (resolved_ambiguous or {}).items():
        if norm_name in ambiguous_names:
            lookup[norm_name] = choice
            del ambiguous_names[norm_name]
    tags, category_labels = lexicon_client.fetch_tags_with_categories()
    label_to_category_id = {label: cid for cid, label in category_labels.items()}

    moves = []  # matched, target category exists, categoryId needs to change
    already_correct = []  # matched, already in the right category
    needs_category = []  # matched, but "Sub-genre - {Family}" doesn't exist yet
    ambiguous = []  # matched a name that's ambiguous across families
    unmatched = []  # no taxonomy match at all - left alone either way

    for tag in tags:
        norm = lexicon_client._normalize_label(tag["label"])
        current_category = category_labels.get(tag["categoryId"], "(no category)")

        if norm in ambiguous_names:
            candidates = ambiguous_names[norm]
            # A tag flagged ambiguous by name alone might already be
            # sitting in one of its valid candidate categories - e.g. a
            # DJ resolved it in an earlier session, checked it, and
            # moved it via apply_moves(), but nothing remembers that
            # resolution across a later Check Genre Organization rerun
            # (resolved_ambiguous above only ever covers the *current*
            # call - review_ui.py's copy of it lives in that session's
            # own state, not written anywhere plan_moves() can see on
            # its own). Without this check, an already-resolved-and-
            # moved tag would reappear as "ambiguous" forever, reading
            # as if the earlier work never happened even though it
            # genuinely did - reported directly, confirmed directly
            # (all 12 ambiguous tags in a real run turned out to
            # already be correctly placed).
            resolved_match = next(
                (c for c in candidates if current_category == f"{CATEGORY_PREFIX}{c[1]}"),
                None,
            )
            if resolved_match is not None:
                family_key, family_canonical, subgenre_key, subgenre_canonical = resolved_match
                already_correct.append({
                    **tag,
                    "current_category": current_category,
                    "family": family_canonical,
                    "subgenre": subgenre_canonical,
                    "target_category": current_category,
                })
            else:
                ambiguous.append({**tag, "current_category": current_category, "candidates": candidates})
            continue

        match = lookup.get(norm)
        if match is None:
            unmatched.append({**tag, "current_category": current_category})
            continue

        family_key, family_canonical, subgenre_key, subgenre_canonical = match
        target_category = f"{CATEGORY_PREFIX}{family_canonical}"
        row = {
            **tag,
            "current_category": current_category,
            "family": family_canonical,
            "subgenre": subgenre_canonical,
            "target_category": target_category,
        }

        target_category_id = label_to_category_id.get(target_category)
        if target_category_id is None:
            needs_category.append(row)
        elif current_category == target_category:
            already_correct.append(row)
        else:
            moves.append({**row, "target_category_id": target_category_id})

    return {
        "moves": moves,
        "already_correct": already_correct,
        "needs_category": needs_category,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
    }


def print_report(plan: dict) -> None:
    moves, already_correct, needs_category, ambiguous, unmatched = (
        plan["moves"], plan["already_correct"], plan["needs_category"], plan["ambiguous"], plan["unmatched"]
    )

    by_target: dict[str, list[dict]] = {}
    for row in moves:
        by_target.setdefault(row["target_category"], []).append(row)

    print(
        f"{len(moves)} tag(s) would move, {len(already_correct)} already in the right category, "
        f"{len(needs_category)} need a category created first, {len(ambiguous)} ambiguous, "
        f"{len(unmatched)} not in this taxonomy at all\n"
    )

    if moves:
        print("=== WOULD MOVE ===")
        for target, rows in sorted(by_target.items()):
            existing_targets = {r["current_category"] for r in rows} - {"(no category)"}
            print(f"\n{target}  ({len(rows)} tag(s))")
            for r in sorted(rows, key=lambda r: r["subgenre"]):
                print(f"    {r['label']!r} (id={r['id']})  {r['current_category']!r} -> {target!r}")
            # A tag leaving a category the DJ named by hand (not this
            # script's own "Sub-genre - X" convention) is exactly the
            # kind of consequence worth calling out explicitly, not
            # just leaving buried in the row-by-row list above.
            hand_named = {c for c in existing_targets if not c.startswith(CATEGORY_PREFIX)}
            if hand_named:
                print(
                    f"    NOTE: pulls tag(s) out of your own existing categor"
                    f"{'y' if len(hand_named) == 1 else 'ies'} {sorted(hand_named)}"
                )

    if needs_category:
        by_family: dict[str, list[dict]] = {}
        for row in needs_category:
            by_family.setdefault(row["target_category"], []).append(row)
        print("\n=== NEEDS A CATEGORY CREATED FIRST (this script never creates one) ===")
        for target, rows in sorted(by_family.items()):
            tags_preview = ", ".join(r["label"] for r in rows[:5])
            more = f" (+{len(rows) - 5} more)" if len(rows) > 5 else ""
            print(f"  Create {target!r} in Lexicon, then rerun, to move: {tags_preview}{more}")

    if ambiguous:
        print("\n=== AMBIGUOUS - matches more than one family, skipped ===")
        for row in ambiguous:
            options = ", ".join(f"{fam} ({sub})" for fam, sub in row["candidates"])
            print(f"  {row['label']!r} (id={row['id']}, currently in {row['current_category']!r}) could be: {options}")

    if unmatched:
        print(f"\n=== NOT IN THIS TAXONOMY ({len(unmatched)}) - left alone ===")
        by_cat: dict[str, int] = {}
        for row in unmatched:
            by_cat[row["current_category"]] = by_cat.get(row["current_category"], 0) + 1
        for cat, n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>3}  {cat}")


def apply_moves(moves: list[dict]) -> dict:
    """Actually move each given row's tag to its target_category_id.
    Returns {"ok": int, "failures": [{"label", "id", "error"}, ...]} -
    structured rather than printed directly, so review_ui.py's own
    "Reorganize Genre Tags" section can report the same outcome as a
    notification instead of stdout only reaching the CLI caller."""
    ok = 0
    failures = []
    for row in moves:
        r = requests.patch(
            f"{lexicon_client.LEXICON}/tag",
            json={"id": row["id"], "categoryId": row["target_category_id"]},
            timeout=15,
        )
        if r.ok:
            ok += 1
        else:
            failures.append({"label": row["label"], "id": row["id"], "error": f"HTTP {r.status_code} {r.text[:150]}"})
    return {"ok": ok, "failures": failures}


def create_categories(labels: list[str]) -> dict:
    """Actually create each given category (each starts empty - see
    lexicon_client.create_category()'s own docstring for why that
    makes this safe and reversible on its own). Returns {"ok":
    [{"label", "id"}, ...], "failures": [{"label", "error"}, ...]} -
    structured the same way apply_moves() is, for the same reason
    (review_ui.py reports this as a notification, not stdout).

    Callers should only ever pass labels plan_moves() has already
    confirmed have no matching category yet (its own needs_category
    bucket) - this function doesn't re-check that itself, so calling
    it twice for the same label would create a duplicate category
    rather than erroring."""
    ok = []
    failures = []
    for label in labels:
        try:
            category_id = lexicon_client.create_category(label)
            ok.append({"label": label, "id": category_id})
        except requests.RequestException as e:
            failures.append({"label": label, "error": str(e)})
    return {"ok": ok, "failures": failures}


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--apply", action="store_true", help="actually move tags (default: report only, writes nothing)")
    args = p.parse_args()

    plan = plan_moves()
    print_report(plan)

    if not args.apply:
        print("\n(dry run - nothing written; pass --apply to actually move these tags)")
        return

    if not plan["moves"]:
        print("\nnothing to move")
        return

    result = apply_moves(plan["moves"])
    for f in result["failures"]:
        print(f"  FAILED {f['label']!r} (id={f['id']}): {f['error']}")
    print(f"\n{result['ok']} tag(s) moved, {len(result['failures'])} failed")


if __name__ == "__main__":
    main()
