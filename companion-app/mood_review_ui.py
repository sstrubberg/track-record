#!/usr/bin/env python3
"""Mood/Theme review screen.

Same NiceGUI native-window shape as review_ui.py (Genre/Subgenre's own
copy of this file) - generate a plan, review it, save decisions, no
terminal needed beyond launching this file. See review_ui.py's
docstring for the full rationale behind the one-write-path model
(Generate Plan never writes anything; Save Decisions writes whatever's
checked, pre-checked auto-include rows included) - unchanged here,
just pointed at mood_plan.py / mood_apply.py / config/mood_weights.yaml
instead of their Genre/Subgenre counterparts.

Differences from review_ui.py, both because there's only one fetch
source for Mood/Theme (see fetch/audio_model_mood.py):
- No source-toggle checkboxes - nothing to toggle between yet.
- Runs on a different port (8081 vs. NiceGUI's 8080 default) so this
  and review_ui.py can both be open at once without one refusing to
  bind the other's port.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from nicegui import run, ui

import mood_apply as apply
import mood_plan as plan_module
import lexicon_client
import scoring

PLAN_FILE = Path(__file__).resolve().parent / "mood_plan.json"


def load_plan(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def group_by_track(auto_rows: list[dict], review_rows: list[dict], create_rows: list[dict]) -> dict:
    tracks: dict[int, dict] = {}
    for row in auto_rows:
        t = tracks.setdefault(row["track_id"], {
            "artist": row["artist"], "title": row["title"], "rows": [],
        })
        t["rows"].append({**row, "is_new": False, "is_auto": True})
    for row in review_rows:
        t = tracks.setdefault(row["track_id"], {
            "artist": row["artist"], "title": row["title"], "rows": [],
        })
        t["rows"].append({**row, "is_new": False, "is_auto": False})
    for row in create_rows:
        t = tracks.setdefault(row["track_id"], {
            "artist": row["artist"], "title": row["title"], "rows": [],
        })
        t["rows"].append({**row, "is_new": True, "is_auto": False})
    return tracks


def build_ui() -> None:
    state = {"plan": load_plan(PLAN_FILE)}
    progress = {"current": 0, "total": 0, "artist": "", "title": "", "result": None, "rendered": -1, "stopping": False}
    stop_event = threading.Event()
    weights = scoring.load_weights(plan_module.WEIGHTS_PATH)  # just for review_section()'s "cleared the N% bar" badge text

    ui.label("Track Record — Mood/Theme Review").classes("text-xl font-bold")

    scan_mode_hints = {
        "all": "Leave count blank to scan the whole library.",
        "recent": f"Scans the N most recently added tracks (blank = {plan_module.DEFAULT_RECENT_COUNT}).",
        "incoming": "Scans everything in your Incoming bin; set a count to cap it.",
    }

    with ui.row().classes("items-center gap-2"):
        scan_mode_select = ui.select(
            {"all": "Whole library", "recent": "Most recently added", "incoming": "Incoming"},
            value="all",
            label="scan",
        ).classes("w-48")
        limit_input = ui.number(label="count (optional)", min=1).classes("w-40")
        generate_button = ui.button("Generate Plan")
        stop_button = ui.button("Stop", icon="stop", color="negative")
        stop_button.visible = False

    scan_mode_hint = ui.label(scan_mode_hints["all"]).classes("text-xs text-gray-500")
    scan_mode_select.on_value_change(lambda e: scan_mode_hint.set_text(scan_mode_hints[e.value]))

    progress_label = ui.label("").classes("text-gray-500")
    progress_bar = ui.linear_progress(value=0, show_value=False).classes("w-64")
    progress_bar.visible = False
    preview_container = ui.column().classes("gap-0")

    _KIND_CLASSES = {
        "AUTO": "text-green-600",
        "REVIEW": "text-gray-400",
        "REVIEW (low confidence)": "text-orange-400",
        "CREATE": "text-blue-400",
    }

    def render_preview(result: dict) -> None:
        preview_container.clear()
        with preview_container:
            for row in result["auto"]:
                kind = "AUTO"
                ui.label(f"{kind}  {row['tag']}  ({row['confidence']:.0%})").classes(f"text-sm {_KIND_CLASSES[kind]}")
            for row in result["review"]:
                kind = "REVIEW (low confidence)" if row.get("low_confidence") else "REVIEW"
                ui.label(f"{kind}  {row['tag']}  ({row['confidence']:.0%})").classes(f"text-sm {_KIND_CLASSES[kind]}")
            for row in result["create"]:
                kind = "CREATE"
                ui.label(f"{kind}  {row['tag']}  ({row['confidence']:.0%}) - new tag").classes(f"text-sm {_KIND_CLASSES[kind]}")

    def update_progress() -> None:
        if not progress["total"]:
            return
        suffix = "  (stopping after this track...)" if progress["stopping"] else ""
        progress_label.text = f"[{progress['current']}/{progress['total']}] {progress['artist']} — {progress['title']}{suffix}"
        progress_bar.value = progress["current"] / progress["total"]
        if progress["result"] is not None and progress["rendered"] != progress["current"]:
            progress["rendered"] = progress["current"]
            render_preview(progress["result"])

    ui.timer(0.3, update_progress)

    @ui.refreshable
    def review_section() -> None:
        plan = state["plan"]
        if not plan or not (plan.get("auto") or plan.get("review") or plan.get("create")):
            ui.label("No plan yet - click Generate Plan above.").classes("text-gray-500")
            state["entries"] = []
            return

        categories = lexicon_client.fetch_categories()
        category_options = {c["id"]: c["label"] for c in categories}
        min_conf = weights.get("auto_include", {}).get("min_confidence", 1.0)

        tracks = group_by_track(plan.get("auto", []), plan.get("review", []), plan.get("create", []))
        entries: list[tuple[dict, ui.checkbox, ui.select | None]] = []

        def toggle_all(e) -> None:
            for _, cb, _ in entries:
                cb.value = e.value

        with ui.row().classes("items-center gap-2"):
            ui.checkbox("Select all", value=False, on_change=toggle_all)
            ui.label(
                f"{len(tracks)} track(s) with proposed tags - high-confidence "
                f"ones are pre-checked, review the rest"
            ).classes("text-gray-500")

        ROW_GRID = (
            "display:grid; grid-template-columns: 32px 1fr 190px 96px 44px 28px 40px; "
            "column-gap:10px; align-items:center;"
        )

        for _, info in sorted(tracks.items(), key=lambda kv: ((kv[1]["artist"] or ""), kv[1]["title"] or "")):
            rows = sorted(info["rows"], key=lambda r: -r["confidence"])
            with ui.expansion(
                f"{info['artist']} — {info['title']}", caption=f"{len(rows)} candidate(s)"
            ).classes("w-full border border-gray-200 dark:border-gray-700 rounded-lg mb-2"):
                track_checkboxes: list[ui.checkbox] = []

                def toggle_track(e, cbs=track_checkboxes) -> None:
                    for cb in cbs:
                        cb.value = e.value

                with ui.row().classes(
                    "items-center gap-2 px-2 py-1 mb-1 bg-gray-50 dark:bg-gray-800 rounded"
                ):
                    ui.checkbox("Select all for this track", value=False, on_change=toggle_track).props("dense")

                for row in rows:
                    is_auto = row.get("is_auto", False)
                    with ui.element("div").style(ROW_GRID).classes(
                        "w-full px-2 py-2 border-b border-gray-100 dark:border-gray-800 "
                        "hover:bg-gray-50 dark:hover:bg-gray-800/60"
                        + (" bg-green-50 dark:bg-green-950/30" if is_auto else "")
                    ):
                        cb = ui.checkbox(value=is_auto).props("dense")
                        track_checkboxes.append(cb)

                        label = row["tag"] + ("  · new" if row["is_new"] else "")
                        label_classes = "truncate cursor-pointer"
                        if is_auto:
                            label_classes += " text-green-700 dark:text-green-400"
                        tag_label = ui.label(label).classes(label_classes)
                        tag_label.on("click", lambda e, cb=cb: setattr(cb, "value", not cb.value))
                        tag_label.tooltip("Click to toggle")

                        select = None
                        if row["is_new"]:
                            select = ui.select(
                                category_options,
                                value=row.get("suggested_category_id"),
                                label="category",
                            ).props("dense outlined").classes("w-full")
                        else:
                            ui.element("div")

                        ui.linear_progress(value=row["confidence"], show_value=False).classes("w-full")
                        ui.label(f"{row['confidence']:.0%}").classes("text-sm text-right")

                        if is_auto:
                            ui.icon("check_circle", color="green").props("size=xs").tooltip(
                                f"Cleared the auto-include confidence bar ({min_conf:.0%}+) - pre-checked"
                            )
                        elif row.get("low_confidence"):
                            ui.icon("warning", color="orange").props("size=xs").tooltip("Low confidence")
                        else:
                            ui.element("div")

                        with ui.button(icon="more_vert").props("flat round dense size=sm"):
                            with ui.menu():
                                for src in row["sources"]:
                                    text = f"{src['source']}: {src.get('note') or ''}"
                                    item = ui.menu_item(text)
                                    if src.get("url"):
                                        item.on("click", lambda url=src["url"]: ui.navigate.to(url, new_tab=True))

                        entries.append((row, cb, select))

        state["entries"] = entries

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

            try:
                result = await run.io_bound(apply.apply_decisions, approved_review, approved_create)
            except Exception as e:
                ui.notify(f"Save failed: {e}", type="negative")
                return

            n = len(result["entries"])
            failed = result["failed_creates"]
            msg = f"Applied {n} track(s)"
            if skipped_no_category:
                msg += f" - skipped {skipped_no_category} new-tag row(s) with no category chosen"
            if failed:
                names = ", ".join(f"'{f['tag']}' ({f['error']})" for f in failed)
                msg += f" - failed to create: {names}"
            ui.notify(msg, type="positive" if not failed else "warning")

            applied_pairs = {
                (e["track_id"], tag) for e in result["entries"] for tag in e["tags_added"]
            }
            if applied_pairs:
                for bucket in ("auto", "review", "create"):
                    plan[bucket] = [
                        r for r in plan.get(bucket, [])
                        if (r["track_id"], r["tag"]) not in applied_pairs
                    ]
                review_section.refresh()

        ui.button("Save Decisions", on_click=save).props("color=primary").classes("mt-4")

    review_section()

    async def generate():
        checked = sum(1 for _, cb, _ in state.get("entries", []) if cb.value)
        if checked:
            with ui.dialog() as confirm_dialog, ui.card():
                ui.label(
                    f"{checked} checked tag(s) haven't been saved yet. "
                    f"Generating a new plan will discard them - they won't "
                    f"be written anywhere."
                )
                with ui.row().classes("w-full justify-end gap-2 mt-2"):
                    ui.button("Cancel", on_click=lambda: confirm_dialog.submit("cancel")).props("flat")
                    ui.button(
                        "Discard and generate", color="negative",
                        on_click=lambda: confirm_dialog.submit("continue"),
                    )
            if await confirm_dialog != "continue":
                return

        stop_event.clear()
        generate_button.disable()
        stop_button.visible = True
        stop_button.enable()
        progress_bar.visible = True
        progress.update(current=0, total=0, artist="", title="", result=None, rendered=-1, stopping=False)
        preview_container.clear()
        progress_label.text = "starting..."

        def on_track_planned(i, total, track, result):
            progress.update(
                current=i, total=total,
                artist=track.get("artist") or "", title=track.get("title") or "",
                result=result,
            )

        limit = int(limit_input.value) if limit_input.value else None

        try:
            new_plan = await run.io_bound(
                plan_module.generate_plan,
                limit=limit,
                scan_mode=scan_mode_select.value,
                on_track_planned=on_track_planned,
                should_stop=stop_event.is_set,
            )
        except Exception as e:
            ui.notify(f"Plan generation failed: {e}", type="negative")
            return
        finally:
            progress_bar.visible = False
            stop_button.visible = False
            generate_button.enable()

        state["plan"] = new_plan
        review_section.refresh()

        stopped = new_plan.get("stopped_early", False)
        ui.notify(
            f"{'Stopped early' if stopped else 'Plan ready'}: "
            f"{len(new_plan['auto'])} pre-checked (high confidence), "
            f"{len(new_plan['review'])} need review, "
            f"{len(new_plan['create'])} propose a new tag - nothing written "
            f"yet, review and click Save Decisions",
            type="warning" if stopped else "positive",
        )

    def request_stop():
        stop_event.set()
        progress["stopping"] = True
        stop_button.disable()

    generate_button.on_click(generate)
    stop_button.on_click(request_stop)


def main():
    build_ui()
    ui.run(
        native=True,
        window_size=(900, 700),
        reload=False,
        title="Track Record - Mood/Theme Review",
        port=8081,  # review_ui.py uses NiceGUI's 8080 default - distinct
        # port so both windows can be open at once without a bind conflict.
        reconnect_timeout=30,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
