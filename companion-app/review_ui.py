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
to Lexicon: "Apply Tags", and it writes whatever is checked,
splitting checked rows by kind under the hood and calling each
action's own apply_decisions() - genre_apply for genre rows,
mood_apply for mood rows, potentially both in one click - then
reporting both outcomes in one combined notification.

Layout:
- Scan-mode picker (whole library / most recently added / Incoming,
  optionally capped by count) and the Genre/Subgenre source checkboxes
  (Discogs, the audio model - only shown when Genre/Subgenre itself is
  checked, since Mood/Theme has only one source and nothing to toggle)
  are shared controls above one "Generate Plan"
  button. A full-library run is slow (rate-limited API calls plus
  local audio inference per track), so "Stop" aborts mid-run - takes
  effect after the current track finishes, not instantly, and keeps
  whatever was already planned. Generating a new plan while the
  current one has checked-but-unsaved rows asks for confirmation
  first, rather than silently discarding them.
- "Whole library" scans remember where they left off (see
  scan_progress.py) - a capped run picks up after the last track a
  previous run actually finished, rather than the same first N tracks
  every time, so working through a big library in batches makes real
  progress. A caption under the scan controls shows "X of Y tracks
  scanned" per enabled action, with a "Reset" button (confirmed first)
  to deliberately start that action's whole-library scanning over -
  e.g. after a scoring change worth re-covering old ground for.
  "Recent"/"Incoming" have no such position to save or show.
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
  always changeable here, never pre-checked regardless of confidence -
  creating a tag is a bigger action than adding an existing one. For
  Genre/Subgenre, the default tries a per-tag family match first (see
  genre_family_hint.py) before falling back to that action's own
  new_tag_category config; Mood/Theme has no such taxonomy, so it only
  ever uses new_tag_category.
- Source, per-source note, and links live behind a `⋮` overflow menu.
- Different DJ edits of the same song (e.g. "Promiscuous (Intro
  Clean)" / "Promiscuous (Quick Hit Clean)") show up as separate
  tracks - separate audio files, separate track_ids in Lexicon
  (detected via find_sibling_edits(); a remix only groups with other
  DJ edits of that *same* remix, never with the plain version or a
  different remix of the same song - see _remix_identity()). A
  track with a detected sibling gets a "Copy checked genre tags from
  '<sibling title>'" button next to its Genre/Subgenre "select all":
  work through one edit fully, move on to its sibling, and pull
  whatever's already checked over from there in one click - but only
  where this track's own audio/catalog lookup also proposed that exact
  tag as a candidate, never inventing one it didn't earn. A one-time
  copy, not a live link - nothing stays bound afterward, so unchecking
  something on either track later never cascades anywhere. (An earlier
  version auto-synced every check bidirectionally and live; dropped
  after real use turned up two problems with it - no visibility into
  which edits a track was actually linked to beyond a bare count, and
  no way to let one edit genuinely differ without the live link
  fighting you. A later version pushed a "copy to" from the edit being
  worked on - dropped in turn because it meant scrolling back up to an
  earlier track to push forward, rather than working a track and
  reaching sideways to whichever sibling was already done. Pulling
  fixes both.) Mood/Theme has no such button at all: real testing on
  two edits of the same song found genre stayed consistent between
  them while mood-adjacent tags genuinely differed (a spoken intro on
  one edit reading as "Ballad"/"Vocal") - mood is edit-sensitive in a
  way genre isn't.

Each action's own apply_auto() / `python plan.py` (or `mood_plan.py`)
+ `python apply.py` (or `mood_apply.py`) CLI path still applies a
plan's auto bucket immediately with no review step - a deliberately
different, opt-in tool for scripted/headless use (e.g. a cron job),
not what this screen does.
"""

from __future__ import annotations

import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from nicegui import run, ui

import apply as genre_apply
import charts_apply
import charts_plan as charts_plan_module
import config_editor
import lexicon_client
import model_versions
import mood_apply
import mood_plan
import plan as genre_plan_module
import scan_progress
import scoring

GENRE_PLAN_FILE = Path(__file__).resolve().parent / "genre_plan.json"
MOOD_PLAN_FILE = Path(__file__).resolve().parent / "mood_plan.json"
CHARTS_PLAN_FILE = Path(__file__).resolve().parent / "charts_plan.json"

KIND_LABELS = {"genre": "Genre / Subgenre", "mood": "Mood / Theme", "charts": "Charts"}


def load_plan(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def group_by_track(genre_plan: dict | None, mood_plan_: dict | None, charts_plan: dict | None) -> dict:
    """Merge all three actions' plans into one per-track structure, each
    row tagged with `kind` ("genre", "mood", or "charts") so the review
    screen can split a track's expansion into type sub-groups instead
    of one flat undifferentiated list. `is_auto` (pre-checked + badge)
    and `is_new` (category picker, never auto) work exactly as they did
    when each action had its own separate group_by_track - only the
    extra `kind` tag and taking three plans instead of one is new here.
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
    add(charts_plan, "charts")
    return tracks


# Same paren/bracket-stripping regex discogs.py/musicbrainz.py already
# use to strip DJ edit/version suffixes ("(Intro Clean)", "(MM Edit)")
# off a title before searching a public catalog - reused here for a
# different purpose: detecting when two different tracks already in
# this DJ's own library are actually different edits of the same
# underlying song, not two different songs that happen to share a
# title.
_EDIT_SUFFIX_RE = re.compile(r"\s*[\(\[][^)\]]*[)\]]")

# Same ARTIST_SPLIT pattern discogs.py/musicbrainz.py already use to
# isolate a primary artist credit before searching an external catalog
# - reused here because a DJ's own library isn't consistent about it
# either: two edits of the same song can genuinely have the Artist
# field spelled differently ("Deee-Lite" on one edit, "Deee-Lite Ft.
# Q-Tip" on another because that edit's tags happened to be entered
# with the feature credit folded in). Matching on primary artist alone
# catches that; matching on the raw field, as this used to, silently
# missed it - real library data confirmed this exact case.
_ARTIST_SPLIT = re.compile(
    r"\s+(?:&|x|and|with|feat\.?|ft\.?|featuring|vs\.?|f/)\s+"
    r"|,(?!\s*(?:inc|ltd|llc|co|jr|sr)\b)\s*"
    r"|\s+/\s+", re.I,
)


def _primary_artist(artist: str) -> str:
    primary = _ARTIST_SPLIT.split(artist)[0].strip()
    return primary or artist


# A remix shares its title with the song it remixes but not its sound -
# it's a different producer's reinterpretation, often landing in a
# different genre entirely (a house remix of a rock song is still,
# genre-wise, a house record). So a remix and the plain edit of the
# same song shouldn't find each other as siblings, and neither should
# two *different* remixes of it - but a DJ can absolutely have two
# separate DJ edits of the *same* remix (e.g. an intro edit and a
# quick-hit edit both built from the Gigamesh Remix), and those two
# genuinely are siblings of each other, same as any other pair of
# edits. So "which remix, if any" needs to be part of what makes two
# tracks a match, not a reason to disqualify a track from matching
# altogether. Captures the parenthetical/bracketed group containing
# "remix" itself (e.g. "Gigamesh Remix" out of "(Gigamesh Remix)") -
# None for a track that isn't a remix at all.
_REMIX_NAME_RE = re.compile(r"[\(\[]([^)\]]*\bremix\b[^)\]]*)[)\]]", re.I)


def _remix_identity(title: str | None) -> str | None:
    match = _REMIX_NAME_RE.search(title or "")
    return match.group(1).strip().lower() if match else None


def _song_key(artist: str | None, title: str | None) -> tuple[str, str, str | None]:
    """Case-insensitive (primary artist, edit-suffix-stripped title,
    remix identity) - the grouping key find_sibling_edits() below uses
    to treat "Promiscuous (Intro Clean)" and "Promiscuous (Quick Hit
    Clean)" as edits of one song, while keeping a remix's own edits
    grouped only with each other (see _remix_identity)."""
    stripped_title = _EDIT_SUFFIX_RE.sub("", title or "").strip().lower()
    return (
        _primary_artist((artist or "").strip()).lower(),
        stripped_title or (title or "").strip().lower(),  # don't key on "" if stripping ate the whole title
        _remix_identity(title),
    )


def find_sibling_edits(tracks: dict) -> dict[int, list[int]]:
    """Return {track_id: [other track_id, ...]} for every track that
    shares its (primary artist, edit-suffix-stripped title, remix
    identity) key with at least one other track currently in the plan -
    i.e. tracks this DJ's own library holds as separate DJ edits of the
    same underlying song (and, if it's a remix, the same remix - a
    remix never groups with the plain version or a different remix of
    the same song). Tracks with no such sibling don't appear in the
    returned dict at all, so `track_id in find_sibling_edits(tracks)`
    doubles as an is-this-track-part-of-a-group check.
    """
    groups: dict[tuple[str, str, str | None], list[int]] = {}
    for track_id, info in tracks.items():
        groups.setdefault(_song_key(info["artist"], info["title"]), []).append(track_id)
    return {
        track_id: [t for t in group if t != track_id]
        for group in groups.values() if len(group) > 1
        for track_id in group
    }


def _format_duration(seconds: float) -> str:
    """Renders as "2m 15s", "45s", or "1h 05m" - seconds precision
    under a minute, minutes precision above it (a countdown ticking
    "1h 5m 23s, 1h 5m 22s, ..." reads as more precise than the
    underlying estimate actually is)."""
    seconds = max(0, round(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {seconds:02d}s"
    return f"{seconds}s"


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


PAGE_SIZE_OPTIONS = [25, 50, 100, 200]
DEFAULT_PAGE_SIZE = 50


def build_ui() -> None:
    state = {
        "genre_plan": load_plan(GENRE_PLAN_FILE),
        "mood_plan": load_plan(MOOD_PLAN_FILE),
        "charts_plan": load_plan(CHARTS_PLAN_FILE),
        # Checked/category-choice state used to live entirely in the
        # live ui.checkbox/ui.select objects themselves, read directly
        # at save time - fine when every row was always on screen, but
        # pagination (below) only keeps the current page's rows
        # rendered at all. These two dicts, keyed by (track_id, kind,
        # tag), are the actual source of truth now; checkboxes/selects
        # just read their initial value from here and write back on
        # change, so a check made on page 1 survives navigating to
        # page 4 and back, even though the page-1 checkbox itself gets
        # torn down and rebuilt in between.
        "checked": {},
        "category_choice": {},
        "page": 1,
        "page_size": DEFAULT_PAGE_SIZE,
        # Which tracks' expansions are open - ui.expansion's own open/
        # closed state doesn't survive review_section.refresh() (a full
        # rebuild recreates every expansion fresh, defaulting closed),
        # and several actions already call refresh() unconditionally
        # (Select all, per-track "select all", sibling-edit genre sync
        # below) - without this, any of those would silently collapse
        # every track you had open. Same persistent-dict-survives-a-
        # rebuild pattern as "checked" above.
        "expanded_tracks": set(),
        # Cached whole-library track list, used only to turn a saved
        # scan_progress cursor (a track id) into an "X of Y tracks
        # scanned" count for the caption below - None until first
        # needed, then kept for the rest of the session rather than
        # re-fetched on every render. Cleared after a whole-library
        # scan actually runs, since that's the one time the count or
        # the cursor could plausibly have changed.
        "library_snapshot": None,
        # Last "Check for Updates" result in the Settings dialog's Audio
        # models card, keyed by model_versions.ModelFile.label - None
        # until checked at least once this session (nothing checks the
        # network just from opening Settings). Cleared for a specific
        # model right after it's updated, so its row falls back to the
        # plain "vN, from what's on disk" reading rather than showing a
        # stale "update available" for a version that's already applied.
        "model_check_results": {},
    }
    progress = {
        "current": 0, "total": 0, "phase": "", "artist": "", "title": "", "result": None,
        "rendered": -1, "stopping": False,
        # time.monotonic() timestamp of when the *current phase*
        # started - not wall-clock time, since a system clock change
        # mid-run shouldn't skew the estimate. Used by update_progress()
        # below for a live countdown; None means "no phase running, or
        # too early in one to estimate yet."
        "phase_started_at": None,
    }
    stop_event = threading.Event()
    genre_weights = scoring.load_weights()  # just for the "cleared the N% bar" badge text, per kind
    mood_weights = scoring.load_weights(mood_plan.WEIGHTS_PATH)
    charts_weights = scoring.load_weights(charts_plan_module.WEIGHTS_PATH)

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
        with ui.row().classes("items-center gap-0"):
            # Settings is per-DJ config tuning (a handful of numbers,
            # touched rarely), not the main workflow - a gear-icon
            # dialog next to this same header fits it better than a
            # separate full-screen view with its own back button.
            # Click handler wired up further down (open_settings_dialog
            # is defined after review_section exists, so the button is
            # created here for header position but wired up later).
            settings_button = ui.button(icon="settings").props("flat round dense")
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
        "track": "Search by artist/title - each DJ edit of a song is its own separate track.",
    }

    # {track_id: "Artist - Title"} for the "Single track" picker below -
    # built once, synchronously, right here at startup rather than lazily
    # on first use. Tried lazy first (only fetch if "Single track" ever
    # actually gets picked) via a later set_options() call, but that ran
    # into a real NiceGUI/Quasar limitation confirmed directly with a
    # minimal reproduction outside this app entirely: a with_input=True
    # ui.select's dropdown never picks up options set after the element
    # is already mounted, even though the server-side model updates
    # correctly - only options given at construction time ever reach the
    # client. Fetching eagerly sidesteps it, and the actual cost is
    # trivial (confirmed: ~0.3s for a 1,770-track library) - Lexicon
    # already has to be running for anything in this app to work at all,
    # so this isn't adding a new dependency, just moving an already-
    # required cheap read earlier. Edit/version info (e.g. "(MM Edit)",
    # "(Intro Clean)") already lives in Lexicon's own title field, so
    # distinct DJ edits of the same song already read as distinct
    # options with no extra handling needed here.
    #
    # Wrapped: this is the one place in build_ui() that now touches the
    # network before any button is ever clicked - everywhere else, a
    # Lexicon connection failure surfaces on the specific action that
    # needed it (e.g. "Generate Plan" failing with a clear error),
    # never by refusing to even open the window. Caught directly:
    # launching with Lexicon not yet running crashed build_ui() itself
    # before this try/except existed - a real regression, not a
    # hypothetical one. track_options_error, if set, is surfaced right
    # on the picker instead (see its use below) rather than only in a
    # log a DJ would never see from the native window.
    track_options: dict[int, str] = {}
    track_options_error: str | None = None
    try:
        track_options = {
            t["id"]: f"{t.get('artist') or '(no artist)'} - {t.get('title') or '(no title)'}"
            for t in sorted(lexicon_client.fetch_library(), key=lambda t: (t.get("artist") or "", t.get("title") or ""))
        }
    except Exception as e:
        track_options_error = str(e)

    with ui.row().classes("items-center gap-2"):
        scan_mode_select = ui.select(
            {"all": "Whole library", "recent": "Most recently added", "incoming": "Incoming", "track": "Single track"},
            value="all",
            label="scan",
        ).classes("w-48")
        limit_input = ui.number(label="count (optional)", min=1).classes("w-40")
        track_picker = ui.select(
            track_options, label="track", with_input=True,
        ).props("dense outlined").classes("w-96")
        track_picker.visible = False
        generate_button = ui.button("Generate Plan")
        stop_button = ui.button("Stop", icon="stop", color="negative")
        stop_button.visible = False

    scan_mode_hint = ui.label(scan_mode_hints["all"]).classes("text-xs text-gray-500")
    if track_options_error:
        track_picker_error = ui.label(
            f"Couldn't load your track list at startup ({track_options_error}) - "
            f"make sure Lexicon is open, then restart Track Record to use this."
        ).classes("text-xs text-red-600")
        track_picker_error.visible = False

    def on_scan_mode_change(e) -> None:
        scan_mode_hint.set_text(scan_mode_hints[e.value])
        limit_input.visible = e.value != "track"
        track_picker.visible = e.value == "track" and not track_options_error
        if track_options_error:
            track_picker_error.visible = e.value == "track"
        scan_progress_caption.refresh()

    scan_mode_select.on_value_change(on_scan_mode_change)

    with ui.row().classes("items-center gap-4"):
        ui.label("Tag with:").classes("text-sm text-gray-500")
        genre_enabled_checkbox = ui.checkbox("Genre / Subgenre", value=True).props("dense")
        mood_enabled_checkbox = ui.checkbox("Mood / Theme", value=True).props("dense")
        # No per-source checkbox row for Charts, same as Mood/Theme -
        # just the one source (billboard-tag's own chart-cache match),
        # nothing yet to toggle between.
        charts_enabled_checkbox = ui.checkbox("Charts", value=True).props("dense")
        genre_enabled_checkbox.on_value_change(lambda: scan_progress_caption.refresh())
        mood_enabled_checkbox.on_value_change(lambda: scan_progress_caption.refresh())
        charts_enabled_checkbox.on_value_change(lambda: scan_progress_caption.refresh())

    # Which Genre/Subgenre fetch sources actually run - unchecking one
    # skips it entirely for the run, not just down-weights it (that's
    # still source_weights.yaml's job). Useful on its own: the two audio
    # models are by far the slowest part of a run (local inference per
    # track), so a metadata-only pass with both off is a much faster way
    # to sanity-check Discogs coverage. Only meaningful (and only shown)
    # when Genre/Subgenre itself is checked above - Mood/Theme has just
    # the one source, nothing yet to toggle. MusicBrainz was a source
    # here through 2026-08-07 - dropped (see plan.py) after consistently
    # disappointing suggestions; audio_model_genre_effnet took its slot,
    # a second, architecturally-independent audio model rather than a
    # second web lookup.
    SOURCE_LABELS = {
        "discogs": "Discogs",
        "audio_model": "Audio Model (discogs-maest)",
        "audio_model_genre_effnet": "Audio Model (genre_discogs400)",
    }
    with ui.row().classes("items-center gap-4") as source_row:
        ui.label("Genre/Subgenre sources:").classes("text-sm text-gray-500")
        source_checkboxes = {
            name: ui.checkbox(label, value=True).props("dense")
            for name, label in SOURCE_LABELS.items()
        }
    source_row.bind_visibility_from(genre_enabled_checkbox, "value")

    def get_library_snapshot() -> list[dict] | None:
        """None (rather than raising) if Lexicon isn't reachable right
        now - caught directly: this used to call fetch_library()
        unwrapped, and since scan_progress_caption() runs it on every
        initial render (default scan mode is "all"), launching the app
        before Lexicon was open crashed build_ui() itself, not just
        whatever action actually needed the connection. Cached either
        way, success or failure, so a still-unreachable Lexicon doesn't
        retry (and re-fail) on every single refresh."""
        if state["library_snapshot"] is None and not state.get("library_snapshot_error"):
            try:
                state["library_snapshot"] = lexicon_client.fetch_library()
            except Exception as e:
                state["library_snapshot_error"] = str(e)
        return state["library_snapshot"]

    async def reset_scan_progress(action: str, label: str) -> None:
        with ui.dialog() as confirm_dialog, ui.card():
            ui.label(
                f"Reset scan progress for {label}? The next whole-library scan "
                f"will start over from the beginning instead of continuing where "
                f"it left off. Tags already applied are untouched - nothing gets "
                f"un-tagged."
            )
            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                ui.button("Cancel", on_click=lambda: confirm_dialog.submit("cancel")).props("flat")
                ui.button(
                    "Reset", color="negative",
                    on_click=lambda: confirm_dialog.submit("continue"),
                )
        if await confirm_dialog != "continue":
            return
        scan_progress.reset(action)
        notify(f"Scan progress reset for {label} - the next whole-library scan starts from the beginning")
        scan_progress_caption.refresh()

    # Only meaningful for "Whole library" - "Most recently added" is
    # already a moving window and "Incoming" self-narrows as tracks
    # leave that bin, so neither has an analogous saved position (see
    # scan_progress.py). Shown per enabled action, since Genre/Subgenre
    # and Mood/Theme scans track their own separate positions and a DJ
    # might only be running one of them right now. Charts deliberately
    # has no entry here at all - scan_progress.py's ACTIONS only ever
    # covers "genre"/"mood" (see charts_plan.py's own docstring on why
    # a saved position isn't worth it for an in-memory chart-cache
    # lookup), so there's no cursor to show even when Charts is on.
    @ui.refreshable
    def scan_progress_caption() -> None:
        if scan_mode_select.value != "all":
            return
        actions = [
            ("genre", "Genre/Subgenre", genre_enabled_checkbox.value),
            ("mood", "Mood/Theme", mood_enabled_checkbox.value),
        ]
        if not any(enabled for _, _, enabled in actions):
            return
        snapshot = get_library_snapshot()
        if snapshot is None:
            ui.label(
                f"Couldn't reach Lexicon to check scan progress ({state['library_snapshot_error']})"
            ).classes("text-xs text-red-600")
            return
        total = len(snapshot)
        for action, label, enabled in actions:
            if not enabled:
                continue
            cursor = scan_progress.get_cursor(action)
            scanned = sum(1 for t in snapshot if t["id"] <= cursor) if cursor is not None else 0
            caught_up = " (fully caught up)" if total and scanned >= total else ""
            with ui.row().classes("items-center gap-2"):
                ui.label(f"{label}: {scanned:,} of {total:,} tracks scanned{caught_up}").classes(
                    "text-xs text-gray-500"
                )
                if cursor is not None:
                    ui.button(
                        "Reset", on_click=lambda a=action, l=label: reset_scan_progress(a, l)
                    ).props("flat dense size=sm color=grey")

    scan_progress_caption()

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
        eta = ""
        # Needs at least one finished track in this phase to have any
        # real per-track timing to extrapolate from - before that,
        # showing a guess (e.g. off the very first track's cost alone,
        # which often eats extra one-time overhead like a model's first
        # load) would read as confident when it isn't. Re-anchored off
        # the phase's own start time and recomputed every tick (this
        # runs on the same 0.3s ui.timer as everything else here), not
        # just when a new track finishes, so it actually ticks down
        # smoothly like a countdown rather than jumping once per track.
        if progress["current"] > 0 and progress["phase_started_at"] is not None:
            elapsed = time.monotonic() - progress["phase_started_at"]
            avg_per_track = elapsed / progress["current"]
            remaining = avg_per_track * progress["total"] - elapsed
            if remaining > 0:
                eta = f"  · ~{_format_duration(remaining)} left"
        progress_label.text = (
            f"[{progress['phase']} {progress['current']}/{progress['total']}] "
            f"{progress['artist']} — {progress['title']}{suffix}{eta}"
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
        charts_plan = state["charts_plan"]
        if not genre_plan and not mood_plan_ and not charts_plan:
            ui.label("No plan yet - click Generate Plan above.").classes("text-gray-500")
            return

        categories = lexicon_client.fetch_categories()
        category_options = {c["id"]: c["label"] for c in categories}
        min_conf_by_kind = {
            "genre": genre_weights.get("auto_include", {}).get("min_confidence", 1.0),
            "mood": mood_weights.get("auto_include", {}).get("min_confidence", 1.0),
            "charts": charts_weights.get("auto_include", {}).get("min_confidence", 1.0),
        }

        tracks = group_by_track(genre_plan, mood_plan_, charts_plan)

        # {track_id: reason} for every track Charts scanned but had
        # nothing new to propose for - see charts_plan.py's own
        # plan_track() for what populates this and why. Only ever
        # rendered for a track that's already showing for some other
        # reason (another action's real candidates) - a track with
        # nothing from any action still doesn't show up at all, same
        # as before Charts existed.
        charts_no_match = {r["track_id"]: r["reason"] for r in (charts_plan or {}).get("no_match", [])}

        # Ensure every row currently in the plan has a checked-default
        # (pre-checked for auto rows, unchecked otherwise) regardless of
        # which page it'll land on - a row on page 4 that the DJ never
        # scrolls to still needs to count as pre-checked for Save
        # Decisions, the same as it would if pagination didn't exist.
        # setdefault only writes a value the first time a key is seen,
        # so an already-answered checkbox (checked, unchecked, or a
        # category picked) is never reset back to its default just
        # because the page holding it got rebuilt. Also prunes both
        # dicts down to keys that still exist - a full regenerate or a
        # completed save both remove rows, and their leftover checked/
        # category state should go with them rather than linger forever.
        valid_keys = set()
        for track_id, info in tracks.items():
            for row in info["rows"]:
                key = (track_id, row["kind"], row["tag"])
                valid_keys.add(key)
                state["checked"].setdefault(key, row.get("is_auto", False))
        for d in (state["checked"], state["category_choice"]):
            for key in list(d):
                if key not in valid_keys:
                    del d[key]
        state["expanded_tracks"] &= tracks.keys()

        # DJ edits of the same song (e.g. "Promiscuous (Intro Clean)" /
        # "Promiscuous (Quick Hit Clean)") - see find_sibling_edits()'s
        # own docstring. Genre/Subgenre only, never Mood/Theme: real
        # testing on two edits of the same song found genre stayed
        # essentially consistent between them, while mood-adjacent tags
        # (a spoken intro reading as "Ballad"/"Vocal") genuinely
        # differed - mood is edit-sensitive in a way genre isn't.
        #
        # Deliberately NOT a live binding - an earlier version of this
        # auto-mirrored every check/uncheck bidirectionally between
        # siblings, which turned out to have two real problems in
        # practice: no visibility into which edits a track was actually
        # linked to beyond a bare count, and no way to let one edit
        # genuinely differ from its sibling without the live link
        # fighting you. render_rows() below instead shows each
        # sibling's real title and offers a one-time "Copy checked
        # genre tags from..." action - pulls whatever's already checked
        # on the named sibling into this track once, same "only where
        # this track's own lookup also proposed it as a candidate" rule
        # as before, but nothing stays linked afterward. Unchecking
        # something later never cascades anywhere. Pulling rather than
        # pushing matches how a DJ actually works the list: finish one
        # edit, move to the next one (its sibling, right below it once
        # sorted), and grab what's already settled instead of scrolling
        # back up to the edit worked a minute ago.
        siblings_by_track = find_sibling_edits(tracks)

        def toggle_all(e) -> None:
            for track_id, info in tracks.items():
                for row in info["rows"]:
                    state["checked"][(track_id, row["kind"], row["tag"])] = e.value
            review_section.refresh()

        # value=False used to be a hard-coded literal here, not derived
        # from state - harmless before toggle_all() called refresh()
        # unconditionally (nothing ever rebuilt this checkbox out from
        # under itself), but once it did, every click rebuilt this exact
        # checkbox back to its literal False, snapping it right back to
        # unchecked even though the click had, in fact, just checked
        # every row (confirmed directly: the underlying state["checked"]
        # writes always worked correctly - only this checkbox's own
        # displayed value was wrong). Computed fresh each render instead.
        all_checked = bool(valid_keys) and all(state["checked"].get(key) for key in valid_keys)
        with ui.row().classes("items-center gap-2"):
            # Bare "Select all" didn't say what it was all of - genre,
            # mood, one track, every track, the visible page? It's
            # actually every genre and mood candidate row across the
            # whole plan (see toggle_all() above), independent of
            # pagination - matching that in the label instead of
            # leaving it to be inferred.
            ui.checkbox("Select all tags (every track)", value=all_checked, on_change=toggle_all)
            ui.label(f"{len(tracks)} track(s) with proposed tags - high-confidence ones are pre-checked, review the rest").classes(
                "text-gray-500"
            )

        # Pagination - at real-library scale (thousands of tracks) every
        # track's expansion showing up at once, even collapsed, was
        # measured at 500K+ DOM nodes and 20+ seconds just to build the
        # element tree for 2,000 synthetic tracks before a page-size
        # limit existed. Slicing to one page's worth of track shells is
        # the actual fix for that; row content is also deferred until a
        # track is opened (see build_track_content below) so even a full
        # page of collapsed tracks stays cheap.
        sorted_tracks = sorted(tracks.items(), key=lambda kv: ((kv[1]["artist"] or ""), kv[1]["title"] or ""))
        page_size = state["page_size"]
        total_pages = max(1, -(-len(sorted_tracks) // page_size))  # ceil div
        state["page"] = max(1, min(state["page"], total_pages))
        start = (state["page"] - 1) * page_size
        page_tracks = sorted_tracks[start:start + page_size]

        with ui.row().classes("items-center gap-3"):
            def on_page_size_change(e) -> None:
                state["page_size"] = e.value
                state["page"] = 1
                review_section.refresh()

            ui.select(PAGE_SIZE_OPTIONS, value=page_size, label="tracks per page").props("dense").classes(
                "w-32"
            ).on_value_change(on_page_size_change)

            if total_pages > 1:
                def on_page_change(e) -> None:
                    state["page"] = e.value
                    review_section.refresh()

                ui.pagination(1, total_pages, value=state["page"], direction_links=True, on_change=on_page_change)
                ui.label(f"tracks {start + 1}-{min(start + page_size, len(sorted_tracks))} of {len(sorted_tracks)}").classes(
                    "text-xs text-gray-500"
                )

        def render_rows(rows: list[dict], select_all_label: str) -> None:
            """One kind's sub-group within a track's expansion - its
            own heading, its own "select all", its own confidence-
            sorted rows. Shared by both kinds; only the row data and
            label differ."""
            rows = sorted(rows, key=lambda r: -r["confidence"])
            kind = rows[0]["kind"]
            track_id = rows[0]["track_id"]

            def toggle_sub(e, rows=rows, kind=kind) -> None:
                for row in rows:
                    state["checked"][(row["track_id"], kind, row["tag"])] = e.value
                review_section.refresh()

            ui.label(KIND_LABELS[kind]).classes("text-xs font-bold text-gray-500 uppercase mt-2")
            with ui.row().classes("items-center gap-2 px-2 py-1 mb-1 bg-gray-50 dark:bg-gray-800 rounded"):
                ui.checkbox(select_all_label, value=False, on_change=toggle_sub).props("dense")

                sibling_ids = siblings_by_track.get(track_id, ()) if kind == "genre" else ()
                # One button per sibling, each pulling from that one
                # sibling specifically - not a single button pulling
                # from all of them merged together, so which edit a
                # given check actually came from stays visible (the
                # button names it) rather than blurring into "some
                # sibling, somewhere." Almost always exactly one sibling
                # in practice; this only matters when a song has three+
                # DJ edits in the library at once.
                for sibling_id in sibling_ids:
                    sibling_title = tracks[sibling_id]["title"]
                    sibling_has_checked = any(
                        state["checked"].get((sibling_id, "genre", row["tag"]))
                        for row in tracks[sibling_id]["rows"] if row["kind"] == "genre"
                    )

                    def copy_from_sibling(sibling_id=sibling_id, sibling_title=sibling_title, track_id=track_id) -> None:
                        donor_checked_tags = [
                            row["tag"] for row in tracks[sibling_id]["rows"]
                            if row["kind"] == "genre" and state["checked"].get((sibling_id, "genre", row["tag"]))
                        ]
                        if not donor_checked_tags:
                            notify(
                                f"\"{sibling_title}\" has no genre tags checked yet - check some there first",
                                type="warning",
                            )
                            return
                        copied = 0
                        for tag in donor_checked_tags:
                            key = (track_id, "genre", tag)
                            # Only where this track's own audio/catalog
                            # lookup already proposed this exact tag as a
                            # candidate - never invents one it didn't
                            # earn. A one-time copy, not a live link:
                            # nothing here keeps watching for future
                            # changes on either track.
                            if key in valid_keys and not state["checked"].get(key):
                                state["checked"][key] = True
                                copied += 1
                        if copied:
                            notify(f"Copied {copied} genre tag(s) from \"{sibling_title}\"", type="positive")
                        else:
                            notify(
                                f"Nothing new to copy from \"{sibling_title}\" - this track either already "
                                "has those tags checked or never proposed them as candidates",
                                type="warning",
                            )
                        review_section.refresh()

                    copy_btn = ui.button(
                        f"Copy checked genre tags from \"{sibling_title}\"", on_click=copy_from_sibling
                    ).props("outline dense size=sm")
                    if not sibling_has_checked:
                        copy_btn.tooltip(f"\"{sibling_title}\" has no genre tags checked yet")

            for row in rows:
                is_auto = row.get("is_auto", False)
                key = (row["track_id"], kind, row["tag"])
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
                    # disagree with this one. Reads its initial value
                    # from state["checked"] (already defaulted above)
                    # and writes back on every change, rather than being
                    # the source of truth itself - the checkbox object
                    # doesn't survive a page turn, the dict does.
                    cb = ui.checkbox(value=state["checked"][key]).props("dense")
                    cb.on_value_change(lambda e, key=key: state["checked"].__setitem__(key, e.value))

                    label = row["tag"] + ("  · new" if row["is_new"] else "")
                    label_classes = "truncate cursor-pointer"
                    if is_auto:
                        label_classes += " text-green-700 dark:text-green-400"
                    tag_label = ui.label(label).classes(label_classes)
                    tag_label.on("click", lambda e, cb=cb: setattr(cb, "value", not cb.value))
                    tag_label.tooltip("Click to toggle")

                    if row["is_new"]:
                        default_category = state["category_choice"].get(key, row.get("suggested_category_id"))
                        select = ui.select(
                            category_options,
                            value=default_category,
                            label="category",
                        ).props("dense outlined").classes("w-full")
                        select.on_value_change(lambda e, key=key: state["category_choice"].__setitem__(key, e.value))
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

        for track_id, info in page_tracks:
            genre_rows = [r for r in info["rows"] if r["kind"] == "genre"]
            mood_rows = [r for r in info["rows"] if r["kind"] == "mood"]
            charts_rows = [r for r in info["rows"] if r["kind"] == "charts"]
            caption_bits = []
            if genre_rows:
                caption_bits.append(f"{len(genre_rows)} genre")
            if mood_rows:
                caption_bits.append(f"{len(mood_rows)} mood")
            if charts_rows:
                caption_bits.append(f"{len(charts_rows)} charts")
            caption = " · ".join(caption_bits) + " candidate(s)"

            if not charts_rows and track_id in charts_no_match:
                # Charts ran (this track just wasn't sitting on genre/
                # mood candidates alone) and genuinely found nothing new -
                # say why, rather than a DJ wondering whether Charts is
                # even working. Not folded into caption_bits above, same
                # reasoning as the sibling-detection note below - it
                # isn't another "N candidate(s)" clause.
                caption += f" — Charts: {charts_no_match[track_id]}"

            n_siblings = len(siblings_by_track.get(track_id, ()))
            if n_siblings:
                # No live sync to announce anymore (see render_rows'
                # "Copy checked genre tags from..." button) - just naming
                # that sibling edit(s) exist and are detected, so it's
                # obvious why that button showed up rather than a DJ
                # wondering where it came from. Appended after
                # " candidate(s)", not folded into caption_bits above,
                # so it doesn't read as another "N candidate(s)" clause.
                caption += f" — {n_siblings} sibling edit{'s' if n_siblings != 1 else ''} detected"

            # Lazy render: a collapsed track only ever costs an
            # expansion shell + one empty container (a couple of DOM
            # nodes) - its actual candidate rows (the expensive part,
            # ~7 elements each) are only built the first time it's
            # opened, not for every track on the page whether you look
            # at it or not. `built` guards against rebuilding on every
            # subsequent open/close of the same track.
            built = {"done": False}

            def populate(container, genre_rows=genre_rows, mood_rows=mood_rows, charts_rows=charts_rows, built=built) -> None:
                if built["done"]:
                    return
                built["done"] = True
                with container:
                    if genre_rows:
                        render_rows(genre_rows, "Select all genre tags for this track")
                    if mood_rows:
                        render_rows(mood_rows, "Select all mood tags for this track")
                    if charts_rows:
                        render_rows(charts_rows, "Select all chart tags for this track")

            # Starts open if it was open before whatever triggered this
            # render (see state["expanded_tracks"]'s own comment) -
            # populate it immediately in that case, since there's no
            # fresh "just opened it" on_value_change event to do that
            # for us when the expansion is created already-open.
            was_expanded = track_id in state["expanded_tracks"]
            expansion = ui.expansion(
                f"{info['artist']} — {info['title']}", caption=caption, value=was_expanded
            ).classes("w-full border border-gray-200 dark:border-gray-700 rounded-lg mb-2")
            with expansion:
                content = ui.column().classes("w-full gap-0")
            if was_expanded:
                populate(content)

            def on_expansion_toggle(e, c=content, p=populate, tid=track_id) -> None:
                if e.value:
                    state["expanded_tracks"].add(tid)
                    p(c)
                else:
                    state["expanded_tracks"].discard(tid)

            expansion.on_value_change(on_expansion_toggle)

        async def save():
            approved = {
                "genre": {"review": [], "create": []},
                "mood": {"review": [], "create": []},
                "charts": {"review": [], "create": []},
            }
            skipped_no_category = 0
            for track_id, info in tracks.items():
                for row in info["rows"]:
                    key = (track_id, row["kind"], row["tag"])
                    if not state["checked"].get(key):
                        continue
                    if row["is_new"]:
                        category_id = state["category_choice"].get(key, row.get("suggested_category_id"))
                        if category_id is None:
                            skipped_no_category += 1
                            continue
                        approved[row["kind"]]["create"].append({**row, "category_id": category_id})
                    else:
                        approved[row["kind"]]["review"].append(row)

            if not any(approved[k][b] for k in ("genre", "mood", "charts") for b in ("review", "create")):
                msg = "Nothing checked - nothing to save"
                if skipped_no_category:
                    msg = f"{skipped_no_category} new-tag row(s) checked but no category chosen - pick one first"
                notify(msg, type="warning")
                return

            apply_fns = {
                "genre": genre_apply.apply_decisions,
                "mood": mood_apply.apply_decisions,
                "charts": charts_apply.apply_decisions,
            }
            results: dict[str, dict] = {}
            try:
                for kind in ("genre", "mood", "charts"):
                    if approved[kind]["review"] or approved[kind]["create"]:
                        results[kind] = await run.io_bound(
                            apply_fns[kind], approved[kind]["review"], approved[kind]["create"]
                        )
            except Exception as e:
                # apply_decisions() itself only raises for something
                # outside its own per-tag error handling (e.g. Lexicon
                # unreachable) - without this, that exception would just
                # die silently server-side and nothing would ever tell
                # you Apply Tags didn't actually do anything.
                notify(f"Apply failed: {e}", type="negative")
                return

            # Count unique tracks, not len(entries) summed across kinds -
            # a track that got both a genre tag and a mood tag in the
            # same save produces one entry in each result, which would
            # double-count it as "2 tracks" rather than the 1 it is.
            touched_tracks = {e["track_id"] for r in results.values() for e in r["entries"]}
            n_tags = sum(len(e["tags_added"]) for r in results.values() for e in r["entries"])
            failed_creates_all = [f for r in results.values() for f in r["failed_creates"]]
            # Distinct from failed_creates_all: a tag that already
            # exists (or just got created above) but that Lexicon then
            # refused to actually attach to a track - see apply.py's
            # _merge_rows docstring. Used to be swallowed entirely (a
            # bare print() server-side, nothing telling the DJ or this
            # screen it happened at all) - a row like that stayed
            # checked forever with zero explanation, which is exactly
            # what a real DJ hit.
            failed_writes_all = [f for r in results.values() for f in r["failed"]]
            msg = f"Applied {n_tags} tag(s) across {len(touched_tracks)} track(s)"
            if skipped_no_category:
                msg += f" - skipped {skipped_no_category} new-tag row(s) with no category chosen"
            if failed_creates_all:
                names = ", ".join(f"'{f['tag']}' ({f['error']})" for f in failed_creates_all)
                msg += f" - failed to create: {names}"
            if failed_writes_all:
                names = ", ".join(f"{f['artist']} — {f['title']} ({f['error']})" for f in failed_writes_all)
                msg += f" - failed to write: {names}"
            any_write_failed = bool(failed_creates_all or failed_writes_all)
            notify(msg, type="positive" if not any_write_failed else "warning")

            # Drop whatever's now correctly reflected in Lexicon from
            # the in-memory plans - otherwise those rows sit there
            # still checked, the review screen keeps showing tags that
            # already made it to Lexicon (or were already there before
            # this save), and generate()'s "you have unsaved checked
            # rows" guard falsely trips on a plan that was, in fact,
            # fully resolved. resolved_pairs (not just entries' own
            # tags_added) covers both a freshly-written tag and one
            # that turned out to already be on the track - either way,
            # nothing about that row still needs a decision. Rows
            # checked but skipped (no category), or belonging to a
            # track apply.py's _merge_rows reported as failed, are
            # deliberately left in place, still checked - those
            # genuinely still need a decision or a retry.
            plans = {"genre": state["genre_plan"], "mood": state["mood_plan"], "charts": state["charts_plan"]}
            changed = False
            for kind, result in results.items():
                plan = plans[kind]
                if not plan:
                    continue
                resolved_pairs = result["resolved_pairs"]
                if not resolved_pairs:
                    continue
                changed = True
                for bucket in ("auto", "review", "create"):
                    plan[bucket] = [r for r in plan.get(bucket, []) if (r["track_id"], r["tag"]) not in resolved_pairs]
            if changed:
                review_section.refresh()

        ui.button("Apply Tags", on_click=save).props("color=primary").classes("mt-4")

    review_section()

    async def generate():
        include_genre = genre_enabled_checkbox.value
        include_mood = mood_enabled_checkbox.value
        include_charts = charts_enabled_checkbox.value
        if not include_genre and not include_mood and not include_charts:
            notify("Turn on Genre/Subgenre, Mood/Theme, and/or Charts before generating", type="warning")
            return

        enabled_sources = {name for name, cb in source_checkboxes.items() if cb.value}
        if include_genre and not enabled_sources:
            notify(
                "Turn at least one Genre/Subgenre source back on, or turn off Genre/Subgenre",
                type="warning",
            )
            return

        if scan_mode_select.value == "track" and track_picker.value is None:
            notify("Search for and pick a track first", type="warning")
            return

        checked = sum(1 for v in state["checked"].values() if v)
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
            if include_charts:
                state["charts_plan"] = None
            review_section.refresh()

        stop_event.clear()
        generate_button.disable()
        stop_button.visible = True
        stop_button.enable()
        progress_bar.visible = True
        progress.update(
            current=0, total=0, phase="", artist="", title="", result=None,
            rendered=-1, stopping=False, phase_started_at=None,
        )
        preview_container.clear()
        progress_label.text = "starting..."

        def on_track_planned(i, total, track, result):
            progress.update(
                current=i, total=total,
                artist=track.get("artist") or "", title=track.get("title") or "",
                result=result,
            )

        scan_mode = scan_mode_select.value
        # "track" is a GUI-only concept - plan.py/mood_plan.py's own
        # scan_mode only ever means which *pool* to fetch from
        # ("all"/"recent"/"incoming"); a single track is that same
        # "all" pool narrowed down by the existing track_id param
        # (already built for the CLI's own --track-id - see plan.py's
        # docstring), not a fourth pool of its own. limit doesn't mean
        # anything for one already-identified track either.
        picked_track_id = track_picker.value if scan_mode == "track" else None
        backend_scan_mode = "all" if scan_mode == "track" else scan_mode
        limit = int(limit_input.value) if limit_input.value and scan_mode != "track" else None

        new_genre_plan = state.get("genre_plan")
        new_mood_plan = state.get("mood_plan")
        new_charts_plan = state.get("charts_plan")
        any_failed = False
        stopped = False

        # Only "Whole library" has a saved position to resume from at
        # all (see scan_progress.py) - None here means "start from the
        # beginning", same as before this existed, for "recent"/
        # "incoming"/"track" or a first-ever whole-library run. Charts
        # never resumes regardless of scan_mode - see charts_plan.py's
        # own docstring on why a saved position isn't worth it there.
        resuming = scan_mode == "all"

        if include_genre:
            progress.update(phase="Genre", current=0, total=0, rendered=-1, phase_started_at=time.monotonic())
            try:
                new_genre_plan = await run.io_bound(
                    genre_plan_module.generate_plan,
                    limit=limit,
                    scan_mode=backend_scan_mode,
                    track_id=picked_track_id,
                    enabled_sources=enabled_sources,
                    on_track_planned=on_track_planned,
                    should_stop=stop_event.is_set,
                    since_track_id=scan_progress.get_cursor("genre") if resuming else None,
                )
                if new_genre_plan.get("stopped_early"):
                    stopped = True
                if resuming:
                    # Advances only as far as this run actually got -
                    # last_scanned_track_id is None (a no-op) if a stop
                    # landed before the first track finished.
                    scan_progress.advance("genre", new_genre_plan.get("last_scanned_track_id"))
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
                progress.update(phase="Mood", current=0, total=0, rendered=-1, phase_started_at=time.monotonic())
                try:
                    new_mood_plan = await run.io_bound(
                        mood_plan.generate_plan,
                        limit=limit,
                        scan_mode=backend_scan_mode,
                        track_id=picked_track_id,
                        on_track_planned=on_track_planned,
                        should_stop=stop_event.is_set,
                        since_track_id=scan_progress.get_cursor("mood") if resuming else None,
                    )
                    if new_mood_plan.get("stopped_early"):
                        stopped = True
                    if resuming:
                        scan_progress.advance("mood", new_mood_plan.get("last_scanned_track_id"))
                except Exception as e:
                    notify(f"Mood/Theme plan generation failed: {e}", type="negative")
                    any_failed = True

        if include_charts:
            if stop_event.is_set():
                # Same short-circuit as the Mood phase above - a stop
                # requested during an earlier phase means Charts never
                # starts at all either.
                stopped = True
            else:
                progress.update(phase="Charts", current=0, total=0, rendered=-1, phase_started_at=time.monotonic())
                try:
                    new_charts_plan = await run.io_bound(
                        charts_plan_module.generate_plan,
                        limit=limit,
                        scan_mode=backend_scan_mode,
                        track_id=picked_track_id,
                        on_track_planned=on_track_planned,
                        should_stop=stop_event.is_set,
                        # No since_track_id - Charts always scans fresh,
                        # never resumes (see charts_plan.py's docstring).
                    )
                    if new_charts_plan.get("stopped_early"):
                        stopped = True
                except Exception as e:
                    notify(f"Charts plan generation failed: {e}", type="negative")
                    any_failed = True

        progress_bar.visible = False
        stop_button.visible = False
        generate_button.enable()

        state["genre_plan"] = new_genre_plan
        state["mood_plan"] = new_mood_plan
        state["charts_plan"] = new_charts_plan
        review_section.refresh()
        if resuming:
            # The cursor (and possibly the library itself) just moved -
            # drop the cached snapshot so the progress caption re-fetches
            # instead of showing a stale count/position.
            state["library_snapshot"] = None
            scan_progress_caption.refresh()
        genre_counts = new_genre_plan if include_genre else None
        mood_counts = new_mood_plan if include_mood else None
        charts_counts = new_charts_plan if include_charts else None
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
        if charts_counts:
            parts.append(
                f"Charts: {len(charts_counts['auto'])} pre-checked, {len(charts_counts['review'])} review, "
                f"{len(charts_counts['create'])} new-tag"
            )
        summary = " | ".join(parts) if parts else "no plan generated"
        notify(
            f"{'Stopped early' if stopped else 'Plan ready'}: {summary} - nothing written yet, "
            f"review and click Apply Tags",
            type="negative" if any_failed else ("warning" if stopped else "positive"),
        )

    def request_stop():
        stop_event.set()
        progress["stopping"] = True
        stop_button.disable()

    generate_button.on_click(generate)
    stop_button.on_click(request_stop)

    # Settings - per-DJ tuning of source_weights.yaml/mood_weights.yaml
    # (weights, auto-include thresholds, new_tag_category), previously
    # only editable by hand in a text editor. Loads its own fresh copy
    # of each file via config_editor's ruamel round-trip load (not
    # scoring.load_weights()'s plain pyyaml, used elsewhere in this file
    # only for reading) every time the dialog opens - the whole point of
    # ruamel here is that Save can't strip the files' own explanatory
    # comments the way a plain pyyaml.dump() would. Always reloads
    # rather than caching between opens; a single DJ running one
    # instance of this app was never going to hit a stale-data problem
    # worth guarding against.
    def _make_number_setter(data, key, is_int: bool):
        def setter(e) -> None:
            data[key] = int(round(e.value)) if is_int else round(float(e.value), 4)

        return setter

    def _render_weights_card(container, path: Path, title: str, description: str) -> None:
        with container, ui.card().classes("w-full"):
            ui.label(title).classes("font-bold")
            ui.label(description).classes("text-xs text-gray-500 mb-2")
            try:
                data = config_editor.load(path)
            except Exception as e:
                ui.label(f"Couldn't load {path.name}: {e}").classes("text-red-600 text-sm")
                return

            sources = data.get("sources") or {}
            if sources:
                ui.label("Source weights").classes("text-xs text-gray-500 mt-1")
                for key, val in sources.items():
                    with ui.row().classes("items-center gap-2 w-full"):
                        ui.label(key).classes("text-sm w-60")
                        ui.number(value=val, min=0, max=1, step=0.05, format="%.2f").classes(
                            "w-28"
                        ).props("dense outlined").on_value_change(
                            _make_number_setter(sources, key, isinstance(val, int))
                        )

            auto_include = data.get("auto_include") or {}
            if auto_include:
                ui.label("Auto-include").classes("text-xs text-gray-500 mt-2")
                for key, val in auto_include.items():
                    is_int = isinstance(val, int)
                    with ui.row().classes("items-center gap-2 w-full"):
                        ui.label(key).classes("text-sm w-60")
                        ui.number(
                            value=val,
                            min=0,
                            max=None if is_int else 1,
                            step=1 if is_int else 0.05,
                            format="%d" if is_int else "%.2f",
                        ).classes("w-28").props("dense outlined").on_value_change(
                            _make_number_setter(auto_include, key, is_int)
                        )

            if "low_confidence_threshold" in data:
                with ui.row().classes("items-center gap-2 w-full mt-2"):
                    ui.label("low_confidence_threshold").classes("text-sm w-60")
                    ui.number(
                        value=data["low_confidence_threshold"], min=0, max=1, step=0.05, format="%.2f"
                    ).classes("w-28").props("dense outlined").on_value_change(
                        _make_number_setter(data, "low_confidence_threshold", False)
                    )

            if "new_tag_category" in data:
                with ui.row().classes("items-center gap-2 w-full mt-2"):
                    ui.label("new_tag_category").classes("text-sm w-60")
                    try:
                        category_labels = sorted(c["label"] for c in lexicon_client.fetch_categories())
                    except Exception:
                        category_labels = None
                    current = data.get("new_tag_category") or ""
                    if category_labels is not None:
                        options = [""] + category_labels
                        if current and current not in options:
                            options.append(current)  # e.g. renamed/deleted in Lexicon since - keep it visible, not silently dropped
                        category_input = ui.select(options, value=current, with_input=True, clearable=True).classes("w-60")
                    else:
                        category_input = ui.input(value=current).classes("w-60")

                    def _on_category_change(e) -> None:
                        data["new_tag_category"] = e.value or ""

                    category_input.on_value_change(_on_category_change)

            def save() -> None:
                try:
                    config_editor.save(path, data)
                except Exception as e:
                    notify(f"Couldn't save {path.name}: {e}", type="negative")
                    return
                # genre_weights/mood_weights are read once at startup
                # (only for the "cleared the N% bar" badge text) -
                # refresh them in place so a saved change is reflected
                # without needing to restart the app, same as
                # new_tag_category's own no-restart-needed behavior.
                if path == scoring.DEFAULT_WEIGHTS_PATH:
                    genre_weights.clear()
                    genre_weights.update(scoring.load_weights())
                elif path == mood_plan.WEIGHTS_PATH:
                    mood_weights.clear()
                    mood_weights.update(scoring.load_weights(mood_plan.WEIGHTS_PATH))
                review_section.refresh()
                notify(f"Saved {path.name}")

            ui.button("Save", on_click=save).props("outline dense").classes("mt-2")

    async def open_settings_dialog() -> None:
        with ui.dialog() as dialog, ui.card().classes("w-[40rem] max-w-full gap-3"):
            with ui.row().classes("items-center justify-between w-full"):
                ui.label("Settings").classes("text-lg font-bold")
                ui.button(icon="close", on_click=dialog.close).props("flat round dense")
            ui.label(
                "Per-DJ retuning of how tags get scored and auto-included - see each "
                "file's own comments (config/source_weights.yaml, config/mood_weights.yaml, "
                "config/charts_weights.yaml) for the full reasoning behind these numbers."
            ).classes("text-xs text-gray-500")
            _render_weights_card(
                ui.column().classes("w-full"),
                scoring.DEFAULT_WEIGHTS_PATH,
                "Genre / Subgenre",
                "Discogs, plus two independent audio models.",
            )
            _render_weights_card(
                ui.column().classes("w-full"),
                mood_plan.WEIGHTS_PATH,
                "Mood / Theme",
                "Only one source right now - the local mood/theme audio model.",
            )
            _render_weights_card(
                ui.column().classes("w-full"),
                charts_plan_module.WEIGHTS_PATH,
                "Charts",
                "Only one source - billboard-tag's own fuzzy chart-cache match.",
            )
            with ui.card().classes("w-full"):
                ui.label("Chart Cache").classes("font-bold")
                ui.label(
                    "What Charts matches tracks against - billboard-tag's own "
                    "index of what charted when, entirely offline once built. "
                    "\"Update Chart Cache\" re-ingests the bulk chart datasets "
                    "(seconds). \"Fetch from Billboard.com\" scrapes the charts "
                    "those datasets don't cover directly from the site - shows "
                    "a real time estimate before it starts, since it's "
                    "confirmed first."
                ).classes("text-xs text-gray-500 mb-2")

                chart_cache_status = ui.label("").classes("text-xs text-gray-500")

                def _fmt_estimate(est: dict) -> str:
                    mins = est["estimated_seconds"] / 60
                    duration = f"{mins:.0f} min" if mins < 90 else f"{mins / 60:.1f} hr"
                    return f"{est['total_requests']} request(s) across {est['slugs']} chart(s), ~{duration}"

                async def refresh_chart_cache(fetch: bool) -> None:
                    full_history = False
                    if fetch:
                        chart_cache_status.text = "checking how much is actually left to fetch..."
                        try:
                            recent_est, full_est = await run.io_bound(
                                lambda: (
                                    charts_plan_module.estimate_fetch(),
                                    charts_plan_module.estimate_fetch(full_history=True),
                                )
                            )
                        except Exception as e:
                            chart_cache_status.text = ""
                            notify(f"Couldn't estimate fetch size: {e}", type="negative")
                            return
                        chart_cache_status.text = ""

                        with ui.dialog() as confirm_dialog, ui.card():
                            ui.label("Scrape Billboard.com directly?")
                            estimate_label = ui.label(_fmt_estimate(recent_est)).classes(
                                "text-sm text-gray-600 dark:text-gray-400"
                            )
                            full_history_checkbox = ui.checkbox(
                                "Full historical re-fetch instead (rare - only for a "
                                "brand-new chart that's never been fetched at all)"
                            ).props("dense")

                            def _on_toggle(e) -> None:
                                estimate_label.text = _fmt_estimate(full_est if e.value else recent_est)

                            full_history_checkbox.on_value_change(_on_toggle)
                            ui.label(
                                "Only charts the bulk datasets don't already cover run "
                                "at all (see charts/billboard_tag.py's own "
                                "DATASET_SOURCES). Runs in the background; Track Record "
                                "stays usable while it does."
                            ).classes("text-xs text-gray-500")
                            with ui.row().classes("w-full justify-end gap-2 mt-2"):
                                ui.button("Cancel", on_click=lambda: confirm_dialog.submit("cancel")).props("flat")
                                ui.button(
                                    "Fetch", color="primary",
                                    on_click=lambda: confirm_dialog.submit("continue"),
                                )
                        if await confirm_dialog != "continue":
                            return
                        full_history = full_history_checkbox.value

                    chart_cache_status.text = (
                        "scraping Billboard.com - this can take a while..." if fetch
                        else "loading bulk chart datasets..."
                    )
                    try:
                        await run.io_bound(charts_plan_module.refresh_cache, fetch, full_history)
                    except Exception as e:
                        chart_cache_status.text = ""
                        notify(f"Chart cache refresh failed: {e}", type="negative")
                        return
                    chart_cache_status.text = ""
                    notify(
                        "Chart cache refreshed" + (" (fetch)" if fetch else " (load)"),
                        type="positive",
                    )

                with ui.row().classes("gap-2"):
                    ui.button(
                        "Update Chart Cache", on_click=lambda: refresh_chart_cache(False)
                    ).props("outline dense size=sm")
                    ui.button(
                        "Fetch from Billboard.com", on_click=lambda: refresh_chart_cache(True)
                    ).props("outline dense size=sm color=warning")
            with ui.card().classes("w-full"):
                ui.label("Audio models").classes("font-bold")
                ui.label(
                    "Version numbers Essentia itself assigns. Opening this doesn't "
                    "check the network on its own - click \"Check for Updates\" to "
                    "actually ask essentia.upf.edu whether anything newer is out."
                ).classes("text-xs text-gray-500 mb-2")

                model_check_status = ui.label("").classes("text-xs text-gray-500")

                async def apply_model_update(model, target_version: int, current_version: int) -> None:
                    size_note = " (~330 MB)" if "discogs-maest" in model.label else " (~2-20 MB)"
                    with ui.dialog() as confirm_dialog, ui.card():
                        ui.label(
                            f"Update {model.label} from v{current_version} to "
                            f"v{target_version}? Downloads the new version{size_note}, "
                            f"switches fetch/{model.module_path.name} to use it, and "
                            f"removes the old cached file. Track Record needs a "
                            f"restart afterward to actually start using it - the "
                            f"running app already has the old version loaded in "
                            f"memory."
                        )
                        with ui.row().classes("w-full justify-end gap-2 mt-2"):
                            ui.button("Cancel", on_click=lambda: confirm_dialog.submit("cancel")).props("flat")
                            ui.button(
                                "Update", color="primary",
                                on_click=lambda: confirm_dialog.submit("continue"),
                            )
                    if await confirm_dialog != "continue":
                        return
                    model_check_status.text = f"updating {model.label}..."
                    try:
                        await run.io_bound(model_versions.apply_update, model, target_version)
                    except Exception as e:
                        model_check_status.text = ""
                        notify(f"Couldn't update {model.label}: {e}", type="negative")
                        return
                    state["model_check_results"].pop(model.label, None)
                    model_check_status.text = ""
                    model_rows.refresh()
                    notify(
                        f"{model.label} updated to v{target_version} - restart "
                        f"Track Record for this to take effect",
                        type="positive",
                    )

                @ui.refreshable
                def model_rows() -> None:
                    for model in model_versions.MODEL_FILES:
                        check = state["model_check_results"].get(model.label)
                        with ui.row().classes("items-center gap-2"):
                            ui.label(model.label).classes("text-sm w-96")
                            if check is not None and check.get("error"):
                                ui.label(f"couldn't check: {check['error']}").classes(
                                    "text-xs text-red-600"
                                )
                            elif check is not None and check["update_available"]:
                                ui.label(f"v{check['current']} → v{check['latest']} available").classes(
                                    "text-xs text-orange-500"
                                )
                                ui.button(
                                    "Update",
                                    on_click=lambda model=model, check=check: apply_model_update(
                                        model, check["latest"], check["current"]
                                    ),
                                ).props("outline dense size=sm")
                            elif model.weights_path.exists():
                                label = f"v{model.current_version}"
                                if check is not None:
                                    label += " (up to date)"
                                ui.label(label).classes(
                                    "text-xs bg-gray-100 dark:bg-gray-800 rounded px-2 py-0.5"
                                )
                            else:
                                ui.label("not downloaded yet").classes("text-xs text-gray-400 italic")

                model_rows()

                async def check_model_updates() -> None:
                    model_check_status.text = "checking essentia.upf.edu/models.html..."
                    results = await run.io_bound(model_versions.check_all)
                    state["model_check_results"] = {r["label"]: r for r in results}
                    model_check_status.text = ""
                    model_rows.refresh()
                    n_updates = sum(1 for r in results if r["update_available"])
                    n_errors = sum(1 for r in results if r.get("error"))
                    if n_errors:
                        notify(f"Couldn't reach essentia.upf.edu for {n_errors} model(s)", type="negative")
                    elif n_updates:
                        notify(f"{n_updates} model update(s) available", type="warning")
                    else:
                        notify("All audio models up to date", type="positive")

                ui.button("Check for Updates", on_click=check_model_updates).props(
                    "outline dense size=sm"
                ).classes("mt-2")
        # Same await-the-dialog idiom as every confirm dialog elsewhere
        # in this file (reset_scan_progress, generate's discard-
        # unsaved-changes prompt) - dialog.open() alone (a plain sync
        # call outside that idiom)
        # was tried first and silently never showed anything on screen;
        # __await__ is what actually flips this dialog's value to True
        # on the frontend. No submit() call needed here since there's
        # nothing to return - the × button's dialog.close() (or ESC, or
        # a backdrop click, both on by default) sets value back to
        # False, which is what resolves this await either way.
        await dialog

    settings_button.on_click(open_settings_dialog)


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
