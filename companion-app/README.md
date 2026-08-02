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

## Trying a fetch source directly

Each module under `fetch/` is runnable on its own for a quick check
against a real track, before any scoring/review/apply is wired up:

```
python fetch/musicbrainz.py "Frankie Knuckles" "Your Love"
python fetch/discogs.py "Frankie Knuckles" "Your Love"
```
