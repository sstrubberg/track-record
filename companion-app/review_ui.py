#!/usr/bin/env python3
"""Genre/Subgenre review screen.

NiceGUI, run in native mode (pywebview desktop window, not a browser
tab), opened on demand rather than as a background service. Reads a
plan written by plan.py - auto-included tags never show up here, only
"review" (existing tags, needs a human decision) and "create" (tag
doesn't exist yet, needs a human decision either way per the earlier
design call: creating a tag is always review-gated).

Layout:
- Tracks grouped in expandable sections.
- Per candidate row: checkbox (default unchecked) + tag name +
  confidence bar/percentage - nothing else inline.
- Source, per-source note, and links live behind a `⋮` overflow menu.
- "Save Decisions" commits every checked row via apply.apply_decisions().
"""

from __future__ import annotations

import json
from pathlib import Path

from nicegui import run, ui

import apply

PLAN_FILE = Path(__file__).resolve().parent / "genre_plan.json"


def load_plan(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"no {path} - run plan.py first")
    return json.loads(path.read_text())


def group_by_track(review_rows: list[dict], create_rows: list[dict]) -> dict:
    tracks: dict[int, dict] = {}
    for row in review_rows:
        t = tracks.setdefault(row["track_id"], {
            "artist": row["artist"], "title": row["title"], "rows": [],
        })
        t["rows"].append({**row, "is_new": False})
    for row in create_rows:
        t = tracks.setdefault(row["track_id"], {
            "artist": row["artist"], "title": row["title"], "rows": [],
        })
        t["rows"].append({**row, "is_new": True})
    return tracks


def build_ui(plan: dict) -> None:
    tracks = group_by_track(plan.get("review", []), plan.get("create", []))
    checkboxes: list[tuple[dict, ui.checkbox]] = []

    ui.label("Track Record — Genre/Subgenre Review").classes("text-xl font-bold")
    ui.label(f"{len(tracks)} track(s) need a decision").classes("text-gray-500")

    for _, info in sorted(tracks.items(), key=lambda kv: ((kv[1]["artist"] or ""), kv[1]["title"] or "")):
        rows = sorted(info["rows"], key=lambda r: -r["confidence"])
        with ui.expansion(f"{info['artist']} — {info['title']}", caption=f"{len(rows)} candidate(s)").classes("w-full"):
            for row in rows:
                with ui.row().classes("items-center w-full gap-2 py-1"):
                    cb = ui.checkbox(value=False)
                    checkboxes.append((row, cb))

                    label = row["tag"] + ("  (new tag)" if row["is_new"] else "")
                    ui.label(label).classes("flex-grow")

                    ui.linear_progress(value=row["confidence"], show_value=False).classes("w-24")
                    ui.label(f"{row['confidence']:.0%}").classes("w-12 text-right")

                    if row.get("low_confidence"):
                        ui.icon("warning", color="orange").tooltip("Low confidence")

                    with ui.button(icon="more_vert").props("flat round dense"):
                        with ui.menu() as menu:
                            for src in row["sources"]:
                                text = f"{src['source']}: {src.get('note') or ''}"
                                item = ui.menu_item(text)
                                if src.get("url"):
                                    item.on("click", lambda url=src["url"]: ui.navigate.to(url, new_tab=True))

    async def save():
        approved_review = [r for r, cb in checkboxes if cb.value and not r["is_new"]]
        approved_create = [r for r, cb in checkboxes if cb.value and r["is_new"]]
        if not approved_review and not approved_create:
            ui.notify("Nothing checked - nothing to save", type="warning")
            return
        n = await run.io_bound(apply.apply_decisions, approved_review, approved_create)
        ui.notify(f"Applied {n} track(s)", type="positive")

    ui.button("Save Decisions", on_click=save).props("color=primary").classes("mt-4")


def main():
    plan = load_plan(PLAN_FILE)
    if not plan.get("review") and not plan.get("create"):
        print("nothing needs review - run plan.py first, or everything auto-included")
        return
    build_ui(plan)
    ui.run(native=True, window_size=(900, 700), reload=False, title="Track Record - Genre Review")


if __name__ in {"__main__", "__mp_main__"}:
    main()
