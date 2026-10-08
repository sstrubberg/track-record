# companion-app

Separate local Python process - see the top-level README for why
(Lexicon plugins can't spawn processes or load native modules, so this
is where Essentia and everything else actually runs).

Install into a virtual environment (from the repo root, one time):

```
python3 -m venv .venv
source .venv/bin/activate
cd companion-app
pip install -r requirements.txt
```

macOS only ships `python3`, not `python`, so every command below that
says `python` assumes this environment is active - run
`source ../.venv/bin/activate` from this directory first in any new
terminal window (the prompt shows `(.venv)` once it's on).

## Before running anything

**Lexicon has to already be open**, with its Local API switched on -
every action here (reading the library, fetching categories, writing
tags) goes through `http://localhost:48624/v1`, and nothing's
listening there unless Lexicon itself is running. This is a one-time
toggle, off by default: in Lexicon, **Settings → Integrations → Local
API**, switch on **"Local API Enabled"**. No token or key needed on
Track Record's side - the connection is bare `localhost`, nothing to
put in `.env` for it.

## Environment variables

```
cp .env.example .env
```

Then fill in `.env` with your own credentials - it's gitignored, so it
never gets committed:

| Variable          | Used by                | Get one from |
|-------------------|-------------------------|---------------|
| `DISCOGS_TOKEN`   | `fetch/discogs.py`      | https://www.discogs.com/settings/developers (personal access token) |
| `ANTHROPIC_API_KEY` | `fetch/llm_web_search.py` (not yet built) | https://console.anthropic.com/ |

## Review UI (Genre/Subgenre, Mood/Theme, and Charts)

```
source ../.venv/bin/activate   # skip if the prompt already shows (.venv)
python track_record.py
```

(a thin, brand-named entry point - `track_record.py` just calls
`review_ui.main()`; `review_ui.py` is the actual screen, named for what
it does like every other module here, since a DJ only ever sees the
command and the window title, never the filename)

One native window, one command, one "Generate Plan" for all three
actions. Nothing else needs the terminal, and generating a plan never
writes anything - "Apply Tags" is the only action that does:

1. Pick what to scan - whole library (optionally capped to the first
   N tracks), the N most recently added, everything in Incoming, or a
   single track (search by artist/title - each DJ edit of a song is
   its own separate entry, since edit info already lives in the title
   itself) for spot-checking one song without waiting on a real scan -
   and which action(s) to include via the "Tag with:" checkboxes
   (Genre/Subgenre, Mood/Theme, Charts, any combination; all three on
   by default). With Genre/Subgenre checked, a checkbox per fetch
   source (Discogs, and two independent audio models - discogs-maest
   and genre_discogs400) also appears, letting you turn any off for
   this run entirely - e.g. skip both audio models, by far the slowest
   part of a run, for a quick metadata-only pass. (Mood/Theme and
   Charts have no such picker - each has only one source to toggle.)
   Click "Generate Plan". With more than one action checked, they run
   as sequential phases (Genre, then Mood, then Charts), each with its
   own live per-track progress, plus a countdown ("~2m 15s left") once
   at least one track in the current phase has finished - extrapolated
   from that phase's own average time per track so far, re-anchored
   off the phase's start time and recomputed continuously rather than
   only when a new track finishes, so it actually ticks down smoothly.
   Only ever covers the phase currently running, not the whole
   multi-phase run - Genre/Subgenre, Mood/Theme, and Charts cost very
   different amounts per track, so a phase not yet started has nothing
   real to extrapolate its own pace from yet. "Stop" aborts after the
   current track and keeps whatever was already planned, skipping
   every not-yet-started phase entirely rather than starting them
   after a stop. A
   capped "whole library" run remembers where it left off, per action
   except Charts - a caption under the scan controls reads "X of Y
   tracks scanned" once a cursor exists, with its own "Reset"
   (confirmed first) to deliberately start that action's whole-library
   scanning over. Charts always scans fresh regardless of scan mode -
   matching against its chart cache is an in-memory lookup with no
   network call or audio inference per track, so there's nothing
   costly about redoing it, and it gets no cursor or caption of its
   own. Nothing to save or show for "recent"/"incoming" either - see
   the top-level README's "Choosing what to scan" for the full
   rationale.
2. Every proposed tag lands in a grouped-by-track list - but a track
   with candidates from more than one action doesn't dump them into
   one pile: its entry splits into a "Genre / Subgenre" sub-group, a
   "Mood / Theme" sub-group, and/or a "Charts" sub-group (only
   whichever actually have candidates for that track), each with its
   own confidence-sorted rows and its own "select all." A track that
   has genre/mood candidates but nothing from Charts gets a small note
   right in its collapsed caption saying why - "no chart match found,"
   a mashup/transition/blend title got skipped, or it already carries
   every chart tag the match would have proposed - since most tracks
   genuinely never charted at all, and that's expected, not a sign
   Charts isn't working. Mood/Theme gets the same treatment on the rare
   track where every one of its 56 mood/theme classes falls under the
   model's own confidence floor - "no mood/theme guess cleared the
   model's own confidence floor" rather than the sub-group just not
   showing up. Tags confident enough to auto-include
   show up **pre-checked**, marked with a green check and a tooltip
   explaining why - review them like anything else, uncheck one if you
   disagree. Everything else starts unchecked; a global "Select all"
   on top of each sub-group's own speeds up a big plan. Expect fewer
   pre-checked rows in the Mood/Theme sub-groups than Genre/Subgenre
   tends to produce - mood/theme is a genuinely noisier task for a
   model to call from audio alone (see the top-level README for the
   real numbers), so most of what shows up there will need your own
   judgment rather than clearing the auto-include bar on its own.
   A large plan (thousands of tracks) is paginated - 50 tracks per
   page by default, configurable to 25/100/200 via the "tracks per
   page" picker above the list - and each track's rows aren't built
   until you actually expand it, so the screen stays responsive
   regardless of plan size. "Select all" and Apply Tags both act
   across the *whole* plan, not just the visible page, so checking a
   tag on page 1 and a different one on page 3 both make it into the
   same Apply Tags click. Different DJ edits of the same song (e.g.
   "(Intro Clean)" / "(Quick Hit Clean)") are detected automatically by
   artist + title, ignoring edit/version suffixes and matching on
   primary artist so a feature credit folded into one edit's Artist
   field doesn't break the match. A remix only groups with other DJ
   edits of that *same* remix, never with the plain version or a
   different remix of the song - a remix can genuinely be a different
   genre than the original. A "Copy checked genre tags from
   '\<sibling title>'" button appears next to a track's Genre/Subgenre
   "select all" when one's found. It's a one-time copy, not a live
   link: finish tagging one edit, move on to its sibling, and click the
   button there to pull whatever's checked over, only where this track
   already proposed that exact tag too - nothing stays bound
   afterward, so unchecking something later never cascades. Mood/Theme
   has no such button, since real testing found mood genuinely differs
   between edits in a way genre doesn't; see the top-level README's
   "Review" section for the full rationale.
3. Check what you agree with (or leave the pre-checked ones as they
   are) and hit **"Apply Tags"** - the one action that writes to
   Lexicon, splitting whatever's checked by kind under the hood and
   reporting one combined result. Applying writes the updated plan back
   to its own JSON file immediately, not just to memory - a resolved row
   (written, or found already on the track) is gone for good the moment
   it's applied, not just for the rest of this window's session. That
   matters because the window itself doesn't always survive a break:
   stepping away for a while can disconnect the native window's own
   connection to its background process, and reconnecting past a point
   forces a full reload - everything not yet written to a file (which
   scan mode is picked, an in-progress but unapplied check) resets, the
   same as quitting and relaunching would. Checked-but-unsaved state
   otherwise survives closing and reopening the app (it's the plan
   file's own contents, read back in), and generating a new plan while
   anything is still checked asks for confirmation before discarding
   it.

If you'd rather drive any action from scripts (e.g. a cron job that
generates a plan overnight for review in the morning), `plan.py` /
`apply.py` (Genre/Subgenre), `mood_plan.py` / `mood_apply.py`
(Mood/Theme), and `charts_plan.py` / `charts_apply.py` (Charts) are
still plain CLIs:

```
python plan.py --limit 20                       # try it on the first 20 tracks
python plan.py                                   # the whole library
python plan.py --mode recent                     # the 20 most recently added
python plan.py --mode incoming                   # everything in Incoming
python plan.py --sources audio_model,audio_model_genre_effnet  # skip Discogs
python apply.py                                  # applies the plan's auto-include rows immediately

python mood_plan.py --limit 20        # same flags, Mood/Theme's own plan/log files
python mood_plan.py --mode recent
python mood_apply.py

python charts_plan.py --limit 20      # same flags again, minus --resume/--reset-progress -
python charts_plan.py --mode recent   #   Charts has no resumable scan position (see above)
python charts_apply.py
```

## Settings

A gear icon in the header (next to the notification bell) opens a
dialog for retuning `config/source_weights.yaml` (Genre/Subgenre),
`config/mood_weights.yaml` (Mood/Theme), and `config/charts_weights.yaml`
(Charts) without hand-editing YAML: each fetch source's weight, the
auto-include thresholds (`min_agreeing_sources`/`min_confidence`),
`low_confidence_threshold`, and `new_tag_category` (a searchable
dropdown of your real Lexicon Custom Tag categories, since that value
only ever matters as a label Track Record looks up by name). Each file
has its own "Save," and a save takes effect immediately - no restart
needed, next "Generate Plan" already uses it. Written via
`config_editor.py`'s round-trip YAML (`ruamel.yaml`, not the plain
`pyyaml` used for reading elsewhere) specifically so saving a value
never strips the explanatory comments every one of these files is full
of - editing by hand in a text editor still works exactly as before
and remains fully supported for anything this dialog doesn't expose.

The same dialog also has a **Chart Cache** card - "Update Chart Cache"
re-ingests billboard-tag's bulk chart datasets (seconds); "Fetch from
Billboard.com" scrapes the charts those datasets don't cover directly
from the site. Confirming a fetch shows a real time estimate first -
by default it only checks the last two years (a routine catch-up run
this often finds real new chart weeks recent anyway), with a "Full
historical re-fetch instead" option for the rare case that actually
needs the full 1958-forward range (e.g. right after adding a brand-new
chart to `chart_map.json` that's never been fetched at all - see
`charts_plan.py`'s `estimate_fetch()`/`RECENT_FETCH_YEARS_BACK` for
the full reasoning, including a real quirk in billboard_tag.py's own
progress-tracking: the full range doesn't actually get cheaper just
because it was already fetched once before, since its own resume
logic is sensitive to which exact day you happen to run it on).
See "Setting up Charts" below for the one-time setup this depends on.

## Setting up Charts

Charts needs two things this repo doesn't ship ready-made for a
different library, both one-time, both from inside `charts/`:

1. **A real `chart_map.json`** - maps *your* Lexicon tag labels to
   Billboard chart slugs. The one committed here is generated for this
   project's own library; a different DJ's tag names won't match it.
   Generate your own:
   ```
   cd charts
   python billboard_tag.py init          # proposes a mapping, writes nothing
   python billboard_tag.py init --yes    # writes chart_map.json once you're happy with it
   ```
   **Review what it proposes before trusting it** - a loose fuzzy
   match can confidently claim an existing broad genre tag (e.g. a
   generic "Rap" or "Rock" tag) as if it were a specific chart's own
   tag, which would conflate chart-appearance tagging with genre
   tagging on the same tag going forward. Anything you don't want
   mapped, delete that line from the written file (or don't map it at
   all, and add it by hand only for the charts you're sure about).
   Without this file, Charts silently falls back to
   `DEFAULT_CHART_MAP` - the original billboard-tag author's own tag
   names, almost certainly wrong for your library.
2. **A chart cache** (`billboard_cache.json`) - what Charts actually
   matches tracks against. Build it via Settings' "Chart Cache" card
   (see above) or from the terminal:
   ```
   python billboard_tag.py load    # bulk chart datasets, seconds
   python billboard_tag.py fetch   # scrapes Billboard.com for the rest, can take hours
   ```
   `load` alone already covers seven major charts going back decades;
   `fetch` only gap-fills what `load` doesn't. Neither needs to be
   re-run often - Billboard's charted history doesn't change underfoot
   the way, say, an audio model version does. When you do want fresh
   weeks, prefer Settings' "Fetch from Billboard.com" over running
   `python billboard_tag.py fetch` bare from a terminal - the Settings
   button caps the date range to the last two years by default
   (see above), while the bare CLI command always attempts the full
   1958-forward range.

## Trying a fetch source directly

Each module under `fetch/` is runnable on its own for a quick check
against a real track, before any scoring/review/apply is wired up:

```
python fetch/musicbrainz.py "Frankie Knuckles" "Your Love"
python fetch/discogs.py "Frankie Knuckles" "Your Love"
python fetch/audio_model.py "/path/to/track.wav"
python fetch/audio_model_mood.py "/path/to/track.wav"
python fetch/audio_model_genre_effnet.py "/path/to/track.wav"
```

`audio_model.py` downloads the discogs-maest model weights (~330 MB,
CC BY-NC-SA 4.0, see [NOTICE.md](../NOTICE.md)) into `models/` on first
use - gitignored, not part of this repo. `audio_model_mood.py` does
the same for its own two much smaller model files (~21 MB combined).
`audio_model_genre_effnet.py` shares one of those two files (the
discogs-effnet embedding extractor) rather than downloading its own
copy - only its ~2 MB classification head is new if Mood/Theme's
weights are already present, ~20 MB combined otherwise. Any of the
audio-model scripts: the audio file needs to be at least ~30 seconds
long; shorter clips raise `input signal is too short`.

## Checking for newer audio models

Once a model file is downloaded it's cached forever - none of the
`fetch/audio_model*.py` scripts ever check for a newer version on
their own, so nothing changes underfoot between runs. The Settings
dialog's "Audio models" card is the normal way to check and update:
opening it doesn't touch the network (versions shown are read from
what's already downloaded), but "Check for Updates" does, and any
model with a newer version published gets its own "Update" button -
confirms first (it's a real download, a source-file change, and needs
a restart to take effect, not a config value applied instantly), then
runs in the background.

`model_versions.py` is the same thing from a terminal, for scripting
or when the GUI isn't running:

```
python model_versions.py              # report only, changes nothing
python model_versions.py --apply      # download + switch to newer ones
```

Both read Essentia's own [models.html](https://essentia.upf.edu/models.html)
listing for each model this project uses (version numbers are
Essentia's own, baked into the filename - not something this project
assigns), download the new version, edit the matching
`fetch/audio_model*.py` file's URL/path constants to point at it, and
delete the old cached file. **Restart Track Record afterward** -
editing the source file doesn't change what's already loaded in the
running process's memory.
