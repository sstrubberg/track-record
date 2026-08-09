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

Scoped to *moving tags between categories* and *renaming a tag to a
taxonomy spelling it's already recognized as* (given explicit
confirmation for both) - still never merges two tags into one, that
being the one move here genuinely bigger than "what does this tag's
own metadata say": it means re-pointing every track that carries the
old tag and then deleting it, not just updating a label or a
categoryId. Renaming used to be off the table entirely here too - the
DJ this was built for reported hand-created tags "all over the place,"
inconsistently spelled as well as inconsistently categorized, and
asked for both to get fixed in the same pass. What makes renaming safe
enough to add: it's only ever proposed for a tag whose name already
*is* a taxonomy spelling (its own canonical name, or a listed source
alias, case/hyphen/whitespace differences aside) - never for a tag a
DJ manually assigned to a family after it matched nothing (see
manual_assignments below). Renaming "hiphop" to "Hip Hop" is a spelling
fix; renaming "Deep House Tribute Mashup" to "Electronic" because a DJ
said that ambiguous label belongs there would erase real information a
bare family name can't replace - plan_moves() keeps these two cases
structurally distinct so the second is never proposed by mistake.

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
choices). A tag matching nothing in the taxonomy at all is reported as
unmatched - plan_moves()'s manual_assignments param takes a DJ's
explicit family pick for these too (review_ui.py's own section), but
unlike resolved_ambiguous, a manual assignment never implies a rename -
see above for why.

Always a dry run - reports what WOULD change, changes nothing - unless
--apply is passed, same convention as billboard_tag.py/apply.py's own
--dry-run-by-default write scripts. The change itself: PATCH /tag
{"id": tag_id, "categoryId": new_id, "label": new_label} (either field
omitted if that part of this tag isn't changing) - a flat body, no
"edits" wrapper (confirmed directly against a live instance; /tag does
NOT take the same {"id", "edits": {...}} shape /track does - a 400
"'edits' is not allowed" is what a wrapped body gets back).

    python reorganize_genres.py              # report only, writes nothing
    python reorganize_genres.py --apply       # actually move/rename tags
"""

from __future__ import annotations

import argparse

import requests

import genre_taxonomy
import lexicon_client

CATEGORY_PREFIX = "Sub-genre - "

# Every non-"Sub-genre - X" category this DJ's real Lexicon library uses
# for genre/subgenre tags (see module docstring: hand-built before this
# script existed) - checked alongside CATEGORY_PREFIX to decide whether
# a tag is even a genre/subgenre tag in the first place. Without this,
# fetch_tags_with_categories() (every Custom Tag in the library, not
# just genre ones) fed Mood, Mix, Event, Timing, Era, and Charts tags
# into the same matching loop - harmless while "unmatched" was a
# passive report nobody had to act on, but once it grew an interactive
# per-tag "assign to a family" picker (see plan_moves()'s
# manual_assignments param), showing a DJ a family picker next to
# "Halloween" or "Warmup" would be actively wrong, not just noisy.
# Caught directly: 122 of 258 "unmatched" tags in a real run turned out
# to be non-genre tags from six categories that were never in scope.
# Personal-tool territory (a single DJ's own real category names, not a
# general-purpose config) - add a name here if this DJ ever creates
# another genre-ish catch-all category outside the "Sub-genre - X"
# convention.
_OTHER_GENRE_CATEGORIES = {"Genre", "Subgenre", "Reggae"}


def _is_genre_like_category(category_label: str) -> bool:
    """A tag with no category at all is still considered in scope -
    see plan_moves()'s docstring for why an uncategorized tag is worth
    scanning rather than excluding outright."""
    return (
        category_label == "(no category)"
        or category_label in _OTHER_GENRE_CATEGORIES
        or category_label.startswith(CATEGORY_PREFIX)
    )


def plan_moves(
    resolved_ambiguous: dict[str, tuple[str, str, str, str]] | None = None,
    manual_assignments: dict[str, tuple[str, str, str, str]] | None = None,
) -> dict:
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

    manual_assignments, if given: same shape and same normalized-name
    key, but for a tag that matched *nothing* in the taxonomy at all -
    a DJ placing a genuinely custom tag name into a family by hand
    (review_ui.py's "Not in this taxonomy" section). Also merged into
    the lookup so it flows through the same bucketing, but tracked
    separately from resolved_ambiguous: unlike an ambiguous name (which
    by definition already *is* a real taxonomy spelling, just an
    overlapping one), a manually-assigned name might be nothing like
    its target family's canonical spelling - "Deep House Tribute
    Mashup" placed under Electronic shouldn't get renamed to
    "Electronic" just because that's the category it's moving into.
    See module docstring for the full reasoning. rows built from a
    manual assignment always carry needs_rename=False regardless of
    how their label compares to the family's canonical name.

    Only ever considers tags whose *current* category is genre-like
    (see _is_genre_like_category) - a Mood tag named "Dark" or a Mix
    cue tag named "Blend" was never a candidate for this taxonomy in
    the first place and has no business in any bucket below, matched
    or not.
    """
    taxonomy = genre_taxonomy.load_taxonomy()
    lookup, ambiguous_names = genre_taxonomy.build_lookup(taxonomy)
    for norm_name, choice in (resolved_ambiguous or {}).items():
        if norm_name in ambiguous_names:
            lookup[norm_name] = choice
            del ambiguous_names[norm_name]
    manually_placed_norms: set[str] = set()
    for norm_name, choice in (manual_assignments or {}).items():
        if norm_name not in lookup and norm_name not in ambiguous_names:
            lookup[norm_name] = choice
            manually_placed_norms.add(norm_name)
    all_tags, category_labels = lexicon_client.fetch_tags_with_categories()
    tags = [t for t in all_tags if _is_genre_like_category(category_labels.get(t["categoryId"], "(no category)"))]
    label_to_category_id = {label: cid for cid, label in category_labels.items()}

    moves = []  # matched, target category exists, categoryId (and maybe label) needs to change
    already_correct = []  # matched, already in the right category (may still need a rename - see renames)
    renames = []  # subset of already_correct where the label alone needs to change
    needs_category = []  # matched, but "Sub-genre - {Family}" doesn't exist yet
    ambiguous = []  # matched a name that's ambiguous across families
    unmatched = []  # no taxonomy match at all, and no manual_assignments entry either

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
                needs_rename = tag["label"] != subgenre_canonical
                row = {
                    **tag,
                    "current_category": current_category,
                    "family": family_canonical,
                    "subgenre": subgenre_canonical,
                    "target_category": current_category,
                    "canonical_label": subgenre_canonical,
                    "needs_rename": needs_rename,
                    "manually_placed": False,
                }
                already_correct.append(row)
                if needs_rename:
                    renames.append(row)
            else:
                ambiguous.append({**tag, "current_category": current_category, "candidates": candidates})
            continue

        match = lookup.get(norm)
        if match is None:
            unmatched.append({**tag, "current_category": current_category})
            continue

        family_key, family_canonical, subgenre_key, subgenre_canonical = match
        target_category = f"{CATEGORY_PREFIX}{family_canonical}"
        manually_placed = norm in manually_placed_norms
        needs_rename = (not manually_placed) and tag["label"] != subgenre_canonical
        row = {
            **tag,
            "current_category": current_category,
            "family": family_canonical,
            "subgenre": subgenre_canonical,
            "target_category": target_category,
            "canonical_label": None if manually_placed else subgenre_canonical,
            "needs_rename": needs_rename,
            "manually_placed": manually_placed,
        }

        target_category_id = label_to_category_id.get(target_category)
        if target_category_id is None:
            needs_category.append(row)
        elif current_category == target_category:
            already_correct.append(row)
            if needs_rename:
                renames.append(row)
        else:
            moves.append({**row, "target_category_id": target_category_id})

    return {
        "moves": moves,
        "already_correct": already_correct,
        "renames": renames,
        "needs_category": needs_category,
        "ambiguous": ambiguous,
        "unmatched": unmatched,
    }


def print_report(plan: dict) -> None:
    moves, already_correct, renames, needs_category, ambiguous, unmatched = (
        plan["moves"], plan["already_correct"], plan["renames"], plan["needs_category"],
        plan["ambiguous"], plan["unmatched"],
    )

    by_target: dict[str, list[dict]] = {}
    for row in moves:
        by_target.setdefault(row["target_category"], []).append(row)

    print(
        f"{len(moves)} tag(s) would move, {len(renames)} would rename in place, "
        f"{len(already_correct) - len(renames)} already correct, "
        f"{len(needs_category)} need a category created first, {len(ambiguous)} ambiguous, "
        f"{len(unmatched)} not in this taxonomy at all\n"
    )

    if moves:
        print("=== WOULD MOVE ===")
        for target, rows in sorted(by_target.items()):
            existing_targets = {r["current_category"] for r in rows} - {"(no category)"}
            print(f"\n{target}  ({len(rows)} tag(s))")
            for r in sorted(rows, key=lambda r: r["subgenre"]):
                rename_note = f" (rename to {r['canonical_label']!r})" if r["needs_rename"] else ""
                print(f"    {r['label']!r} (id={r['id']})  {r['current_category']!r} -> {target!r}{rename_note}")
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

    if renames:
        print("\n=== WOULD RENAME (category is already correct) ===")
        for r in sorted(renames, key=lambda r: r["label"]):
            print(f"    {r['label']!r} (id={r['id']}) -> {r['canonical_label']!r}")

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
        print(f"\n=== NOT IN THIS TAXONOMY ({len(unmatched)}) - left alone (CLI has no picker for these; review_ui.py does) ===")
        by_cat: dict[str, int] = {}
        for row in unmatched:
            by_cat[row["current_category"]] = by_cat.get(row["current_category"], 0) + 1
        for cat, n in sorted(by_cat.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>3}  {cat}")


def apply_moves(rows: list[dict]) -> dict:
    """Actually apply each given row's change(s): a categoryId move (if
    the row has a target_category_id - a "moves" row), a label rename
    (if the row has needs_rename=True and a canonical_label - a
    "renames" row, or a "moves" row that needs both at once), or both
    together in a single PATCH. Returns {"ok": int, "failures":
    [{"label", "id", "error"}, ...]} - structured rather than printed
    directly, so review_ui.py's own "Reorganize Genre Tags" section can
    report the same outcome as a notification instead of stdout only
    reaching the CLI caller.

    Takes rows from either plan_moves() bucket (moves or renames) or a
    mix of both in one list - the PATCH body is built from whichever
    fields the row actually carries, so this one function covers "Apply
    Checked Changes" applying both in a single pass."""
    ok = 0
    failures = []
    for row in rows:
        body: dict = {"id": row["id"]}
        if row.get("target_category_id") is not None:
            body["categoryId"] = row["target_category_id"]
        if row.get("needs_rename") and row.get("canonical_label"):
            body["label"] = row["canonical_label"]
        if len(body) == 1:
            # Nothing to actually change on this row (shouldn't happen -
            # callers filter to checked rows from moves/renames, both of
            # which always carry at least one real change - but skip
            # rather than send a no-op PATCH if it ever does).
            continue
        r = requests.patch(f"{lexicon_client.LEXICON}/tag", json=body, timeout=15)
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
    p.add_argument(
        "--apply", action="store_true",
        help="actually move/rename tags (default: report only, writes nothing)",
    )
    args = p.parse_args()

    plan = plan_moves()
    print_report(plan)

    if not args.apply:
        print("\n(dry run - nothing written; pass --apply to actually move/rename these tags)")
        return

    to_apply = plan["moves"] + plan["renames"]
    if not to_apply:
        print("\nnothing to move or rename")
        return

    result = apply_moves(to_apply)
    for f in result["failures"]:
        print(f"  FAILED {f['label']!r} (id={f['id']}): {f['error']}")
    print(f"\n{result['ok']} tag(s) changed, {len(result['failures'])} failed")


if __name__ == "__main__":
    main()
