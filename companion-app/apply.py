"""Writes approved tags to Lexicon via its Local API.

Same rule as the existing Billboard tool: merge, never replace. Lexicon's
`tags` field is flat, so this reads the track's live tag array and
appends rather than overwriting - a bare overwrite would wipe unrelated
tags. Only writes to custom tag categories that already exist in
Lexicon; never creates a new category on the user's behalf.
"""

from __future__ import annotations


def apply_tags(track_id: str, approved_tags: list[str]) -> None:
    """Merge approved_tags into the track's existing tags via the Lexicon
    Local API, then append the decision to applied_log.
    """
    raise NotImplementedError
