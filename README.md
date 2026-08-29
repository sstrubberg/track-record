# Track Record

A companion app for [Lexicon](https://www.lexicondj.com/) that
enriches a DJ's library with tags Lexicon's built-in "Find Tags" doesn't
reliably provide: accurate, granular genre/subgenre tags, mood/theme
tags, and which Billboard charts a track actually appeared on, all with
full source attribution and a review step, run side-by-side from one
screen.

Built to be shared with other DJs, fully open source.

## Problem being solved

Lexicon's built-in genre tagging (pulling from Beatport, Spotify,
MusicBrainz, Discogs) merges results with no visible source and no way
to verify a match - and in practice comes back too generic (e.g.
"House" instead of the actual subgenre). This toolkit draws from its
own, different sources instead - Discogs and independent local audio
models, not Lexicon's Beatport/Spotify/MusicBrainz mix - and surfaces
each one separately with confidence, so a DJ can decide what to trust
rather than accept a black-box merge. A tag confident enough to clear
its own auto-include bar is pre-checked, but review is the default for
everything else, not just edge cases.

## Scope

Three actions, one shared pipeline shape, all run from the same review
screen:

- **Genre/Subgenre** - Discogs + two independent local audio models
- **Mood/Theme** - a local audio model only (see below for why)
- **Charts** - fuzzy-matches each track against a real Billboard chart
  history cache (ported from a separate project, `billboard-tag`; see
  `companion-app/charts/`) - a genuinely different kind of source from
  the other two, since it's matching the track itself against a
  catalog of chart appearances rather than inferring genre/mood from
  audio or metadata

## Getting started

1. **Turn on Lexicon's own Local API** - off by default, and nothing
   here works without it. In Lexicon: **Settings → Integrations →
   Local API**, switch on **"Local API Enabled"**. Track Record talks
   to Lexicon entirely through this (`localhost:48624`, no separate
   account or token) - Lexicon has to already be open, every time, for
   any of this to do anything.
2. **Install and configure the companion app**:
   ```
   cd companion-app
   pip install -r requirements.txt
   cp .env.example .env   # fill in DISCOGS_TOKEN - see companion-app/README.md
   ```
3. **Run it**: `python track_record.py` - one native window, one
   command, covers Genre/Subgenre, Mood/Theme, and Charts all three.

See [companion-app/README.md](companion-app/README.md) for the full
walkthrough - what each part of the screen does, the Settings dialog,
checking for audio model updates, and running an individual fetch
source standalone to sanity-check it.

## Architecture

Lexicon plugins run in a sandboxed JS environment - no `require()`, no
spawning processes, no native modules - so Essentia/discogs-maest could
never run inside one directly. The companion app sidesteps that by
being a separate local Python process a DJ runs directly
(`python track_record.py`, a thin entry point over review_ui.py's
actual screen), talking to Lexicon purely over its own Local API (see
"Getting started" above for turning that on) - reading the library,
writing tags - with nothing on the Lexicon side involved.

```
track-record/
├── LICENSE                      # AGPL-3.0
├── NOTICE.md                    # third-party model attribution
└── companion-app/               # the actual work happens here (Python)
    ├── charts/                  # billboard_tag.py - ported billboard-tag logic,
    │                             #   untouched, plus this library's own chart_map.json
    │                             #   (charts_plan.py imports it - see there)
    ├── fetch/
    │   ├── musicbrainz.py               # untouched, just no longer wired into plan.py
    │   ├── discogs.py
    │   ├── llm_web_search.py            # Claude/Gemini + web search, artist+title+genre in
    │   ├── audio_model.py               # discogs-maest wrapper (Essentia) - genre/style
    │   ├── audio_model_genre_effnet.py  # genre_discogs400 - second, independent genre model
    │   └── audio_model_mood.py          # discogs-effnet + mtg_jamendo_moodtheme - mood/theme
    ├── scoring.py                  # weighted noisy-OR - shared by all three actions
    ├── genre_family_hint.py        # Genre/Subgenre: per-tag category suggestion for new tags
    ├── lexicon_client.py           # shared Local API client (tracks, tags, writes)
    ├── scan_progress.py            # whole-library scan position, per action (not Charts)
    ├── model_versions.py           # checks/switches audio model versions - see Status
    ├── config_editor.py            # ruamel.yaml round-trip load/save for the Settings dialog
    ├── apply.py                    # writes approved tags via Lexicon Local API - shared
    ├── plan.py                     # Genre/Subgenre: load -> fetch -> score
    ├── mood_plan.py                # Mood/Theme: its own load -> fetch -> score
    ├── mood_apply.py               # Mood/Theme: thin wrapper around apply.py
    ├── charts_plan.py              # Charts: matches tracks against charts/billboard_tag.py's cache
    ├── charts_apply.py             # Charts: thin wrapper around apply.py
    ├── review_ui.py                # NiceGUI screen for ALL THREE actions, one window,
    │                                #   one Generate Plan, checkboxes choose which
    │                                #   action(s) to include, plus a Settings dialog
    ├── track_record.py             # `python track_record.py` - thin, brand-named
    │                                #   entry point that just calls review_ui.main()
    └── config/
        ├── source_weights.yaml    # Genre/Subgenre tuning
        ├── mood_weights.yaml      # Mood/Theme tuning, same shape, separate file
        ├── charts_weights.yaml    # Charts tuning, same shape again
        └── genre_taxonomy.yaml    # Discogs 400-style family/subgenre list (genre_family_hint.py)
```

## Shared pipeline

`load → fetch → score/plan → review → apply` - each stage is a clean
handoff: read what's needed from Lexicon, call out to fetch sources,
score the results into a plan file, present it for review, then write
back only what's approved.

### Choosing what to scan

A plan generation covers one of four pools, picked from the review
screen itself - no terminal flags: the **whole library** (optionally
capped to the first N tracks), the **N most recently added** tracks,
everything currently in Lexicon's **Incoming** bin, or a **single
track** searched by artist/title. A full-library run is slow
(rate-limited API calls plus local audio inference per track), so a
**Stop** button aborts mid-run - it takes effect after the current
track finishes, not instantly, and keeps whatever was already planned
as a normal, smaller plan rather than discarding it.

**Single track** is for spot-checking one song - re-running a track
after a scoring change, or just seeing what a specific edit gets
proposed without waiting on a real scan. The searchable picker is
built from the same `track_id` parameter `plan.py`/`mood_plan.py`'s
own CLIs already exposed via `--track-id`; the GUI's only real
addition is resolving an artist/title search into that id, since a DJ
thinks in terms of "artist - title," not Lexicon's internal numeric
ids. Different DJ edits of the same song are still separate options -
edit/version info (e.g. "(MM Edit)") already lives in Lexicon's own
title field, so they read as distinct entries with no extra handling
needed. Doesn't have or need a scan-progress position, same as
"recent"/"incoming."

A capped **whole-library** run remembers where it left off
(`scan_progress.py`), separately per action - a second 100-track run
picks up at track 101 instead of re-covering the first 100, so working
through a large backlog in batches makes real progress instead of
looping over the same tracks. The review screen shows this as an "X of
Y tracks scanned" caption with its own **Reset** button (confirmed
before it takes effect) to deliberately start an action's whole-
library scanning over from scratch - worth doing after a scoring
change, for instance. Only "whole library" has a position like this to
save; "recent" is already a moving window and "incoming" self-narrows
as tracks leave that bin, so neither shows or needs one. Purely about
which tracks get *scanned* - it's unrelated to, and doesn't change,
the per-tag check every scan already does regardless (a candidate tag
already applied to a track is always skipped).

Charts is the one exception: no saved position at all, even for
"whole library" - see below for why.

### Genre/Subgenre fetch sources

Queried independently per track, never merged blindly - and each one
has its own checkbox in the review screen (on by default), so a DJ can
turn a source off entirely for a run rather than just down-weighting
it in `source_weights.yaml`. Useful on its own: the two audio models
are by far the slowest part of a run (local inference per track), so a
metadata-only pass with both off is a fast way to sanity-check Discogs
coverage before committing to a full scan. At least one source has to
stay on. `plan.py`'s CLI takes the same choice via `--sources
audio_model,audio_model_genre_effnet` (skip Discogs) or `--sources
discogs` (skip both audio models).

- **Discogs** - style/genre via public API, artist+title search
- **LLM web-search classification** - given artist, title, and current
  genre, prompted to return every defensible subgenre tag with its
  source URL and the model's own reported confidence, not one "best" answer
- **discogs-maest** (Essentia/MTG, local audio model) - runs directly on
  the audio file, no internet dependency, so it's the only source that
  returns anything for untraceable tracks (transition edits, mashups,
  bootlegs, DJ tools with no web presence)
- **genre_discogs400** (Essentia/MTG, local audio model) - a second,
  independent audio model on the same Discogs-400-style taxonomy as
  discogs-maest, but an EfficientNet classification head on
  discogs-effnet embeddings rather than an end-to-end transformer -
  real architectural diversity, not the same model asked twice.
  Shares its embedding extractor with the Mood/Theme audio model
  rather than downloading a second copy (~2 MB on top if Mood/Theme's
  weights are already present, ~20 MB combined otherwise)

MusicBrainz was a fetch source here through 2026-08-07 - dropped after
this project's own DJ found its suggestions consistently disappointing
in day-to-day use. `fetch/musicbrainz.py` is untouched and still works
standalone (`python fetch/musicbrainz.py "artist" "title"`); it's just
no longer wired into `plan.py`'s `SOURCES`. genre_discogs400 took its
slot as the third source - worth knowing: with two of the three
sources being audio models that share an embedding extractor, those
two agreeing with each other is weaker evidence than the old
Discogs-catalog-vs-audio-ML kind of agreement was. `auto_include.
min_agreeing_sources` is 3, not 2, as a direct consequence - auto-include
via source agreement now needs literal unanimity across all three
sources, not just any 2 of 3, closer in spirit to how strict "2 of 2"
was right after MusicBrainz was first dropped. `min_confidence` in
`source_weights.yaml` is still the other lever if this needs further
tuning once there's more real-world signal.

No cap on candidate tags per track.

`discogs-maest`'s classes are `Genre---Style` pairs (e.g.
`"Hip Hop---RnB/Swing"`); both halves become separate candidates, so a
genre-level tag can benefit from cross-source agreement the same way a
style-level one does.

#### Matching a DJ library's title/artist strings against public catalogs

DJ libraries routinely spell things in ways Discogs won't match
verbatim - it normalizes before searching, then falls back to the
untouched original if the normalized version finds nothing:

- **Edit/version suffixes** - `"Hollaback Girl (Intro Clean)"`,
  `"... (MM Edit)"` - stripped from the title before search (a survey
  of one real library found 99% of tracks carried at least one of
  these), via a paren/bracket-stripping regex.
- **Merged featured-artist credits** - `"Nelly Furtado ft Timbaland"`
  is stored as one Artist field, but Discogs indexes releases under the
  primary artist alone - split on the common separators (`ft`, `feat`,
  `x`, `&`, commas) before search, not used to build a fuzzy key.
- **Bootleg/compilation Discogs releases** - a result whose `format`
  says `Compilation` or `Unofficial Release` describes dozens of
  unrelated tracks, not the one asked about, and is skipped rather than
  trusted.

These fix the common, generic cases. Some mismatches are one-off and
not fixable by normalization - e.g. an act catalogued under a
stylized spelling Discogs itself uses (`N*E*R*D`). A small hand-
maintained alias dict, keyed by the DJ's own spelling, is the natural
way to handle a specific one-off by hand if one turns out to matter.

### Mood/Theme fetch source

Just one, deliberately: MusicBrainz and Discogs are genre-and-catalog
databases with essentially no reliable mood data to query, and LLM web
search stays on hold over API cost for the same reason it does for
Genre/Subgenre - so the local audio model (`fetch/audio_model_mood.py`)
is the whole story here, at least until that changes.

Unlike `discogs-maest` (a single end-to-end model), MTG-Jamendo
mood/theme is a two-stage pipeline: `discogs-effnet` extracts a general
audio embedding (used here purely as an embedding extractor - its own
400-style genre predictions are discarded, only its penultimate layer
is read), which a small classification head trained on top of those
embeddings turns into 56 mood/theme class probabilities - `energetic`,
`uplifting`, `dark`, `romantic`, `film`, `summer`, and 50 more. Same
`{"tag", "score", "source", "url", "note"}` candidate shape as every
other source, so it plugs into scoring.py/review unchanged.

Worth being upfront about: this is a genuinely noisier task than genre
classification. MTG's own published metrics put this exact model's
test PR-AUC at 0.14, and in this project's own testing a track's
top-scoring mood rarely clears 20-30% even when it's clearly the right
call (an orchestral film-score fanfare scoring highest on `film` /
`action` / `epic` / `trailer` - correct, just not confident-sounding
the way genre predictions tend to be). `config/mood_weights.yaml`'s
auto-include threshold is set low relative to Genre/Subgenre's as an
honest consequence of that, not a claim that this source deserves more
trust - expect most Mood/Theme runs to lean heavily on the review
screen rather than auto-include.

### Charts matching

Genuinely different from the other two: not inferring anything from
audio or metadata, just matching a track against a real catalog of
what charted when. Built on `billboard-tag`, a separate project ported
in as `companion-app/charts/billboard_tag.py` untouched (its own README
says so explicitly - a verbatim copy, not a fork) - `charts_plan.py`
imports it for its stable matching primitives rather than
re-implementing them.

The match itself: normalize this track's artist/title (stripping DJ
edit suffixes like "(Intro Clean)", spelling out symbols so "&"
matches "and"), then try an exact key lookup against the chart cache,
falling back to a fuzzy match (rapidfuzz, cutoff 88) if nothing exact
turns up, and a second fallback key (for a bare-slash artist credit
like "The Jackson 5/The Jacksons") if the primary key finds nothing at
all, exact or fuzzy. A hit doesn't score one tag at a time the way
Genre/Subgenre's sources do - every chart the matched song ever
appeared on becomes a candidate tag at once, all sharing that one
match's confidence, since there's no second independent signal per tag
the way Discogs vs. an audio model are for Genre/Subgenre.

Most tracks genuinely have no chart appearance at all - that's the
normal outcome here, not a sign something's broken, unlike Genre/
Subgenre or Mood/Theme where *some* candidate (even a low-confidence
one) is the usual case. So a track that already shows in the review
list via another action, but has nothing from Charts, gets a small
note explaining why instead of just silently having no Charts
sub-group: "no chart match found," "title looks like a mashup/
transition/blend - skipped," or "already tagged: X, Y" if this track
already carries every chart tag the match would have proposed.

Matching against a chart record requires a real
`companion-app/charts/chart_map.json` mapping this library's own tag
labels to Billboard chart slugs - the built-in fallback
(`DEFAULT_CHART_MAP`) is the original billboard-tag author's own tag
names, not yours, and shouldn't be relied on for a different library.
Generate one for real via `python billboard_tag.py init` from inside
`charts/` (review its proposed mapping before trusting it - a loose
fuzzy match can confidently claim an existing broad genre tag for a
chart it doesn't actually belong to; this project's own `chart_map.json`
has a few charts deliberately left unmapped for exactly that reason,
see its `_comment` field).

The chart cache itself (`billboard_cache.json`) is built entirely
offline from real Billboard chart history - Settings' "Chart Cache"
card (or `python billboard_tag.py load`/`fetch` from a terminal) is
what actually builds and refreshes it; matching a track against it
touches no network at all. "Update Chart Cache" (`load`) re-ingests
bulk chart datasets in seconds; "Fetch from Billboard.com" scrapes the
charts those datasets don't cover directly from the site.

Before confirming a fetch, Settings shows a real estimate
(`charts_plan.py`'s `estimate_fetch()`) instead of a blanket "can take
hours" warning - genuinely important, because of a real quirk in
billboard_tag.py's own progress-tracking (untouched, so worked around
here rather than fixed there): `week_dates()` always counts backward
from *today* in fixed steps, with no awareness of when a previous
fetch actually ran, so unless today happens to land on an exact
step-multiple of the last run, none of the freshly generated dates
line up with what `billboard_cache.progress.json` already recorded as
done - confirmed directly, a fetch four weeks after a real one showed
*zero* overlap, meaning it would've treated the entire 1958-forward
history as still needed (measured: 30,050 requests, ~28 hours) despite
almost all of it already being cached. The default estimate instead
caps the range to the last two years - any real new chart data is
recent by definition, so this sidesteps the mismatch for a fraction of
the cost (measured the same day: 990 requests, ~1 hour) - with a "Full
historical re-fetch instead" checkbox for the rare case that actually
needs the full range (a brand-new chart just added to `chart_map.json`
that's never been fetched at all).

This is also why Charts never keeps a resumable whole-library scan
position the way Genre/Subgenre and Mood/Theme do (see "Choosing what
to scan" above) - matching against an already-loaded cache is an
in-memory lookup, no per-track network call or audio inference, so
redoing a whole-library run costs nothing worth avoiding a saved
position for.

### Scoring

```
confidence = 1 - Π(1 - weight_i × score_i)   for each source i that found the tag
```

`weight_i` is configured per-source, one YAML file per action
(`companion-app/config/source_weights.yaml` for Genre/Subgenre,
`mood_weights.yaml` for Mood/Theme, `charts_weights.yaml` for Charts) -
each ships with sensible defaults and is fully editable, either by
hand or via the Settings dialog, which is how a different DJ retunes
the toolkit for their own library without touching code.

### Review

One rule for the whole screen: **generating a plan never writes
anything** - it's always just a preview, and exactly one action
writes to Lexicon: **"Apply Tags"**, which writes whatever is
checked. Earlier builds had a second write path (an "Apply now"
button for auto-include tags, gated by a separate "Dry run" checkbox)
- collapsed into this one, since a DJ shouldn't need two different
mental models for what's really one action.

"Generate Plan" has checkboxes for which action(s) to include -
Genre/Subgenre, Mood/Theme, Charts, any combination, in the same run -
rather than a DJ needing to run separate scans against the same tracks
just because the three pipelines live in separate config/plan files
underneath. Selecting more than one runs them as sequential phases
(Genre, then Mood, then Charts), each with its own live per-track
progress; stopping during an earlier phase skips every phase after it
entirely rather than starting a new scan after a stop was already
requested. Regenerating with only some of the three checked leaves the
others' existing plans untouched in the review list.

Every candidate, from every tier, is grouped by track - but a track
with candidates from more than one action doesn't dump them into one
undifferentiated pile: its expansion splits into a "Genre / Subgenre"
sub-group, a "Mood / Theme" sub-group, and/or a "Charts" sub-group (only
whichever actually have candidates for that track), each independently
confidence-sorted with its own "select all," so the different kinds
never blur together into a wall of unrelated checkboxes. Within each
sub-group: plain checkbox + tag + confidence row, source/notes/links
behind a `⋮` overflow control. A row that already cleared *its own
action's* auto-include confidence bar (each action is tuned
independently - see Scoring below) starts **pre-checked**, with a
green check and a tooltip explaining why (and naturally sorts near the
top of its sub-group, since rows are ordered by confidence) - still
just a checkbox, uncheck it like any other if you disagree. A global
"Select all" and each sub-group's own "select all" speed up working
through a large plan; all of them just drive the same per-row
checkboxes "Apply Tags" reads.

A plan spanning thousands of tracks is paginated - 50 tracks per page
by default, configurable to 25/100/200 in the UI - with each track's
rows built only the first time it's actually expanded, rather than all
at once up front. Measured against a synthetic 2,000-track plan, that
took the initial page from ~521,000 DOM nodes / 20.6s to build down to
937 nodes / 0.73s. Checked/category state lives in memory keyed by
track rather than in the on-page checkbox widgets themselves, so it
survives turning the page - checking a tag on page 1 and a different
one on page 3 both land in the same "Apply Tags" click, and both
"select all" controls act on the whole plan, not just the visible
page.

A row proposing a tag that doesn't exist in the library yet also gets
a category picker - always changeable, and never pre-checked
regardless of confidence, since creating a tag is a bigger action than
adding an existing one and always needs an explicit decision. For
Genre/Subgenre specifically, the picker's own default tries a smarter
guess first: if the proposed tag's name is a taxonomy-recognized
family or subgenre name (`config/genre_taxonomy.yaml`, the same
Discogs 400-style list `plan.py`'s scoring already draws on) and that
family's `Sub-genre - {Family}` category already exists in Lexicon,
that's the pre-filled default - "P.Funk" defaults to Sub-genre - Funk
/ Soul rather than the flat `new_tag_category` catch-all. Falls back
to `new_tag_category` (if that resolves to a real category) whenever
there's no family match, the name is ambiguous across more than one
family, or the matched family has no category yet - see
`genre_family_hint.py`'s own docstring for why this is deliberately
narrower than the "Reorganize Genre Tags" workflow this project once
shipped and removed: no renaming, no moving existing tags, no picker
of its own - just a smarter default for a dropdown that already
existed. Mood/Theme and Charts have no such taxonomy to draw on, so
their create rows only ever use each one's own flat `new_tag_category`
default. Clicking "Apply Tags" splits whatever's checked by kind under
the hood and calls each action's own `apply_decisions()` - any
combination in one click - then reports one combined result.

Different DJ edits of the same song ("Promiscuous (Intro Clean)" /
"Promiscuous (Quick Hit Clean)") show up as separate tracks - separate
audio files, separate Lexicon track_ids, detected automatically by
artist + title (matched on primary artist, so "Deee-Lite" and
"Deee-Lite Ft. Q-Tip" on two edits of the same song still find each
other, and edit/version suffixes in parentheses or brackets are
ignored for the match). A remix only groups with other DJ edits of
that *same* remix, never with the plain version of the song or a
different remix of it - a remix is often a different genre entirely
from what it remixes, so a genre tag that fits one has no business
being offered as a one-click copy onto the other, while two edits
built from the identical remix genuinely do share genre. A track with
a detected sibling gets a "Copy checked genre tags from
'\<sibling title>'" button next to its Genre/Subgenre "select all" -
work through one edit, move on to its sibling, and pull whatever's
already checked there in one click, but only where this track's own
audio/catalog lookup also proposed that exact tag as a candidate,
never inventing one it didn't earn. A one-time copy, not a live link -
nothing stays bound afterward, so unchecking something on either track
later never cascades anywhere, and the button always names exactly
which edit it's pulling from. (An earlier version auto-synced every
check bidirectionally and live between siblings; dropped after real
use found two problems with it - no visibility into which edits a
track was actually linked to beyond a bare count, and no way to let
one edit genuinely differ without the live link fighting back. A later
version pushed checked tags forward onto a sibling instead, which
meant scrolling back up to an earlier edit to push from it rather than
working a track and pulling sideways from whichever sibling was
already done. Naming the sibling explicitly and pulling from it fixes
all of that, for less code than the live-binding version needed.)
Mood/Theme has no such button at all: testing on two real edits of the
same song found genre stayed consistent between them while
mood-adjacent tags genuinely differed (a spoken intro on one edit
reading as "Ballad"/"Vocal" to the audio model) - mood is
edit-sensitive in a way genre isn't, so treating it the same way would
paper over a real difference rather than remove busywork.

Generating a new plan while the current one has checked-but-unsaved
rows asks for confirmation first, rather than silently discarding
them - checked state also isn't lost on relaunch, since the whole plan
(including which rows cleared the auto-include bar) is restored from
disk the same way review/create rows always were.

### Apply

Merge, never replace: reads the track's live tag array and appends,
since Lexicon's `tags` field is flat and a bare overwrite wipes
unrelated tags. Only writes to tag categories that already exist in
Lexicon; never creates a new category on the user's behalf. A tag that
already exists is reused rather than recreated, even for a "propose a
new tag" row - matters on a retried save, so nothing ends up
duplicated in Lexicon's tag list.

`apply.py`'s own `apply_auto()` (`python apply.py` from the CLI)
applies a plan's auto-include tier immediately, no review step - a
deliberately different, opt-in tool for scripted/headless use (e.g. a
cron job that generates a plan overnight), not what the review screen
itself does.

## License

AGPL-3.0 (see [LICENSE](LICENSE)), required once the companion app
links against Essentia. See [NOTICE.md](NOTICE.md) for third-party
model attribution (MTG/Essentia pretrained weights, CC BY-NC-SA 4.0).

No monetization planned - free/open distribution to other DJs is the
explicit goal.

## Status

All three pipelines (fetch → score → plan → review → apply) run
end-to-end entirely from their own review screen - no terminal needed
except to launch one. Genre/Subgenre has been exercised against a real
~1,780-track Lexicon library, including real writes; Mood/Theme has
been exercised the same way at smaller scale so far; Charts has been
verified against the same real library (a real `billboard_cache.json`
built from public Billboard chart datasets, confirmed correct matches
and auto-include behavior) but not yet used for a real Apply Tags run.

- **Genre/Subgenre fetch sources**: Discogs and two independent local
  audio models (`discogs-maest` and `genre_discogs400`) are
  implemented, each with the normalization described above.
  `llm_web_search.py` is a stub, on hold over web search API cost.
  MusicBrainz (`fetch/musicbrainz.py`) is implemented and still works
  standalone but isn't wired into `plan.py` - dropped as an active
  source after this project's own DJ found its suggestions
  consistently disappointing.
- **Mood/Theme fetch source**: the local `discogs-effnet` +
  `mtg_jamendo_moodtheme` audio model (see above) - implemented,
  verified against real tracks.
- **Charts source** (`charts_plan.py`, built on
  `charts/billboard_tag.py`): fuzzy-matches artist/title against a
  real Billboard chart cache - implemented, verified against real
  tracks. Needs a real `charts/chart_map.json` for this library (see
  "Charts matching" above) - ships with one already generated and
  reviewed for this project's own library.
- **Scoring** (`scoring.py`, weighted noisy-OR): implemented, shared
  by all three actions - each keeps its own `config/*_weights.yaml`.
- **Plan generation** (`plan.py` / `mood_plan.py` / `charts_plan.py`,
  each its own `generate_plan()`): reads tags/tracks over the Lexicon
  Local API, resolves candidates against what already exists, writes
  an auto-include / needs-review / propose-a-new-tag plan. Callable
  from the CLI or directly (used by each action's GUI); a scan-mode
  picker chooses whole library / most recently added / Incoming / a
  single track searched by artist-title, with an optional Stop
  mid-run. Genre/Subgenre also has a per-source toggle; Mood/Theme and
  Charts don't need one, each with only one source to toggle. Charts
  is also the one action with no resumable whole-library scan
  position - see "Choosing what to scan" above for why.
- **Review UI** (`review_ui.py`, one NiceGUI native window -
  `python track_record.py` is the only command any action needs): the
  whole workflow lives here - one "Generate Plan" with checkboxes for
  which action(s) to include, live per-track progress across however
  many phases are selected (never writes anything), each track's
  candidates split into Genre/Subgenre, Mood/Theme, and/or Charts
  sub-groups (only whichever have candidates for that track) so they
  never blur together, global and per-sub-group "Select all", a
  category picker on new-tag rows (with a per-tag family suggestion for
  Genre/Subgenre - see `genre_family_hint.py`), source/note/links
  behind an overflow menu, and the one action that writes - "Apply
  Tags" - applying whatever's checked (pre-checked auto-include rows
  included) via each action's own `apply_decisions()`, reporting one
  combined result. A gear-icon **Settings** dialog (`config_editor.py`,
  round-trip YAML so saving never strips any config file's own
  comments) covers all three actions' source weights, auto-include
  thresholds, and `new_tag_category` without hand-editing YAML, plus a
  "Chart Cache" card for refreshing `billboard_cache.json`.
- **Apply** (`apply.py`, shared; `mood_apply.py`/`charts_apply.py` thin
  wrappers around it with their own plan/log paths): merge-never-replace
  - reads the track's live tag array and appends rather than
  overwrites, so nothing existing gets silently dropped. A tag that
  already exists is reused rather than recreated, even for a "propose
  a new tag" row.
- **Model versions** (`model_versions.py`, also in the Settings
  dialog): the audio-model fetch scripts cache a model file forever
  once downloaded - no version check of any kind on their own, so
  nothing changes underfoot between runs. The Settings dialog's "Audio
  models" card shows current versions from what's already downloaded
  (no network call just from opening it); its own "Check for Updates"
  reads Essentia's own model listing for real and gives any
  out-of-date model an "Update" button, behind a confirmation (it's a
  real download, a source-file edit, and needs a restart, not a config
  value). `python model_versions.py` / `--apply` is the same check/
  update from a terminal instead, e.g. for scripting.

`llm_web_search.py` remains a stub, on hold over web search API cost
for a full library pass.
