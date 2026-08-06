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
terminal, and generating a plan never writes anything - "Save
Decisions" is the only action that does:

1. Pick what to scan - whole library (optionally capped to the first
   N tracks), the N most recently added, or everything in Incoming -
   and click "Generate Plan". Watch the live per-track progress; "Stop"
   aborts after the current track and keeps whatever was already
   planned.
2. Every proposed tag lands in one grouped-by-track list. Tags
   confident enough to auto-include show up **pre-checked**, marked
   with a green check and a tooltip explaining why - review them like
   anything else, uncheck one if you disagree. Everything else starts
   unchecked; a global "Select all" and a per-track "Select all for
   this track" speed up a big plan.
3. Check what you agree with (or leave the pre-checked ones as they
   are) and hit **"Save Decisions"** - the one action that writes to
   Lexicon. Checked-but-unsaved state survives closing and reopening
   the app, and generating a new plan while anything is still checked
   asks for confirmation before discarding it.

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
