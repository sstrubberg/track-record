# Track Record

A [Lexicon](https://www.lexicondj.com/) DJ plugin + companion app that
enriches a DJ's library with tags Lexicon's built-in "Find Tags" doesn't
reliably provide: accurate, granular genre/subgenre tags with full
source attribution and a review step, plus - later - mood/theme
metadata. Bundles in the existing [Billboard chart-tagging tool](https://github.com/sstrubberg/billboard-tag)
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

- **v1** (this build): Charts action (ported from `billboard_tag.py`) +
  Genre/Subgenre action
- **v2**: Mood/Theme action, same pattern

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
    │   └── audio_model.py       # discogs-maest wrapper (Essentia)
    ├── scoring.py                # weighted noisy-OR, reads config/source_weights.yaml
    ├── lexicon_client.py         # shared Local API client (tracks, tags, writes)
    ├── plan.py                   # load -> fetch -> score, the pipeline itself
    ├── review_ui.py              # NiceGUI screens (native window) - runs the pipeline too
    ├── apply.py                  # writes approved tags via Lexicon Local API
    └── config/
        └── source_weights.yaml
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

### Genre/Subgenre fetch sources

Queried independently per track, never merged blindly:

- **MusicBrainz** - genre via MBID lookup
- **Discogs** - style/genre via public API, artist+title search
- **LLM web-search classification** - given artist, title, and current
  genre, prompted to return every defensible subgenre tag with its
  source URL and the model's own reported confidence, not one "best" answer
- **discogs-maest** (Essentia/MTG, local audio model) - runs directly on
  the audio file, no internet dependency, so it's the only source that
  returns anything for untraceable tracks (transition edits, mashups,
  bootlegs, DJ tools with no web presence)

No cap on candidate tags per track.

`discogs-maest`'s classes are `Genre---Style` pairs (e.g.
`"Hip Hop---RnB/Swing"`); both halves become separate candidates, so a
genre-level tag can benefit from cross-source agreement the same way a
style-level one does.

#### Matching a DJ library's title/artist strings against public catalogs

DJ libraries routinely spell things in ways MusicBrainz/Discogs won't
match verbatim - both fetch sources normalize before searching, then
fall back to the untouched original if the normalized version finds
nothing:

- **Edit/version suffixes** - `"Hollaback Girl (Intro Clean)"`,
  `"... (MM Edit)"` - stripped from the title before search (a survey
  of one real library found 99% of tracks carried at least one of
  these). Same paren/bracket-stripping regex `billboard_tag.py` already
  uses for chart-title matching.
- **Merged featured-artist credits** - `"Nelly Furtado ft Timbaland"`
  is stored as one Artist field, but both catalogs index releases under
  the primary artist alone. Same `ARTIST_SPLIT` pattern
  `billboard_tag.py` uses for chart-artist matching, applied before
  search rather than to build a fuzzy key.
- **Bootleg/compilation Discogs releases** - a result whose `format`
  says `Compilation` or `Unofficial Release` describes dozens of
  unrelated tracks, not the one asked about, and is skipped rather than
  trusted.

These fix the common, generic cases. Some mismatches are one-off and
not fixable by normalization - e.g. an act catalogued under a
stylized spelling Discogs itself uses (`N*E*R*D`), or a MusicBrainz
recording that matches fine but has no genre/tag data attached at any
level. `billboard_tag.py`'s `ARTIST_ALIASES` dict is the precedent for
handling a specific one-off by hand if one turns out to matter.

### Scoring

```
confidence = 1 - Π(1 - weight_i × score_i)   for each source i that found the tag
```

`weight_i` is configured per-source in `companion-app/config/source_weights.yaml`,
which ships with sensible defaults and is fully editable - this is how a
different DJ retunes the toolkit for their own library without touching
code.

### Review

Everything that doesn't clear the auto-include confidence bar appears
in the review screen, grouped by track, with a plain checkbox + tag +
confidence row - source, notes, and links live behind a `⋮` overflow
control. A global "Select all" and a per-track "Select all for this
track" checkbox speed up working through a large plan; both just
drive the same per-row checkboxes "Save Decisions" already reads.

A row proposing a tag that doesn't exist in the library yet also gets
a category picker, defaulting to `new_tag_category` from
`source_weights.yaml` if that resolves to a real category - always
changeable, and always review-gated. Creating a tag is a bigger action
than adding an existing one, so this never happens automatically
regardless of confidence.

### Auto-include and the dry-run gate

A tag that *does* clear the auto-include bar skips the checkbox screen
by design - but it still shouldn't be written silently. A **"Dry
run"** checkbox, on by default, keeps a plan generation a pure
preview: the auto-include tier is shown as a pending list (tag names
and confidence, grouped by track, "N tag(s) cleared the auto-include
confidence bar (X%+)"), and nothing is written until "Apply now" is
clicked deliberately. Unchecking dry run applies the auto-include tier
immediately when generation finishes instead, for a routine run where
that's wanted.

Either way, exactly what was (or would be) written is always spelled
out, never applied silently. A pending set also survives closing and
reopening the app - it's restored from the plan already on disk, not
just held in memory - and generating a new plan over a still-pending
set asks for confirmation first rather than discarding it.

### Apply

Merge, never replace: reads the track's live tag array and appends,
since Lexicon's `tags` field is flat and a bare overwrite wipes
unrelated tags. Only writes to tag categories that already exist in
Lexicon; never creates a new category on the user's behalf.

## License

AGPL-3.0 (see [LICENSE](LICENSE)), required once the companion app
links against Essentia. See [NOTICE.md](NOTICE.md) for third-party
model attribution (MTG/Essentia pretrained weights, CC BY-NC-SA 4.0).

No monetization planned - free/open distribution to other DJs is the
explicit goal.

## Status

The full Genre/Subgenre pipeline (fetch → score → plan → review →
apply) runs end-to-end entirely from `review_ui.py` - no terminal
needed except to launch it once - and has been exercised against a
real ~1,770-track Lexicon library.

- **Charts action**: ported from `billboard_tag.py` as-is, working.
- **Genre/Subgenre fetch sources**: MusicBrainz, Discogs, and the local
  `discogs-maest` audio model are implemented, each with the
  normalization described above. `llm_web_search.py` is a stub, on
  hold over web search API cost.
- **Scoring**: implemented (`scoring.py`, weighted noisy-OR).
- **Plan generation** (`plan.py`'s `generate_plan()`): reads
  tags/tracks over the Lexicon Local API, resolves candidates against
  what already exists, writes an auto-include / needs-review /
  propose-a-new-tag plan. Callable from the CLI or directly (used by
  the GUI); a scan-mode picker chooses whole library / most recently
  added / Incoming, with an optional Stop mid-run.
- **Review UI** (`review_ui.py`, NiceGUI native window): the whole
  workflow lives here - "Generate Plan" with live per-track progress,
  a dry-run gate in front of auto-include writes, global and per-track
  "Select all", a category picker on new-tag rows, source/note/links
  behind an overflow menu, "Save Decisions".
- **Apply** (`apply.py`): merge-never-replace, same rule as
  `billboard_tag.py`. Auto-include rows go through the dry-run gate
  above; review/create rows go through `apply_decisions()` once
  checked and saved.

`llm_web_search.py` remains a stub, on hold over web search API cost
for a full library pass.
