# Track Record

A [Lexicon](https://www.lexicondj.com/) DJ plugin + companion app that
enriches a DJ's library with tags Lexicon's built-in "Find Tags" doesn't
reliably provide: accurate, granular genre/subgenre tags and mood/theme
tags, both with full source attribution and a review step. Bundles in
the existing [Billboard chart-tagging tool](https://github.com/sstrubberg/billboard-tag)
as a third action, since all three share the same
fetch → score → review → apply shape.

Built to be shared with other DJs, fully open source.

## Problem being solved

Lexicon's built-in genre tagging (pulling from Beatport, Spotify,
MusicBrainz, Discogs) merges results with no visible source and no way
to verify a match - and in practice comes back too generic (e.g.
"House" instead of the actual subgenre). This toolkit surfaces every
source separately, with confidence, and only asks for a human decision
on the ones that are genuinely ambiguous.

## Scope

Three actions, one shared pipeline shape:

- **Charts** - ported from `billboard_tag.py`
- **Genre/Subgenre** - MusicBrainz + Discogs + a local audio model
- **Mood/Theme** - a local audio model only (see below for why)

## Architecture

Lexicon plugins run in a sandboxed JS environment - no `require()`, no
spawning processes, no native modules - so Essentia/discogs-maest can't
run inside the plugin itself. The companion app is a separate local
Python process; the Lexicon plugin (`lexicon-plugin/`) is a thin
trigger/status layer, or the companion app is simply run directly by
the user, closer to how `billboard_tag.py` works today.

```
track-record/
├── LICENSE                      # AGPL-3.0
├── NOTICE.md                    # third-party model attribution
├── lexicon-plugin/              # thin JS layer, config.json defines the actions
└── companion-app/               # the actual work happens here (Python)
    ├── charts/                  # ported billboard_tag.py logic - reference
    │                             #   implementation for how an action plugs in
    ├── fetch/
    │   ├── musicbrainz.py
    │   ├── discogs.py
    │   ├── llm_web_search.py    # Claude/Gemini + web search, artist+title+genre in
    │   ├── audio_model.py       # discogs-maest wrapper (Essentia) - genre/style
    │   └── audio_model_mood.py  # discogs-effnet + mtg_jamendo_moodtheme - mood/theme
    ├── scoring.py                 # weighted noisy-OR - shared by both actions
    ├── lexicon_client.py          # shared Local API client (tracks, tags, writes)
    ├── apply.py                   # writes approved tags via Lexicon Local API - shared
    ├── plan.py                    # Genre/Subgenre: load -> fetch -> score
    ├── mood_plan.py                # Mood/Theme: its own load -> fetch -> score
    ├── mood_apply.py               # Mood/Theme: thin wrapper around apply.py
    ├── review_ui.py                 # NiceGUI screen for BOTH actions, one window,
    │                                 #   one Generate Plan, checkboxes choose which
    │                                 #   action(s) to include
    └── config/
        ├── source_weights.yaml    # Genre/Subgenre tuning
        └── mood_weights.yaml      # Mood/Theme tuning, same shape, separate file
```

## Shared pipeline

`load → fetch → score/plan → review → apply` - same shape as
`billboard_tag.py`'s existing `load / fetch / plan / apply`, extended
with an explicit scoring step and a richer review UI than a flat CSV.

### Choosing what to scan

A plan generation covers one of three pools, picked from the review
screen itself - no terminal flags: the **whole library** (optionally
capped to the first N tracks), the **N most recently added** tracks,
or everything currently in Lexicon's **Incoming** bin. A full-library
run is slow (rate-limited API calls plus local audio inference per
track), so a **Stop** button aborts mid-run - it takes effect after
the current track finishes, not instantly, and keeps whatever was
already planned as a normal, smaller plan rather than discarding it.

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
  these). Same paren/bracket-stripping regex `billboard_tag.py` already
  uses for chart-title matching.
- **Merged featured-artist credits** - `"Nelly Furtado ft Timbaland"`
  is stored as one Artist field, but Discogs indexes releases under the
  primary artist alone. Same `ARTIST_SPLIT` pattern `billboard_tag.py`
  uses for chart-artist matching, applied before search rather than to
  build a fuzzy key.
- **Bootleg/compilation Discogs releases** - a result whose `format`
  says `Compilation` or `Unofficial Release` describes dozens of
  unrelated tracks, not the one asked about, and is skipped rather than
  trusted.

These fix the common, generic cases. Some mismatches are one-off and
not fixable by normalization - e.g. an act catalogued under a
stylized spelling Discogs itself uses (`N*E*R*D`). `billboard_tag.py`'s
`ARTIST_ALIASES` dict is the precedent for handling a specific one-off
by hand if one turns out to matter.

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

### Scoring

```
confidence = 1 - Π(1 - weight_i × score_i)   for each source i that found the tag
```

`weight_i` is configured per-source in `companion-app/config/source_weights.yaml`,
which ships with sensible defaults and is fully editable - this is how a
different DJ retunes the toolkit for their own library without touching
code.

### Review

One rule for the whole screen: **generating a plan never writes
anything** - it's always just a preview, and exactly one action
writes to Lexicon: **"Apply Tags"**, which writes whatever is
checked. Earlier builds had a second write path (an "Apply now"
button for auto-include tags, gated by a separate "Dry run" checkbox)
- collapsed into this one, since a DJ shouldn't need two different
mental models for what's really one action.

"Generate Plan" has checkboxes for which action(s) to include -
Genre/Subgenre, Mood/Theme, or both in the same run - rather than a
DJ needing to run two separate scans against the same tracks just
because the two pipelines live in separate config/plan files
underneath. Selecting both runs them as two sequential phases (Genre
first, then Mood), each with its own live per-track progress; stopping
during the first phase skips the second entirely rather than starting
a new scan after a stop was already requested. Regenerating with only
one of the two checked leaves the other's existing plan untouched in
the review list.

Every candidate, from every tier, is grouped by track - but a track
with both genre and mood candidates doesn't dump them into one
undifferentiated pile: its expansion splits into a "Genre / Subgenre"
sub-group and a "Mood / Theme" sub-group, each independently
confidence-sorted with its own "select all," so the two kinds never
blur together into a wall of unrelated checkboxes. Within each
sub-group: plain checkbox + tag + confidence row, source/notes/links
behind a `⋮` overflow control. A row that already cleared *its own
action's* auto-include confidence bar (genre and mood are tuned
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
a category picker, defaulting to that action's own `new_tag_category`
config if that resolves to a real category - always changeable, and
never pre-checked regardless of confidence. Creating a tag is a bigger
action than adding an existing one, so it always needs an explicit
decision. Clicking "Apply Tags" splits whatever's checked by kind
under the hood and calls each action's own `apply_decisions()` -
potentially both in one click - then reports one combined result.

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

### Reorganize (`reorganize_genres.py`, also in the review screen)

A separate, later step - after a DJ has already applied/created a
batch of genre tags via the review screen, not part of Generate/Apply
itself. Available both as a CLI script and as its own collapsed
"Reorganize Genre Tags" section in `review_ui.py`, below the main
review list - "Check Genre Organization" runs the same report either
way, with a checkbox per proposed move (all checked by default,
uncheck any you disagree with) and a confirmed "Move Checked Tags"
button. Deliberately not triggered automatically by Apply Tags - a
batch category move is its own deliberate action, not a side effect of
an unrelated click. `config/genre_taxonomy.yaml` (Discogs' own
400-style family/subgenre structure - same taxonomy discogs-maest's
classes already use, see its `_meta` block for the full schema) defines
which canonical subgenre belongs to which family; moving a tag changes
its Lexicon category to `Sub-genre - {Family}`, extending the same
category-naming convention already in use for `Sub-genre -
Electronic`/`Sub-genre - Rock`/etc.

"Active" isn't hand-curated in the taxonomy file (it ships with every
family/subgenre `active: false`) - a family/subgenre counts as active
here if at least one existing Lexicon tag's label already matches it,
computed fresh from the live library every run.

Same "never creates a category on the user's behalf" rule as Apply
above - a family with matching tags but no `Sub-genre - {Family}`
category yet is reported, not created; create it by hand in Lexicon
first, then rerun. A subgenre name genuinely shared by more than one
family in Discogs' own taxonomy (`Disco` is both an Electronic style
and a Funk/Soul style, `Electro` is both Electronic and Hip Hop, and
14 more) is reported as ambiguous rather than auto-resolved either
way. Scoped to moving tags between categories only - never renames a
label or merges two tags into one; both are bigger, separate decisions
than "which category does this already-correct tag live in."

Always a dry run - reports what would move, writes nothing - unless
`--apply` is passed:

```
python reorganize_genres.py              # report only
python reorganize_genres.py --apply       # actually move tags
```

## License

AGPL-3.0 (see [LICENSE](LICENSE)), required once the companion app
links against Essentia. See [NOTICE.md](NOTICE.md) for third-party
model attribution (MTG/Essentia pretrained weights, CC BY-NC-SA 4.0).

No monetization planned - free/open distribution to other DJs is the
explicit goal.

## Status

Both the Genre/Subgenre and Mood/Theme pipelines (fetch → score → plan
→ review → apply) run end-to-end entirely from their own review
screen - no terminal needed except to launch one. Genre/Subgenre has
been exercised against a real ~1,770-track Lexicon library, including
real writes; Mood/Theme has been exercised the same way at smaller
scale so far.

- **Charts action**: ported from `billboard_tag.py` as-is, working.
- **Genre/Subgenre fetch sources**: MusicBrainz, Discogs, and the local
  `discogs-maest` audio model are implemented, each with the
  normalization described above. `llm_web_search.py` is a stub, on
  hold over web search API cost.
- **Mood/Theme fetch source**: the local `discogs-effnet` +
  `mtg_jamendo_moodtheme` audio model (see above) - implemented,
  verified against real tracks.
- **Scoring** (`scoring.py`, weighted noisy-OR): implemented, shared
  by both actions - each keeps its own `config/*_weights.yaml`.
- **Plan generation** (`plan.py` / `mood_plan.py`, each its own
  `generate_plan()`): reads tags/tracks over the Lexicon Local API,
  resolves candidates against what already exists, writes an
  auto-include / needs-review / propose-a-new-tag plan. Callable from
  the CLI or directly (used by each action's GUI); a scan-mode picker
  chooses whole library / most recently added / Incoming, with an
  optional Stop mid-run. Genre/Subgenre also has a per-source toggle;
  Mood/Theme doesn't need one yet, with only one source to toggle.
- **Review UI** (`review_ui.py`, one NiceGUI native window -
  `python review_ui.py` is the only command either action needs): the
  whole workflow lives here - one "Generate Plan" with checkboxes for
  which action(s) to include, live per-track progress across both
  phases (never writes anything), each track's candidates split into
  Genre/Subgenre and Mood/Theme sub-groups so the two never blur
  together, global and per-sub-group "Select all", a category picker
  on new-tag rows, source/note/links behind an overflow menu, and the
  one action that writes - "Apply Tags" - applying whatever's
  checked (pre-checked auto-include rows included) via each action's
  own `apply_decisions()`, reporting one combined result.
- **Apply** (`apply.py`, shared; `mood_apply.py` a thin wrapper around
  it with its own plan/log paths): merge-never-replace, same rule as
  `billboard_tag.py`. A tag that already exists is reused rather than
  recreated, even for a "propose a new tag" row.

`llm_web_search.py` remains a stub, on hold over web search API cost
for a full library pass.
