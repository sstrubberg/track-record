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
    ├── review_ui.py              # NiceGUI screens (native window)
    ├── apply.py                  # writes approved tags via Lexicon Local API
    └── config/
        └── source_weights.yaml
```

## Shared pipeline

`load → fetch → score/plan → review → apply` - same shape as
`billboard_tag.py`'s existing `load / fetch / plan / apply`, extended
with an explicit scoring step and a richer review UI than a flat CSV.

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

### Scoring

```
confidence = 1 - Π(1 - weight_i × score_i)   for each source i that found the tag
```

`weight_i` is configured per-source in `companion-app/config/source_weights.yaml`,
which ships with sensible defaults and is fully editable - this is how a
different DJ retunes the toolkit for their own library without touching
code.

### Review

Any tag clearing the auto-include bar (2+ agreeing sources, or an
equivalent combined score) is written straight to the applied log and
never shown for approval. Everything else appears in the review screen,
grouped by track, with a plain checkbox + tag + confidence row by
default - source, notes, and links live behind a `⋮` overflow control.

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

- **Charts action**: ported from `billboard_tag.py` as-is, working.
- **Genre/Subgenre fetch sources**: MusicBrainz, Discogs, and the local
  `discogs-maest` audio model are implemented and tested against a real
  Lexicon library. `llm_web_search.py` is a stub, on hold over web
  search API cost.
- **Scoring**: implemented (`scoring.py`, weighted noisy-OR).
- **Load → fetch → score/plan**: implemented (`plan.py`'s
  `generate_plan()`), tested end-to-end against a real library over the
  Lexicon Local API - reads tags/tracks, resolves candidate tags
  against what already exists, and writes a plan of auto-include /
  needs-review / propose-a-new-tag rows. Callable from the CLI or
  directly (used by the GUI below); a new tag's category is only a
  suggested default here - always changeable, and always review-gated,
  never auto-included.
- **Review UI**: implemented (`review_ui.py`, NiceGUI native window) -
  the whole workflow lives here now, not just review: a "Generate Plan"
  button (with an optional tracks-to-scan limit) runs the pipeline
  in-process with a live per-track progress bar, no terminal needed.
  Tracks grouped in expandable sections, checkbox + tag + confidence
  bar per row, low-confidence flag, a category picker on new-tag rows,
  source/note/links behind an overflow menu, "Save Decisions" button.
- **Apply**: implemented (`apply.py`) - applies the plan's auto rows
  immediately; review_ui.py calls into it for whatever a DJ checks.
  Merge-never-replace, same rule as `billboard_tag.py`.

The full Genre/Subgenre pipeline (fetch → score → plan → review →
apply) exists end-to-end, runs entirely from `review_ui.py`, and has
been exercised against a real Lexicon library. `llm_web_search.py`
remains a stub, on hold over web search API cost for a full library
pass.
