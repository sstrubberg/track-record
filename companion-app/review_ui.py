#!/usr/bin/env python3
"""Genre/Subgenre review screen.

NiceGUI, run in native mode (pywebview desktop window, not a browser
tab). The whole workflow lives here now - generate a plan, review it,
save decisions - so nothing needs the terminal except launching this
one file.

One rule holds for the whole screen: "Generate Plan" never writes
anything, ever - it's always just a preview, no toggle needed because
there's no other mode. Exactly one action writes to Lexicon: "Save
Decisions", and it writes whatever is checked, nothing more. There
used to be a second write path (auto-include tags applied via their
own "Apply now" button, gated by a "Dry run" checkbox) - collapsed
into this one, since a DJ has to hold two different mental models
("did I dry-run this? which button do I click for which rows?") for
what's really one action either way.

Layout:
- "Generate Plan" runs the whole load -> fetch -> score pipeline
  in-process via plan.py's generate_plan(), with a live per-track
  progress bar - a full-library run takes several minutes (rate-limited
  API calls + audio inference per track), so this has to show it's
  working, not just freeze. A scan-mode picker chooses which tracks:
  the whole library (optionally capped by count), the N most recently
  added, or everything in Lexicon's Incoming bin. "Stop" aborts a run
  in progress - takes effect after the current track finishes (can't
  safely interrupt mid-network-call or mid-inference), and keeps
  whatever was already planned as a normal, smaller plan. Generating a
  new plan while the current one has checked-but-unsaved rows asks for
  confirmation first, rather than silently discarding them.
- Tracks grouped in expandable sections. Per candidate row: checkbox +
  tag name + confidence bar/percentage. A row that already cleared the
  auto-include confidence bar starts pre-checked and gets a green
  check + tooltip explaining why (and naturally sorts near the top of
  its track, since rows are ordered by confidence) - still just a
  checkbox, uncheck it like any other if you disagree.
- A "create" row (tag doesn't exist yet) also gets a category picker,
  defaulting to new_tag_category from source_weights.yaml if plan.py
  could resolve one, but always changeable here - category choice is a
  review-time decision, not something to predict beforehand. Never
  pre-checked, regardless of confidence - creating a tag is a bigger
  action than adding an existing one.
- Source, per-source note, and links live behind a `⋮` overflow menu.
- "Save Decisions" commits every checked row via apply.apply_decisions().

`apply.py`'s own `apply_auto()` / `python apply.py` CLI path still
applies a plan's auto bucket immediately with no review step - that's
a deliberately different, opt-in tool for scripted/headless use (e.g.
a cron job), not what this screen does.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

from nicegui import run, ui

import apply
import lexicon_client
import plan as plan_module
import scoring

PLAN_FILE = Path(__file__).resolve().parent / "genre_plan.json"


def load_plan(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def group_by_track(auto_rows: list[dict], review_rows: list[dict], create_rows: list[dict]) -> dict:
    """All three plan buckets share one grouped-by-track view now - auto
    rows are just regular rows that start pre-checked (see review_section
    below), not a separate write path. `is_auto` distinguishes them for
    that pre-check + the "why" badge; `is_new` (create rows only, never
    auto - a new tag is always review-gated) drives the category picker.
    """
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
    weights = scoring.load_weights()  # just for review_section()'s "cleared the N% bar" badge text

    ui.label("Track Record — Genre/Subgenre Review").classes("text-xl font-bold")

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
        # Only redraw the preview when a new track's result has actually
        # arrived - not every timer tick - so it reads as "this track's
        # findings", replaced by the next track's, not a flicker.
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

        # Inline CSS grid instead of a flex row - every candidate row gets
        # the exact same column widths regardless of whether that
        # particular row has a category picker or a warning icon, so the
        # checkbox/label/bar/percent line up down the whole track instead
        # of drifting based on what each row happens to contain. Plain
        # style (not Tailwind classes) because the column widths are
        # fixed pixel values, not something arbitrary-value utility
        # classes reliably cover.
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

                # Its own shaded strip so it reads as a control, not a
                # candidate row - same shape (checkbox + label) as the
                # rows below it made it easy to mistake for one before.
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
                        # Pre-checked, not just flagged - a row that already
                        # cleared the auto-include bar shouldn't need an
                        # extra click on top of the one Save Decisions
                        # click everything else needs. Still just a
                        # checkbox: uncheck it like any other if you
                        # disagree with this one.
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
                            ui.element("div")  # empty grid cell - keeps columns aligned

                        ui.linear_progress(value=row["confidence"], show_value=False).classes("w-full")
                        ui.label(f"{row['confidence']:.0%}").classes("text-sm text-right")

                        # Same grid cell serves either badge - a row is
                        # never both auto-cleared and low-confidence.
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

        state["entries"] = entries  # so generate() can warn before discarding checked-but-unsaved rows

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
                # apply_decisions() itself only raises for something
                # outside its own per-tag error handling (e.g. Lexicon
                # unreachable) - without this, that exception would just
                # die silently server-side and nothing would ever tell
                # you Save Decisions didn't actually do anything.
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
        title="Track Record - Genre Review",
        # discogs-maest inference briefly saturates every CPU core, which
        # can delay the UI's own websocket heartbeat long enough that the
        # client decides the connection dropped ("Connection lost /
        # trying to reconnect"). Nothing is actually wrong - the plan
        # generation keeps running in its own thread regardless - so
        # give the heartbeat enough slack (ping_interval/ping_timeout are
        # derived from this, see NiceGUI's nicegui.py) that a single
        # track's worth of CPU load never trips it.
        reconnect_timeout=30,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
