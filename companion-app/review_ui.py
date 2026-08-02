#!/usr/bin/env python3
"""Genre/Subgenre review screen.

NiceGUI, run in native mode (pywebview desktop window, not a browser
tab). The whole workflow lives here now - generate a plan, review it,
save decisions - so nothing needs the terminal except launching this
one file.

Layout:
- "Generate Plan" (with an optional tracks-to-scan limit) runs the
  whole load -> fetch -> score pipeline in-process via plan.py's
  generate_plan(), with a live per-track progress bar - a full-library
  run takes several minutes (rate-limited API calls + audio inference
  per track), so this has to show it's working, not just freeze.
- Tracks grouped in expandable sections. Per candidate row: checkbox
  (default unchecked) + tag name + confidence bar/percentage.
- A "create" row (tag doesn't exist yet) also gets a category picker,
  defaulting to new_tag_category from source_weights.yaml if plan.py
  could resolve one, but always changeable here - category choice is a
  review-time decision, not something to predict beforehand.
- Source, per-source note, and links live behind a `⋮` overflow menu.
- "Save Decisions" commits every checked row via apply.apply_decisions().
"""

from __future__ import annotations

import json
from pathlib import Path

from nicegui import run, ui

import apply
import lexicon_client
import plan as plan_module

PLAN_FILE = Path(__file__).resolve().parent / "genre_plan.json"


def load_plan(path: Path) -> dict | None:
    if not path.exists():
        return None
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


def build_ui() -> None:
    state = {"plan": load_plan(PLAN_FILE)}
    progress = {"current": 0, "total": 0, "artist": "", "title": ""}

    ui.label("Track Record — Genre/Subgenre Review").classes("text-xl font-bold")

    with ui.row().classes("items-center gap-2"):
        limit_input = ui.number(label="tracks to scan (blank = whole library)", min=1).classes("w-64")
        generate_button = ui.button("Generate Plan")

    progress_label = ui.label("").classes("text-gray-500")
    progress_bar = ui.linear_progress(value=0, show_value=False).classes("w-64")
    progress_bar.visible = False

    def update_progress() -> None:
        if progress["total"]:
            progress_label.text = f"[{progress['current']}/{progress['total']}] {progress['artist']} — {progress['title']}"
            progress_bar.value = progress["current"] / progress["total"]

    ui.timer(0.3, update_progress)

    @ui.refreshable
    def review_section() -> None:
        plan = state["plan"]
        if not plan or (not plan.get("review") and not plan.get("create")):
            ui.label(
                "No plan yet - click Generate Plan above, or run it and "
                "everything auto-included."
            ).classes("text-gray-500")
            return

        categories = lexicon_client.fetch_categories()
        category_options = {c["id"]: c["label"] for c in categories}

        tracks = group_by_track(plan.get("review", []), plan.get("create", []))
        entries: list[tuple[dict, ui.checkbox, ui.select | None]] = []

        ui.label(f"{len(tracks)} track(s) need a decision").classes("text-gray-500")

        for _, info in sorted(tracks.items(), key=lambda kv: ((kv[1]["artist"] or ""), kv[1]["title"] or "")):
            rows = sorted(info["rows"], key=lambda r: -r["confidence"])
            with ui.expansion(f"{info['artist']} — {info['title']}", caption=f"{len(rows)} candidate(s)").classes("w-full"):
                for row in rows:
                    with ui.row().classes("items-center w-full gap-2 py-1"):
                        cb = ui.checkbox(value=False)

                        label = row["tag"] + ("  (new tag)" if row["is_new"] else "")
                        ui.label(label).classes("flex-grow")

                        select = None
                        if row["is_new"]:
                            select = ui.select(
                                category_options,
                                value=row.get("suggested_category_id"),
                                label="category",
                            ).classes("w-40")

                        ui.linear_progress(value=row["confidence"], show_value=False).classes("w-24")
                        ui.label(f"{row['confidence']:.0%}").classes("w-12 text-right")

                        if row.get("low_confidence"):
                            ui.icon("warning", color="orange").tooltip("Low confidence")

                        with ui.button(icon="more_vert").props("flat round dense"):
                            with ui.menu():
                                for src in row["sources"]:
                                    text = f"{src['source']}: {src.get('note') or ''}"
                                    item = ui.menu_item(text)
                                    if src.get("url"):
                                        item.on("click", lambda url=src["url"]: ui.navigate.to(url, new_tab=True))

                        entries.append((row, cb, select))

        async def save():
            approved_review = [r for r, cb, _ in entries if cb.value and not r["is_new"]]
            approved_create = []
            skipped_no_category = 0
            for r, cb, select in entries:
                if not cb.value or not r["is_new"]:
                    continue
                category_id = select.value if select else None
                if category_id is None:
                    skipped_no_category += 1
                    continue
                approved_create.append({**r, "category_id": category_id})

            if not approved_review and not approved_create:
                msg = "Nothing checked - nothing to save"
                if skipped_no_category:
                    msg = f"{skipped_no_category} new-tag row(s) checked but no category chosen - pick one first"
                ui.notify(msg, type="warning")
                return

            n = await run.io_bound(apply.apply_decisions, approved_review, approved_create)
            msg = f"Applied {n} track(s)"
            if skipped_no_category:
                msg += f" - skipped {skipped_no_category} new-tag row(s) with no category chosen"
            ui.notify(msg, type="positive")

        ui.button("Save Decisions", on_click=save).props("color=primary").classes("mt-4")

    review_section()

    async def generate():
        generate_button.disable()
        progress_bar.visible = True
        progress.update(current=0, total=0, artist="", title="")
        progress_label.text = "starting..."

        def on_track_planned(i, total, track, _result):
            progress.update(current=i, total=total, artist=track.get("artist") or "", title=track.get("title") or "")

        limit = int(limit_input.value) if limit_input.value else None

        try:
            new_plan = await run.io_bound(
                plan_module.generate_plan,
                limit=limit,
                on_track_planned=on_track_planned,
            )
        except Exception as e:
            ui.notify(f"Plan generation failed: {e}", type="negative")
            return
        finally:
            progress_bar.visible = False
            generate_button.enable()

        state["plan"] = new_plan
        review_section.refresh()
        ui.notify(
            f"Plan ready: {len(new_plan['auto'])} auto-include, "
            f"{len(new_plan['review'])} need review, "
            f"{len(new_plan['create'])} propose a new tag",
            type="positive",
        )

    generate_button.on_click(generate)


def main():
    build_ui()
    ui.run(native=True, window_size=(900, 700), reload=False, title="Track Record - Genre Review")


if __name__ in {"__main__", "__mp_main__"}:
    main()
