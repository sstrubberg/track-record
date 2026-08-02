"""NiceGUI review screen, run in native mode (pywebview desktop window,
not a browser tab), opened on demand rather than as a background service.

Layout (see spec):
- Tracks grouped in expandable sections; auto-included tags never shown here.
- Per candidate row: checkbox (default unchecked) + tag name + confidence
  bar/percentage - nothing else inline.
- Source, per-source note, and links live behind a `⋮` overflow control.
- "Save Decisions" commits checked rows via apply.apply_tags().
"""

from __future__ import annotations


def run(scored_tracks: list[dict]) -> None:
    """Launch the native review window for tracks needing a human decision."""
    raise NotImplementedError
