#!/usr/bin/env python3
"""Track Record review screen - Genre/Subgenre and Mood/Theme.

NiceGUI, run in native mode (pywebview desktop window, not a browser
tab). One "Generate Plan" for both actions - checkboxes choose which
to include (Genre/Subgenre, Mood/Theme, or both), so a DJ doesn't have
to run two separate scans against the same tracks just because the
two live in different config/pipeline files underneath. This replaced
an earlier two-tab layout (before that, two entirely separate GUI
files) - both were one-click-away-from-the-other-thing in a way that
kept surprising whoever was running it; a single combined generation
is the actual shape of the workflow.

The one thing a combined generation raises that the tabs never had to
solve: a track can now have both a pile of genre candidates and a pile
of mood candidates at once, and dumping all of it into one flat
checkbox list per track would read as a wall of unrelated tags. Each
track's expansion is split into a "Genre / Subgenre" sub-group and a
"Mood / Theme" sub-group instead - one place per track (not two
separate stacked sections, which would mean scrolling past the same
track twice), but visually chunked so the two kinds never blur
together. Each sub-group gets its own "select all" too.

One rule still holds regardless: "Generate Plan" never writes
anything, ever - it's always just a preview. Exactly one action writes
to Lexicon: "Save Decisions", and it writes whatever is checked,
splitting checked rows by kind under the hood and calling each
action's own apply_decisions() - genre_apply for genre rows,
mood_apply for mood rows, potentially both in one click - then
reporting both outcomes in one combined notification.

Layout:
- Scan-mode picker (whole library / most recently added / Incoming,
  optionally capped by count) and the Genre/Subgenre source checkboxes
  (MusicBrainz, Discogs, the audio model - only shown when Genre/
  Subgenre itself is checked, since Mood/Theme has only one source and
  nothing to toggle) are shared controls above one "Generate Plan"
  button. A full-library run is slow (rate-limited API calls plus
  local audio inference per track), so "Stop" aborts mid-run - takes
  effect after the current track finishes, not instantly, and keeps
  whatever was already planned. Generating a new plan while the
  current one has checked-but-unsaved rows asks for confirmation
  first, rather than silently discarding them.
- Genre/Subgenre and Mood/Theme run as two sequential phases when both
  are selected (genre first, then mood) - not interleaved per track -
  each with its own live per-track progress. Stopping during the first
  phase skips the second phase entirely rather than starting it after
  a stop was already requested.
- Regenerating with only one of the two checked leaves the other's
  existing plan (if any) untouched in the review list - only the
  action actually re-run gets replaced.
- Per candidate row: checkbox + tag name + confidence bar/percentage.
  A row that already cleared its own action's auto-include confidence
  bar (genre and mood have separate, independently configured
  thresholds - see config/source_weights.yaml and
  config/mood_weights.yaml) starts pre-checked and gets a green check +
  tooltip explaining why (and naturally sorts near the top of its
  sub-group, since rows are ordered by confidence within it) - still
  just a checkbox, uncheck it like any other if you disagree.
- A "create" row (tag doesn't exist yet) also gets a category picker,
  defaulting to that action's own new_tag_category config if resolved,
  but always changeable here. Never pre-checked, regardless of
  confidence - creating a tag is a bigger action than adding an
  existing one.
- Source, per-source note, and links live behind a `⋮` overflow menu.

Each action's own apply_auto() / `python plan.py` (or `mood_plan.py`)
+ `python apply.py` (or `mood_apply.py`) CLI path still applies a
plan's auto bucket immediately with no review step - a deliberately
different, opt-in tool for scripted/headless use (e.g. a cron job),
not what this screen does.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path

from nicegui import run, ui

import apply as genre_apply
import lexicon_client
import mood_apply
import mood_plan
import plan as genre_plan_module
import scoring

GENRE_PLAN_FILE = Path(__file__).resolve().parent / "genre_plan.json"
MOOD_PLAN_FILE = Path(__file__).resolve().parent / "mood_plan.json"

KIND_LABELS = {"genre": "Genre / Subgenre", "mood": "Mood / Theme"}


def load_plan(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def group_by_track(genre_plan: dict | None, mood_plan_: dict | None) -> dict:
    """Merge both actions' plans into one per-track structure, each row
    tagged with `kind` ("genre" or "mood") so the review screen can
    split a track's expansion into type sub-groups instead of one flat
    undifferentiated list. `is_auto` (pre-checked + badge) and `is_new`
    (category picker, never auto) work exactly as they did when each
    action had its own separate group_by_track - only the extra `kind`
    tag and taking two plans instead of one is new here.
    """
    tracks: dict[int, dict] = {}

    def add(plan: dict | None, kind: str) -> None:
        if not plan:
            return
        for row in plan.get("auto", []):
            t = tracks.setdefault(row["track_id"], {"artist": row["artist"], "title": row["title"], "rows": []})
            t["rows"].append({**row, "is_new": False, "is_auto": True, "kind": kind})
        for row in plan.get("review", []):
            t = tracks.setdefault(row["track_id"], {"artist": row["artist"], "title": row["title"], "rows": []})
            t["rows"].append({**row, "is_new": False, "is_auto": False, "kind": kind})
        for row in plan.get("create", []):
            t = tracks.setdefault(row["track_id"], {"artist": row["artist"], "title": row["title"], "rows": []})
            t["rows"].append({**row, "is_new": True, "is_auto": False, "kind": kind})

    add(genre_plan, "genre")
    add(mood_plan_, "mood")
    return tracks


# Inline CSS grid instead of a flex row - every candidate row gets the
# exact same column widths regardless of whether that particular row
# has a category picker or a warning icon, so the checkbox/label/bar/
# percent line up down the whole track instead of drifting based on
# what each row happens to contain. Plain style (not Tailwind classes)
# because the column widths are fixed pixel values, not something
# arbitrary-value utility classes reliably cover.
ROW_GRID = (
    "display:grid; grid-template-columns: 32px 1fr 190px 96px 44px 28px 40px; "
    "column-gap:10px; align-items:center;"
)


def build_ui() -> None:
    state = {
        "genre_plan": load_plan(GENRE_PLAN_FILE),
        "mood_plan": load_plan(MOOD_PLAN_FILE),
    }
    progress = {"current": 0, "total": 0, "phase": "", "artist": "", "title": "", "result": None, "rendered": -1, "stopping": False}
    stop_event = threading.Event()
    genre_weights = scoring.load_weights()  # just for the "cleared the N% bar" badge text, per kind
    mood_weights = scoring.load_weights(mood_plan.WEIGHTS_PATH)

    # Toasts (ui.notify) vanish on their own after a few seconds, with
    # no way back to one you glanced past - the actual counts in a
    # "Plan ready: ..." toast are exactly the kind of thing worth
    # re-reading a minute later. notify() below shows the same toast
    # as before but also appends to this session's history, kept in a
    # popover behind a bell icon (click to open, click away to close -
    # a menu, not a modal dialog, so it never blocks the rest of the
    # screen) rather than a second copy of the same disappearing toast.
    state_notifications: list[dict] = []
    _NOTIFY_ICONS = {
        "positive": ("check_circle", "text-green-600"),
        "warning": ("warning", "text-orange-500"),
        "negative": ("error", "text-red-600"),
    }

    with ui.row().classes("items-center justify-between w-full"):
        ui.label("Track Record").classes("text-xl font-bold")
        with ui.button(icon="notifications").props("flat round dense"):
            notif_badge = ui.badge("0", color="red").props("floating")
            with ui.menu():
                with ui.column().classes("p-3 gap-2 w-[28rem] max-h-96 overflow-y-auto") as notif_list:
                    pass

    def render_notifications() -> None:
        notif_list.clear()
        with notif_list:
            if not state_notifications:
                ui.label("No notifications yet").classes("text-gray-500 text-sm")
            for n in state_notifications:
                icon, color = _NOTIFY_ICONS.get(n["type"], ("info", "text-gray-600"))
                with ui.row().classes("items-start gap-2 w-full"):
                    ui.icon(icon).classes(f"{color} mt-1").props("size=xs")
                    with ui.column().classes("gap-0 flex-grow min-w-0"):
                        ui.label(n["message"]).classes(f"text-sm {color}")
                        ui.label(n["time"].strftime("%-I:%M:%S %p")).classes("text-xs text-gray-400")
        notif_badge.text = str(len(state_notifications))
        notif_badge.visible = bool(state_notifications)

    def notify(message: str, type: str = "positive") -> None:  # noqa: A002 - matches ui.notify's own param name
        ui.notify(message, type=type)
        state_notifications.insert(0, {"message": message, "type": type, "time": datetime.now()})
        del state_notifications[50:]  # cap history so a long session doesn't grow this unbounded
        render_notifications()

    render_notifications()

    scan_mode_hints = {
        "all": "Leave count blank to scan the whole library.",
        "recent": f"Scans the N most recently added tracks (blank = {genre_plan_module.DEFAULT_RECENT_COUNT}).",
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

    with ui.row().classes("items-center gap-4"):
        ui.label("Tag with:").classes("text-sm text-gray-500")
        genre_enabled_checkbox = ui.checkbox("Genre / Subgenre", value=True).props("dense")
        mood_enabled_checkbox = ui.checkbox("Mood / Theme", value=True).props("dense")

    # Which Genre/Subgenre fetch sources actually run - unchecking one
    # skips it entirely for the run, not just down-weights it (that's
    # still source_weights.yaml's job). Useful on its own: audio_model
    # is by far the slowest part of a run (local inference per track),
    # so a metadata-only pass with it off is a much faster way to
    # sanity-check MusicBrainz/Discogs coverage. Only meaningful (and
    # only shown) when Genre/Subgenre itself is checked above - Mood/
    # Theme has just the one source, nothing yet to toggle.
    SOURCE_LABELS = {
        "musicbrainz": "MusicBrainz",
        "discogs": "Discogs",
        "audio_model": "Audio Model (discogs-maest)",
    }
    with ui.row().classes("items-center gap-4") as source_row:
        ui.label("Genre/Subgenre sources:").classes("text-sm text-gray-500")
        source_checkboxes = {
            name: ui.checkbox(label, value=True).props("dense")
            for name, label in SOURCE_LABELS.items()
        }
    source_row.bind_visibility_from(genre_enabled_checkbox, "value")

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
        progress_label.text = (
            f"[{progress['phase']} {progress['current']}/{progress['total']}] "
            f"{progress['artist']} — {progress['title']}{suffix}"
        )
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
        genre_plan = state["genre_plan"]
        mood_plan_ = state["mood_plan"]
        if not genre_plan and not mood_plan_:
            ui.label("No plan yet - click Generate Plan above.").classes("text-gray-500")
            state["entries"] = []
            return

        categories = lexicon_client.fetch_categories()
        category_options = {c["id"]: c["label"] for c in categories}
        min_conf_by_kind = {
            "genre": genre_weights.get("auto_include", {}).get("min_confidence", 1.0),
            "mood": mood_weights.get("auto_include", {}).get("min_confidence", 1.0),
        }

        tracks = group_by_track(genre_plan, mood_plan_)
        entries: list[tuple[dict, ui.checkbox, ui.select | None, str]] = []

        def toggle_all(e) -> None:
            for _, cb, _, _ in entries:
                cb.value = e.value

        with ui.row().classes("items-center gap-2"):
            ui.checkbox("Select all", value=False, on_change=toggle_all)
            ui.label(f"{len(tracks)} track(s) with proposed tags - high-confidence ones are pre-checked, review the rest").classes(
                "text-gray-500"
            )

        def render_rows(rows: list[dict], select_all_label: str) -> None:
            """One kind's sub-group within a track's expansion - its
            own heading, its own "select all", its own confidence-
            sorted rows. Shared by both kinds; only the row data and
            label differ."""
            rows = sorted(rows, key=lambda r: -r["confidence"])
            sub_checkboxes: list[ui.checkbox] = []

            def toggle_sub(e, cbs=sub_checkboxes) -> None:
                for cb in cbs:
                    cb.value = e.value

            ui.label(KIND_LABELS[rows[0]["kind"]]).classes("text-xs font-bold text-gray-500 uppercase mt-2")
            with ui.row().classes("items-center gap-2 px-2 py-1 mb-1 bg-gray-50 dark:bg-gray-800 rounded"):
                ui.checkbox(select_all_label, value=False, on_change=toggle_sub).props("dense")

            for row in rows:
                is_auto = row.get("is_auto", False)
                kind = row["kind"]
                with ui.element("div").style(ROW_GRID).classes(
                    "w-full px-2 py-2 border-b border-gray-100 dark:border-gray-800 "
                    "hover:bg-gray-50 dark:hover:bg-gray-800/60"
                    + (" bg-green-50 dark:bg-green-950/30" if is_auto else "")
                ):
                    # Pre-checked, not just flagged - a row that already
                    # cleared its action's auto-include bar shouldn't
                    # need an extra click on top of the one Save
                    # Decisions click everything else needs. Still just
                    # a checkbox: uncheck it like any other if you
                    # disagree with this one.
                    cb = ui.checkbox(value=is_auto).props("dense")
                    sub_checkboxes.append(cb)

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
                            f"Cleared {KIND_LABELS[kind]}'s auto-include confidence bar "
                            f"({min_conf_by_kind[kind]:.0%}+) - pre-checked"
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

                    entries.append((row, cb, select, kind))

        for _, info in sorted(tracks.items(), key=lambda kv: ((kv[1]["artist"] or ""), kv[1]["title"] or "")):
            genre_rows = [r for r in info["rows"] if r["kind"] == "genre"]
            mood_rows = [r for r in info["rows"] if r["kind"] == "mood"]
            caption_bits = []
            if genre_rows:
                caption_bits.append(f"{len(genre_rows)} genre")
            if mood_rows:
                caption_bits.append(f"{len(mood_rows)} mood")
            with ui.expansion(
                f"{info['artist']} — {info['title']}", caption=" · ".join(caption_bits) + " candidate(s)"
            ).classes("w-full border border-gray-200 dark:border-gray-700 rounded-lg mb-2"):
                if genre_rows:
                    render_rows(genre_rows, "Select all genre tags for this track")
                if mood_rows:
                    render_rows(mood_rows, "Select all mood tags for this track")

        state["entries"] = entries  # so generate() can warn before discarding checked-but-unsaved rows

        async def save():
            approved = {
                "genre": {"review": [], "create": []},
                "mood": {"review": [], "create": []},
            }
            skipped_no_category = 0
            for r, cb, select, kind in entries:
                if not cb.value:
                    continue
                if r["is_new"]:
                    category_id = select.value if select else None
                    if category_id is None:
                        skipped_no_category += 1
                        continue
                    approved[kind]["create"].append({**r, "category_id": category_id})
                else:
                    approved[kind]["review"].append(r)

            if not any(approved[k][b] for k in ("genre", "mood") for b in ("review", "create")):
                msg = "Nothing checked - nothing to save"
                if skipped_no_category:
                    msg = f"{skipped_no_category} new-tag row(s) checked but no category chosen - pick one first"
                notify(msg, type="warning")
                return

            apply_fns = {"genre": genre_apply.apply_decisions, "mood": mood_apply.apply_decisions}
            results: dict[str, dict] = {}
            try:
                for kind in ("genre", "mood"):
                    if approved[kind]["review"] or approved[kind]["create"]:
                        results[kind] = await run.io_bound(
                            apply_fns[kind], approved[kind]["review"], approved[kind]["create"]
                        )
            except Exception as e:
                # apply_decisions() itself only raises for something
                # outside its own per-tag error handling (e.g. Lexicon
                # unreachable) - without this, that exception would just
                # die silently server-side and nothing would ever tell
                # you Save Decisions didn't actually do anything.
                notify(f"Save failed: {e}", type="negative")
                return

            # Count unique tracks, not len(entries) summed across kinds -
            # a track that got both a genre tag and a mood tag in the
            # same save produces one entry in each result, which would
            # double-count it as "2 tracks" rather than the 1 it is.
            touched_tracks = {e["track_id"] for r in results.values() for e in r["entries"]}
            n_tags = sum(len(e["tags_added"]) for r in results.values() for e in r["entries"])
            failed = [f for r in results.values() for f in r["failed_creates"]]
            msg = f"Applied {n_tags} tag(s) across {len(touched_tracks)} track(s)"
            if skipped_no_category:
                msg += f" - skipped {skipped_no_category} new-tag row(s) with no category chosen"
            if failed:
                names = ", ".join(f"'{f['tag']}' ({f['error']})" for f in failed)
                msg += f" - failed to create: {names}"
            notify(msg, type="positive" if not failed else "warning")

            # Drop whatever was actually written from the in-memory
            # plans - otherwise those rows sit there still checked, the
            # review screen keeps showing tags that already made it to
            # Lexicon, and generate()'s "you have unsaved checked rows"
            # guard falsely trips on a plan that was, in fact, just
            # saved. Rows that were checked but skipped (no category) or
            # failed to create are deliberately left in place, still
            # checked - they still need a decision, nothing happened.
            plans = {"genre": state["genre_plan"], "mood": state["mood_plan"]}
            changed = False
            for kind, result in results.items():
                plan = plans[kind]
                if not plan:
                    continue
                applied_pairs = {(e["track_id"], tag) for e in result["entries"] for tag in e["tags_added"]}
                if not applied_pairs:
                    continue
                changed = True
                for bucket in ("auto", "review", "create"):
                    plan[bucket] = [r for r in plan.get(bucket, []) if (r["track_id"], r["tag"]) not in applied_pairs]
            if changed:
                review_section.refresh()

        ui.button("Save Decisions", on_click=save).props("color=primary").classes("mt-4")

    review_section()

    async def generate():
        include_genre = genre_enabled_checkbox.value
        include_mood = mood_enabled_checkbox.value
        if not include_genre and not include_mood:
            notify("Turn on Genre/Subgenre and/or Mood/Theme before generating", type="warning")
            return

        enabled_sources = {name for name, cb in source_checkboxes.items() if cb.value}
        if include_genre and not enabled_sources:
            notify(
                "Turn at least one Genre/Subgenre source back on, or turn off Genre/Subgenre",
                type="warning",
            )
            return

        checked = sum(1 for _, cb, _, _ in state.get("entries", []) if cb.value)
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

            # Confirming "Discard and generate" should actually discard
            # them right away, not just schedule a replacement that
            # only shows up once the new run finishes - otherwise the
            # stale list (checkboxes, expansions, everything) keeps
            # sitting on screen for the whole run, which reads as if
            # nothing was discarded at all. Only clears whichever
            # action(s) are about to be regenerated - matches
            # generate()'s own rule elsewhere that an action left
            # unchecked this run keeps its existing plan untouched.
            if include_genre:
                state["genre_plan"] = None
            if include_mood:
                state["mood_plan"] = None
            review_section.refresh()

        stop_event.clear()
        generate_button.disable()
        stop_button.visible = True
        stop_button.enable()
        progress_bar.visible = True
        progress.update(current=0, total=0, phase="", artist="", title="", result=None, rendered=-1, stopping=False)
        preview_container.clear()
        progress_label.text = "starting..."

        def on_track_planned(i, total, track, result):
            progress.update(
                current=i, total=total,
                artist=track.get("artist") or "", title=track.get("title") or "",
                result=result,
            )

        limit = int(limit_input.value) if limit_input.value else None
        scan_mode = scan_mode_select.value

        new_genre_plan = state.get("genre_plan")
        new_mood_plan = state.get("mood_plan")
        any_failed = False
        stopped = False

        if include_genre:
            progress.update(phase="Genre", current=0, total=0, rendered=-1)
            try:
                new_genre_plan = await run.io_bound(
                    genre_plan_module.generate_plan,
                    limit=limit,
                    scan_mode=scan_mode,
                    enabled_sources=enabled_sources,
                    on_track_planned=on_track_planned,
                    should_stop=stop_event.is_set,
                )
                if new_genre_plan.get("stopped_early"):
                    stopped = True
            except Exception as e:
                notify(f"Genre/Subgenre plan generation failed: {e}", type="negative")
                any_failed = True

        if include_mood:
            if stop_event.is_set():
                # Genre phase (run first) already saw a stop request -
                # the mood phase never starts at all, rather than
                # kicking off a second scan after the DJ already asked
                # to stop.
                stopped = True
            else:
                progress.update(phase="Mood", current=0, total=0, rendered=-1)
                try:
                    new_mood_plan = await run.io_bound(
                        mood_plan.generate_plan,
                        limit=limit,
                        scan_mode=scan_mode,
                        on_track_planned=on_track_planned,
                        should_stop=stop_event.is_set,
                    )
                    if new_mood_plan.get("stopped_early"):
                        stopped = True
                except Exception as e:
                    notify(f"Mood/Theme plan generation failed: {e}", type="negative")
                    any_failed = True

        progress_bar.visible = False
        stop_button.visible = False
        generate_button.enable()

        state["genre_plan"] = new_genre_plan
        state["mood_plan"] = new_mood_plan
        review_section.refresh()
        genre_counts = new_genre_plan if include_genre else None
        mood_counts = new_mood_plan if include_mood else None
        parts = []
        if genre_counts:
            parts.append(
                f"Genre: {len(genre_counts['auto'])} pre-checked, {len(genre_counts['review'])} review, "
                f"{len(genre_counts['create'])} new-tag"
            )
        if mood_counts:
            parts.append(
                f"Mood: {len(mood_counts['auto'])} pre-checked, {len(mood_counts['review'])} review, "
                f"{len(mood_counts['create'])} new-tag"
            )
        summary = " | ".join(parts) if parts else "no plan generated"
        notify(
            f"{'Stopped early' if stopped else 'Plan ready'}: {summary} - nothing written yet, "
            f"review and click Save Decisions",
            type="negative" if any_failed else ("warning" if stopped else "positive"),
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
        window_size=(1000, 750),
        reload=False,
        title="Track Record",
        # discogs-maest/mood-model inference briefly saturates every CPU
        # core, which can delay the UI's own websocket heartbeat long
        # enough that the client decides the connection dropped
        # ("Connection lost / trying to reconnect"). Nothing is actually
        # wrong - the plan generation keeps running in its own thread
        # regardless - so give the heartbeat enough slack (ping_interval/
        # ping_timeout are derived from this, see NiceGUI's nicegui.py)
        # that a single track's worth of CPU load never trips it.
        reconnect_timeout=30,
    )


if __name__ in {"__main__", "__mp_main__"}:
    main()
