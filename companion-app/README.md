# companion-app

Separate local Python process - see the top-level README for why
(Lexicon plugins can't spawn processes or load native modules, so this
is where Essentia and everything else actually runs).

```
pip install -r requirements.txt
```

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

## Review UI (Genre/Subgenre and Mood/Theme)

```
python review_ui.py
```

One native window, one command, one "Generate Plan" for both actions.
Nothing else needs the terminal, and generating a plan never writes
anything - "Apply Tags" is the only action that does:

1. Pick what to scan - whole library (optionally capped to the first
   N tracks), the N most recently added, or everything in Incoming -
   and which action(s) to include via the "Tag with:" checkboxes
   (Genre/Subgenre, Mood/Theme, or both; both on by default). With
   Genre/Subgenre checked, a checkbox per fetch source (Discogs, and
   two independent audio models - discogs-maest and genre_discogs400)
   also appears, letting you turn any off for this run entirely - e.g.
   skip both audio models, by far the slowest part of a run, for a
   quick metadata-only pass. (Mood/Theme
   has no such picker - there's only one source to toggle.) Click
   "Generate Plan". With both actions checked, they run as two
   sequential phases (Genre first, then Mood), each with its own live
   per-track progress; "Stop" aborts after the current track and keeps
   whatever was already planned, skipping a not-yet-started second
   phase entirely rather than starting it after a stop. A capped
   "whole library" run remembers where it left off, per action - a
   caption under the scan controls reads "X of Y tracks scanned" once
   a cursor exists, with its own "Reset" (confirmed first) to
   deliberately start that action's whole-library scanning over.
   Nothing to save or show for "recent"/"incoming" - see the top-level
   README's "Choosing what to scan" for the full rationale.
2. Every proposed tag lands in a grouped-by-track list - but a track
   with both genre and mood candidates doesn't dump them into one
   pile: its entry splits into a "Genre / Subgenre" sub-group and a
   "Mood / Theme" sub-group, each with its own confidence-sorted rows
   and its own "select all." Tags confident enough to auto-include
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
   "(Intro Clean)" / "(Quick Hit Clean)") are detected automatically -
   a "Copy checked genre tags to '\<sibling title>'" button appears next
   to a track's Genre/Subgenre "select all" when one's found. It's a
   one-time copy, not a live link: click it after checking tags on one
   edit to check the same tags on the named sibling(s), only where they
   already proposed that exact tag too - nothing stays bound
   afterward, so unchecking something later never cascades. Mood/Theme
   has no such button, since real testing found mood genuinely differs
   between edits in a way genre doesn't; see the top-level README's
   "Review" section for the full rationale.
3. Check what you agree with (or leave the pre-checked ones as they
   are) and hit **"Apply Tags"** - the one action that writes to
   Lexicon, splitting whatever's checked by kind under the hood and
   reporting one combined result. Checked-but-unsaved state survives
   closing and reopening the app, and generating a new plan while
   anything is still checked asks for confirmation before discarding
   it.

If you'd rather drive either action from scripts (e.g. a cron job that
generates a plan overnight for review in the morning), `plan.py` /
`apply.py` (Genre/Subgenre) and `mood_plan.py` / `mood_apply.py`
(Mood/Theme) are still plain CLIs:

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
```

## Reorganizing genre tags into families

Available in `review_ui.py` as its own view - a persistent "Reorganize
Genre Tags →" button at the top switches over to it, "← Back to
Tagging" switches back. A genuinely separate screen sharing the same
session, not a section stacked below Apply Tags, and reachable any
time - not gated behind having just applied anything this session.
The first time you ever switch over, it runs "Check Genre
Organization" for you automatically; save() also refreshes its data
quietly in the background the moment a genre tag lands in Lexicon, so
switching over after applying a batch already shows current results.
Reports,
in this order, what's blocked on a missing category, what's ambiguous,
and what would move (grouped by target category, each row showing
where it's coming from and a "→ Family" chip for where it's going,
each with its own checkbox, all checked by default) - category
creation and ambiguous resolution both come first since they unblock
more of "would move." "Move Checked Tags" applies just the checked
ones, behind a confirmation dialog. Same CLI also still works
standalone for the report/move part:

```
python reorganize_genres.py              # report only, writes nothing
python reorganize_genres.py --apply       # actually move tags
```

A later, separate step - after applying/creating a batch of genre tags,
not triggered automatically by Apply Tags. Moves each existing tag
matching `config/genre_taxonomy.yaml` (Discogs' 400-style
family/subgenre list) into a `Sub-genre - {Family}` category. A missing
category can be created right there too - "Create Missing Categories"
previews the exact names, confirms once, then creates them empty
(review-screen only for now, no `--create-categories` CLI flag yet). Never renames a
tag or merges tags either way. Ambiguous tags (a style name that
genuinely belongs to more than one Discogs family) get clickable
family choices in the same section instead of needing a trip to
Lexicon's own UI to resolve by hand. See the top-level README's
"Reorganize" section for the full rationale, including why creating an
empty category is safe even though this project is otherwise strict
about never creating one on a DJ's behalf.

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
