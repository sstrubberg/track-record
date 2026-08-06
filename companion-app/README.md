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

## Genre/Subgenre action

```
python review_ui.py
```

Opens the whole workflow in one native window. Nothing else needs the
terminal:

1. Pick what to scan - whole library (optionally capped to the first
   N tracks), the N most recently added, or everything in Incoming -
   and click "Generate Plan". Watch the live per-track progress; "Stop"
   aborts after the current track and keeps whatever was already
   planned.
2. **Dry run** is checked by default, so nothing gets written yet.
   Once generation finishes, any tag confident enough to auto-include
   shows up as a pending list (tag names + confidence, grouped by
   track) with an "Apply now" button - click it when you're ready, or
   uncheck "Dry run" beforehand to have those write immediately next
   time. A pending set survives closing and reopening the app, and
   generating a new plan over one still pending asks for confirmation
   first.
3. Everything else needing a decision is grouped by track with a
   checkbox + tag + confidence per row - a global "Select all" and a
   per-track "Select all for this track" speed up a big plan. Check
   what you agree with and hit "Save Decisions".

If you'd rather drive it from scripts (e.g. a cron job that generates
a plan overnight for review in the morning), `plan.py` and `apply.py`
are still plain CLIs:

```
python plan.py --limit 20            # try it on the first 20 tracks
python plan.py                        # the whole library
python plan.py --mode recent          # the 20 most recently added
python plan.py --mode incoming        # everything in Incoming
python apply.py                       # applies the plan's auto-include rows immediately
```

## Trying a fetch source directly

Each module under `fetch/` is runnable on its own for a quick check
against a real track, before any scoring/review/apply is wired up:

```
python fetch/musicbrainz.py "Frankie Knuckles" "Your Love"
python fetch/discogs.py "Frankie Knuckles" "Your Love"
python fetch/audio_model.py "/path/to/track.wav"
```

`audio_model.py` downloads the discogs-maest model weights (~330 MB,
CC BY-NC-SA 4.0, see [NOTICE.md](../NOTICE.md)) into `models/` on first
use - gitignored, not part of this repo. The audio file needs to be at
least ~30 seconds long; shorter clips raise `input signal is too short`.
