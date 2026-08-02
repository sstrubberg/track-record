#!/usr/bin/env python3
"""
billboard_tag.py — populate your existing Lexicon "Charts" tags from Billboard.

Nothing writes until you run `apply` without --dry-run and type 'yes'.

    python billboard_tag.py tags            # list your tags + ids
    python billboard_tag.py charts          # verify which chart slugs are real
    python billboard_tag.py fetch           # scrape -> billboard_cache.json
    python billboard_tag.py plan            # DRY RUN -> billboard_plan.csv
    python billboard_tag.py apply --dry-run # show every write, make none
    python billboard_tag.py apply           # write

Requires: pip install billboard.py rapidfuzz requests
"""

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

VERSION = "2026-07-31.1"   # printed at startup so you can tell at a
                           # glance which copy you are running
LEXICON = "http://localhost:48624/v1"
# Written with sorted keys and stable formatting on purpose: the cache is
# committed to git, and deterministic output is what lets git delta-compress
# week-over-week changes down to kilobytes. Do NOT gzip it before committing -
# that defeats both git's own compression and its delta storage, and measured
# ~11x LARGER repo growth.
CACHE = Path("billboard_cache.json")
PLAN = Path("billboard_plan.csv")

# Your Lexicon tag label  ->  billboard.com chart slug.
# Run `charts` to verify these resolve before trusting them.
# Comment out any chart you don't care about — fewer charts, faster scrape.
DEFAULT_CHART_MAP = {
    "US Hot 100":                   "hot-100",
    # Chart ran Jun 1959 - Aug 1985, went dark, resumed Dec 1992 - current.
    # "bubbling-under-hot-100" (no "-singles") was the wrong slug - that one
    # has no archive at all. This one has a real archive back to 1992, but
    # nothing before that: the 1985 gap is real (Billboard stopped
    # publishing it), and 1959-1985 exists only in print (Joel Whitburn's
    # reference book), not anywhere scrapeable.
    "US Bubbling Under Hot 100":    "bubbling-under-hot-100-singles",
    "US Pop Airplay":               "pop-songs",
    "US Adult Pop Airplay":         "adult-pop-songs",
    "US Adult Contemporary":        "adult-contemporary",
    "US Rhythmic Airplay":          "rhythmic-40",
    "US Dance Club":                "dance-club-play-songs",   # frozen at 2020
    # Launched Aug 16 2003 - that's the chart's real age, not an archive
    # limit. "dance-mix-show-airplay" was the wrong slug (no archive at all).
    "US Dance/Mix Show Airplay":    "hot-dance-airplay",
    # Dataset covers 2013-2018. Scraping can't extend it (no archive), so
    # this chart is frozen at 2018 - do not pass it to `fetch`.
    "US Hot Dance/Electronic":      "hot-dance-electronic-songs",
    "US Hot R&B/Hip-hop":           "r-b-hip-hop-songs",
    # Launched Dec 5 1992 - Billboard's R&B chart converted to BDS
    # airplay-only monitoring that exact date, so there's no archive before
    # it because the chart didn't exist in this form. 70s/80s R&B is still
    # covered - by "US Hot R&B/Hip-hop" above, the older combined
    # sales+airplay chart, dataset-backed to 1958. "r-and-b-hip-hop-airplay"
    # (no "hot-" prefix) was the wrong slug - that's the ARTIST chart, a
    # different data shape entirely, not songs.
    "US R&B/Hip-hop Airplay":       "hot-r-and-b-hip-hop-airplay",
    "US Hot Rap":                   "rap-song",
    "US Hot Country":               "country-songs",
    # Dataset covers 1986-2018. Scraping can't extend it (no archive), so
    # this chart is frozen at 2018 - do not pass it to `fetch`.
    "US Hot Latin Songs":           "hot-latin-songs",
    "US Latin Pop Airplay":         "latin-pop-airplay",
    "US Tropical Airplay":          "latin-tropical-airplay",
    "US Hot Dance/Pop":             "hot-dance-pop-songs",  # launched Jan 2025;
                                              # only 2025+ exists, and that is
                                              # the chart's whole lifetime
    "US Dance Single Sales":        "hot-dance-singles-sales",  # archive ends ~2013
    "US Hot Rock & Alternative":    "hot-rock-songs",
    "US Rock & Alternative Airplay": "rock-airplay",
    # Launched Sep 10 1988 (as "Modern Rock Tracks") - that's the chart's
    # real age, not an archive limit; alternative rock wasn't a chart
    # category before then. "alternative-songs" was the wrong slug (no
    # archive at all).
    "US Alternative Airplay":       "alternative-airplay",
    "US Adult Alternative Airplay": "triple-a",
    # Launched Mar 21 1981 (as "Top Tracks") - real age, not an archive
    # limit. "mainstream-rock-songs" was the wrong slug (no archive at all).
    "US Mainstream Rock":           "hot-mainstream-rock-tracks",
    # NO ARCHIVE (verified 2026-07): serves the current week for any date
    #"US Smooth Jazz Airplay":       "smooth-jazz-songs",

    # RENAMES — same chart, older name. Under the current-name rule these tags
    # go dormant; their songs get tagged with the modern label above instead:
    #   US Hot Soul Singles   (Jul 1973 - Jun 1982)
    #   US Hot Black Singles  (Jun 1982 - Oct 1990)
    #   US R&B Singles        (Oct 1990 - Jan 1999)
    #       -> all three become "US Hot R&B/Hip-hop" (current since Apr 2005)
    #   US Easy Listening        -> becomes "US Adult Contemporary"
    #   US Hot Disco Singles     -> becomes "US Dance Club", but see note: the
    #       chart SPLIT in Mar 1985 into Club Play and 12-inch Singles Sales,
    #       so pre-1985 disco entries are ancestors of two modern charts.
    #
    # ACTIVE, NEW CHART — split off Hot Dance/Electronic on Jan 18 2025.
    # Now mapped to hot-dance-pop-songs.
    #
    # DISCONTINUED, no successor — keep these tags, but Billboard serves no
    # archive under the dead slugs, so they stay manual:
    #   US Pop 100 (2005-2009), US Hot Crossover, US Hot Singles Sales
    #
    # SUSPECT — these return no chart date and truncated entry counts, which
    # may mean the page is Pro-gated and only a teaser is public. If they
    # ignore the date parameter they're useless; verify before scraping:
    #   adult-r-and-b-songs, hot-dance-electronic-songs, hot-latin-songs,
    #   smooth-jazz-songs, hot-dance-singles-sales
    #
    # NOT a real Billboard chart - "Rock & Metal" is an Official Charts
    # Company (UK) name; the closest US equivalents are Mainstream Rock,
    # Hot Rock & Alternative, and Rock & Alternative Airplay, all above:
    #   US Rock & Metal
    #
    # NOT Billboard — Official Charts Company:
    #   UK Club, UK Dance, UK Hip-Hop/R&B, UK Indie, UK Rock & Metal
    #
    # Albums chart, needs a different join:
    #   US Billboard 200
}

# Billboard's own name for each slug. Used by `init` to propose a mapping
# against whatever tags a user already has.
KNOWN_CHARTS = {
    "hot-100":                    "Hot 100",
    "r-b-hip-hop-songs":          "Hot R&B/Hip-Hop Songs",
    "country-songs":              "Hot Country Songs",
    "pop-songs":                  "Pop Airplay",
    "adult-pop-songs":            "Adult Pop Airplay",
    "adult-contemporary":         "Adult Contemporary",
    "triple-a":                   "Adult Alternative Airplay",
    "rhythmic-40":                "Rhythmic Airplay",
    "rap-song":                   "Hot Rap Songs",
    "hot-rock-songs":             "Hot Rock & Alternative Songs",
    "rock-airplay":               "Rock & Alternative Airplay",
    "dance-club-play-songs":      "Dance Club Songs",
    "hot-dance-electronic-songs": "Hot Dance/Electronic Songs",
    "hot-dance-pop-songs":        "Hot Dance/Pop Songs",
    "hot-dance-singles-sales":    "Dance Singles Sales",
    "hot-latin-songs":            "Hot Latin Songs",
    "latin-pop-airplay":          "Latin Pop Airplay",
    "latin-tropical-airplay":     "Tropical Airplay",
    "bubbling-under-hot-100-singles": "Bubbling Under Hot 100",
    "hot-dance-airplay":          "Dance/Mix Show Airplay",
    "hot-r-and-b-hip-hop-airplay": "R&B/Hip-Hop Airplay",
    "alternative-airplay":        "Alternative Airplay",
    "hot-mainstream-rock-tracks": "Mainstream Rock",
}

CHART_MAP_FILE = Path("chart_map.json")


def load_chart_map():
    """Your tag labels -> Billboard slugs.

    Ships with the author's mapping as a fallback, but any user should run
    `init` to generate a chart_map.json against their own tag names. Tag
    labels differ between libraries; slugs don't.
    """
    if CHART_MAP_FILE.exists():
        try:
            data = json.loads(CHART_MAP_FILE.read_text())
            m = data.get("charts", data)
            if isinstance(m, dict) and m:
                return {k: v for k, v in m.items() if not k.startswith("_")}
        except Exception as e:
            print(f"warning: could not read {CHART_MAP_FILE} ({e}); "
                  f"using built-in mapping")
    return dict(DEFAULT_CHART_MAP)


CHART_MAP = load_chart_map()

START_YEAR = 1958
STEP_WEEKS = 2
FUZZ_THRESHOLD = 88
AUTO_APPROVE = 95

# RULE: a renamed chart gets Billboard's CURRENT name. A 1974 soul hit charted
# on what is now Hot R&B/Hip-Hop Songs, so it gets "US Hot R&B/Hip-hop".
# A chart that was discontinued with no successor keeps its historical name —
# but Billboard doesn't host archives under dead names, so there's no data to
# pull for those anyway. They stay manual.
#
# This hook stays in place for the case where a DISCONTINUED chart's archive is
# still reachable under some slug and you want it labeled historically.
# Format: slug -> [(from_date_or_None, until_date_or_None, your_tag_label)]
ERA_MAP = {}

# Tracks carrying any of these tags get no chart proposals. Tag names are
# normalized before comparing (lowercased, punctuation stripped), so "mashup"
# would match a tag spelled "Mash-up" or "Mash Up".
# Empty by default: exclusion is driven by the TITLE, not by tags. A remix of
# a #1 is still that song, so remix-tagged tracks are deliberately allowed.
EXCLUDE_TAG_MATCHES = []

# Titles containing any of these get no chart proposals. These are DJ tools
# built from a song rather than the song itself. Normalized the same way, so
# "mashup" catches "Mash-Up", "Mash up" and "(Phase Mashup)".
EXCLUDE_TITLE_MATCHES = ["mashup", "transition", "blend"]

# If a track matches nothing, propose this tag instead. Set to None to skip.
NO_MATCH_TAG = None               # e.g. "NONE"


# ---------------------------------------------------------------- normalizing

PAREN = re.compile(r"\s*[\(\[][^)\]]*[)\]]")
FEAT = re.compile(r"\s+(feat|ft|featuring|with|vs|presents|pres)\.?\s+.*$", re.I)
NOISE = re.compile(r"[^a-z0-9 ]")
SPACES = re.compile(r"\s+")
LEADING_THE = re.compile(r"^the\s+", re.I)

# Splits a credit down to its primary artist. The comma clause deliberately
# does NOT fire before a corporate suffix, or "Lipps, Inc." becomes "Lipps"
# and stops matching a file tagged "Lipps Inc.". The lookahead sits before
# the whitespace so it can't backtrack past the space and split anyway.
# 'ft' matters: without it "Lady Gaga ft Colby O'Donis" never matches
# Billboard's "Lady Gaga Featuring Colby O'Donis". The slash clause requires
# spaces around it, or "AC/DC" would collapse to "AC".
ARTIST_SPLIT = re.compile(
    r"\s+(?:&|x|and|with|feat\.?|ft\.?|featuring|vs\.?|f/)\s+"
    r"|,(?!\s*(?:inc|ltd|llc|co|jr|sr)\b)\s*"
    r"|\s+/\s+", re.I)

# Billboard credits some acts under names your files don't use. Keys and
# values are both post-normalization forms. Add freely.
ARTIST_ALIASES = {
    "hall": "daryl hall john oates",      # you write "Hall & Oates"
    "janet jackson": "janet",             # credited "Janet" 1993-2001
}


def _pre(s):
    """Spell out symbols so '&' matches 'And' and 'Ke$ha' matches 'Kesha'."""
    return (s or "").replace("&", " and ").replace("+", " and ").replace("$", "s")


def norm_title(s):
    s = FEAT.sub("", PAREN.sub("", _pre(s))).lower()
    return SPACES.sub(" ", NOISE.sub(" ", s)).strip()


def norm_title_flat(s):
    """Like norm_title, but keeps parenthetical content as plain text
    instead of stripping it. Billboard sometimes puts an actual subtitle
    in parens - e.g. "Shake Your Body (Down To The Ground)" - and
    norm_title's usual strip (needed to ignore "(Radio Edit)", "(Clean)",
    "(MM Edit)" etc.) loses it, while a library file with the same subtitle
    spelled out unparenthesized normalizes to something too different to
    fuzzy-match. Used only to index an alternate chart key, same idea as
    the double-A-side variants below."""
    s = FEAT.sub("", _pre(s)).lower()
    return SPACES.sub(" ", NOISE.sub(" ", s)).strip()


def norm_artist(s):
    s = ARTIST_SPLIT.split(PAREN.sub("", _pre(s)))[0].lower()
    s = LEADING_THE.sub("", s)
    s = SPACES.sub(" ", NOISE.sub(" ", s)).strip()
    return ARTIST_ALIASES.get(s, s)


def key(artist, title):
    return f"{norm_artist(artist)}|{norm_title(title)}"


# Matches a bare slash with no surrounding whitespace - the AC/DC shape.
# ARTIST_SPLIT deliberately ignores this (see its docstring), so a credit
# like "Jackson 5/The Jacksons" - an edit-pack crediting a track to
# whichever name the act used that era - fuses into one unmatched blob
# instead of splitting.
BARE_SLASH = re.compile(r"\S/\S")


def fallback_key(artist, title):
    """A second lookup candidate for bare-slash credits, used ONLY when the
    primary key (via `key()`) finds no match at all - exact or fuzzy. Never
    consulted otherwise, so it can't override a real primary match: AC/DC
    resolves through the primary key every time and never reaches this.

    Takes the first slash-separated segment as an alternate primary artist.
    Returns None for anything without a bare slash.
    """
    raw = PAREN.sub("", _pre(artist))
    if not BARE_SLASH.search(raw):
        return None
    alt = LEADING_THE.sub("", raw.split("/")[0].lower())
    alt = SPACES.sub(" ", NOISE.sub(" ", alt)).strip()
    alt = ARTIST_ALIASES.get(alt, alt)
    if not alt:
        return None
    return f"{alt}|{norm_title(title)}"


# ------------------------------------------------------------- lexicon client

def lexicon_get(path, **params):
    r = requests.get(f"{LEXICON}{path}", params=params, timeout=60)
    r.raise_for_status()
    body = r.json()
    return body.get("data", body)


def fetch_tag_index():
    """Return ({id: label}, {label_lower: id})."""
    payload = lexicon_get("/tags")
    by_id, by_label = {}, {}
    for t in payload.get("tags", []):
        label = t.get("label") or t.get("name") or ""
        by_id[t["id"]] = label
        by_label[label.lower()] = t["id"]
    return by_id, by_label


def track_tag_names(track, by_id):
    return sorted(by_id.get(i, f"id:{i}") for i in (track.get("tags") or []))


def _flat(s):
    """Lowercase and drop everything but letters/digits, so 'Mash-up',
    'Mash Up' and 'mashup' all collapse to the same string."""
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def is_excluded(names, title=None):
    """Return the matching exclusion term, or None."""
    for n in names:
        flat = _flat(n)
        for term in EXCLUDE_TAG_MATCHES:
            if _flat(term) in flat:
                return f"tag:{n}"
    if title:
        flat = _flat(title)
        for term in EXCLUDE_TITLE_MATCHES:
            if _flat(term) in flat:
                return f"title:{term}"
    return None


def fetch_library():
    out, offset = [], 0
    while True:
        page = lexicon_get("/tracks", limit=1000, offset=offset)
        rows = page.get("tracks", []) if isinstance(page, dict) else page
        if not rows:
            break
        out.extend(rows)
        offset += len(rows)
        print(f"  pulled {offset} tracks")
        if len(rows) < 1000:
            break
    return out


# ------------------------------------------------------ slug + tag validation

# Charts available as bulk datasets. Loaded by `load` in seconds instead of
# scraped over hours. `fetch` skips any slug listed here.
# Charts available as bulk downloads. `load` ingests these in seconds instead
# of scraping them over hours. Each entry maps the file's column names onto
# what the cache needs. "through" is documentation only - it records where the
# dataset stops so you know what gap `fetch` still has to fill.
DATASET_SOURCES = {
    "hot-100": {
        "url": "https://raw.githubusercontent.com/utdata/rwd-billboard-data/"
               "main/data-out/hot-100-current.csv",
        "date": "chart_week", "title": "title",
        "artist": "performer", "rank": "current_week",
        "through": "current",
    },
    "r-b-hip-hop-songs": {
        "url": "https://raw.githubusercontent.com/pdp2600/chartscraper/master/"
               "ChartScraper_data/All_Hip_Hop_Songs_from_1958-10-20_to_2018-12-31.csv",
        "date": "chart_date", "title": "title",
        "artist": "artist", "rank": "ranking",
        "through": "2018-12-31",
    },
    "country-songs": {
        "url": "https://raw.githubusercontent.com/pdp2600/chartscraper/master/"
               "ChartScraper_data/All_Country_Songs_from_2011-04-09_to_2018-12-31.csv",
        "date": "chart_date", "title": "title",
        "artist": "artist", "rank": "ranking",
        "through": "2018-12-31",
    },
    "hot-dance-electronic-songs": {
        "url": "https://raw.githubusercontent.com/pdp2600/chartscraper/master/"
               "ChartScraper_data/All_Dance_Electronic_Songs_from_2013-01-26_to_2018-12-31.csv",
        "date": "chart_date", "title": "title",
        "artist": "artist", "rank": "ranking",
        "through": "2018-12-31",
    },
    "hot-latin-songs": {
        "url": "https://raw.githubusercontent.com/pdp2600/chartscraper/master/"
               "ChartScraper_data/All_Latin_Songs_from_1986-09-20_to_2018-12-31.csv",
        "date": "chart_date", "title": "title",
        "artist": "artist", "rank": "ranking",
        "through": "2018-12-31",
    },
    "pop-songs": {
        "url": "https://raw.githubusercontent.com/pdp2600/chartscraper/master/"
               "ChartScraper_data/All_Pop_Songs_from_1992-10-03_to_2018-12-31.csv",
        "date": "chart_date", "title": "title",
        "artist": "artist", "rank": "ranking",
        "through": "2018-12-31",
    },
    "hot-rock-songs": {
        "url": "https://raw.githubusercontent.com/pdp2600/chartscraper/master/"
               "ChartScraper_data/All_Rock_Songs_from_2009-06-20_to_2018-12-31.csv",
        "date": "chart_date", "title": "title",
        "artist": "artist", "rank": "ranking",
        "through": "2018-12-31",
    },
}


def phase_load(cache_path=None, refresh=False):
    """Ingest bulk chart datasets into the cache. Seconds, not hours."""
    cache_path = cache_path or CACHE
    seen = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    print(f"cache starts with {len(seen)} songs")

    for slug, spec in DATASET_SOURCES.items():
        local = Path(f"dataset_{slug}.csv")
        if refresh or not local.exists():
            print(f"\ndownloading {slug} ...")
            r = requests.get(spec["url"], timeout=600)
            r.raise_for_status()
            local.write_bytes(r.content)
            print(f"  {local}  {local.stat().st_size / 1e6:.1f} MB")
        else:
            print(f"\nusing cached {local} (--refresh to re-download)")

        added = rows = 0
        with local.open(encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                rows += 1
                artist = row[spec["artist"]]
                title = row[spec["title"]]
                k = key(artist, title)
                if not k.strip("|"):
                    continue
                iso = row[spec["date"]]
                try:
                    rank = int(row[spec["rank"]])
                except (TypeError, ValueError):
                    rank = 999
                rec = seen.setdefault(k, {"artist": artist, "title": title,
                                          "charts": {}})
                c = rec["charts"].get(slug)
                if c is None:
                    rec["charts"][slug] = {"first": iso, "last": iso,
                                           "peak": rank}
                    added += 1
                else:
                    c["first"] = min(c["first"], iso)
                    c["last"] = max(c["last"], iso)
                    c["peak"] = min(c["peak"], rank)

        print(f"  {rows} chart-weeks -> {added} unique songs on {slug}"
              f"   (covers through {spec.get('through', '?')})")

    cache_path.write_text(json.dumps(seen, indent=1, sort_keys=True))
    print(f"\ncache now holds {len(seen)} songs -> {cache_path}")


# Spread across eras so a chart that only existed recently still gets
# at least one probe inside its lifetime.
PROBE_DATES = ["2025-06-07", "2015-06-06", "2000-06-10"]


def _probe(slug, when=None):
    """Return (n_entries, reported_date, top_title, error_name)."""
    import billboard
    try:
        c = billboard.ChartData(slug, date=when)
        return len(c), c.date, (c[0].title if len(c) else None), None
    except Exception as e:
        return None, None, None, type(e).__name__


def phase_charts(only_slugs=None):
    _, by_label = fetch_tag_index()

    slugs = {}
    for label, slug in CHART_MAP.items():
        slugs.setdefault(slug, []).append(label)
    if only_slugs:
        want = {x.strip() for x in only_slugs.split(",")}
        slugs = {k: v for k, v in slugs.items() if k in want}

    missing = [l for l in CHART_MAP if l.lower() not in by_label]
    if missing:
        print("TAGS NOT FOUND IN LEXICON (fix spelling in CHART_MAP):")
        for l in missing:
            print(f"  {l}")
        print()

    print("Probing each slug at several dates.")
    print("A date 'works' when Billboard returns THAT week, not a fallback.\n")

    verdicts = {}
    for slug in sorted(slugs):
        print(f"{slug}")
        works, tops = [], []
        for when in PROBE_DATES:
            n, got, top, err = _probe(slug, when)
            time.sleep(REQUEST_DELAY)
            if err:
                print(f"   {when}   ERROR {err}")
                continue
            # Billboard serves the CURRENT chart for dates outside a chart's
            # lifetime. Identical #1s across eras means fallback, not a broken
            # date parameter - so trust the returned date, not the content.
            match = (got == when)
            flag = "ok      " if (match and n) else "fallback" if n else "empty   "
            print(f"   {when}   {flag} n={n or 0:<4} returned={got}  "
                  f"#1={(top or '')[:34]}")
            if match and n:
                works.append(when)
                tops.append(top)

        if len(works) >= 2 and len(set(tops)) > 1:
            v = "OK"
        elif len(works) == 1:
            v = f"LIMITED - only {works[0]} worked (short-lived chart?)"
        elif works:
            v = "SUSPECT - dates work but content identical"
        else:
            v = "UNUSABLE - no probe date returned its own week"
        verdicts[slug] = v
        print(f"   -> {v}\n")

    print("=" * 62)
    for slug in sorted(verdicts):
        mark = "  " if verdicts[slug] == "OK" else "!!"
        print(f"{mark} {slug:<30} {verdicts[slug]}")
    bad = [x for x, v in verdicts.items() if v != "OK"]
    print(f"\n{len(verdicts) - len(bad)} usable, {len(bad)} need attention.")
    for x in bad:
        print(f"  {x}  ->  {', '.join(slugs[x])}")


def phase_tags(filter_text=None):
    payload = lexicon_get("/tags")
    cats = {c["id"]: c.get("label", "?") for c in payload.get("categories", [])}
    tags = payload.get("tags", [])
    shown = 0
    for t in sorted(tags, key=lambda x: (cats.get(x.get("categoryId"), ""),
                                         str(x.get("label")))):
        label = t.get("label") or ""
        if filter_text and filter_text.lower() not in label.lower():
            continue
        print(f"  {t.get('id'):>5}  {label:<32} [{cats.get(t.get('categoryId'), '?')}]")
        shown += 1
    print(f"\n{shown} of {len(tags)} tags shown")


# -------------------------------------------------------------------- fetching

EMPTY_STREAK_LIMIT = 20    # blank weeks AFTER data starts -> chart predates this
PROGRESS_EVERY = 5         # print a progress line every N captured weeks
REQUEST_DELAY = 1.0        # pause between batches (not between every request)
WORKERS = 1                # parallel requests. 1 = sequential. Try 4-8.
SECONDS_PER_WEEK = 3.4     # measured; used only for the time estimate
VERIFY_BLANKS = True       # under concurrency, re-check any blank week once,
                           # sequentially, before believing it. Throttling and
                           # "chart didn't exist yet" look identical otherwise,
                           # and the difference is silent data loss.

# Weeks to skip between samples. Airplay records sit on a chart for 20-40
# weeks, so sampling every 4th loses almost nothing there. Charts built around
# short runs need every week or they fall apart.
STEP_WEEKS_BY_SLUG = {
    "bubbling-under-hot-100-singles": 1,   # one-week entries are the point
    "hot-dance-singles-sales": 1,  # sales charts turn over fast
}
LEAD_BLANK_LIMIT = 400     # blank weeks BEFORE any data -> give up on the chart
                           # (400 @ STEP_WEEKS=2 reaches ~15 years back, enough
                           #  to find archives of charts that ended long ago)


def week_dates(start_year, step_weeks):
    """Yield Saturdays from the most recent one back to start_year."""
    d = date.today()
    d -= timedelta(days=(d.weekday() - 5) % 7)   # snap back to Saturday
    stop = date(start_year, 1, 1)
    while d >= stop:
        yield d
        d -= timedelta(weeks=step_weeks)


def _progress_path(cache_path):
    return cache_path.with_suffix(".progress.json")


def _load_progress(cache_path):
    f = _progress_path(cache_path)
    if not f.exists():
        return {}
    try:
        return {k: set(v) for k, v in json.loads(f.read_text()).items()}
    except Exception:
        return {}


def _save_progress(cache_path, progress):
    _progress_path(cache_path).write_text(
        json.dumps({k: sorted(v) for k, v in progress.items()},
                   indent=0, sort_keys=True))


def _fetch_week(slug, d):
    """One chart-week. Returns (date, entries, status) - never raises.

    status is one of:
      ok        - Billboard returned the week we asked for
      fallback  - it returned a DIFFERENT week, i.e. this date is outside the
                  chart's lifetime. Definitive: no need to re-check.
      blank     - no entries. Could be a real gap OR throttling, so worth
                  re-checking before we believe it.
      error     - request failed. Same treatment as blank.
    """
    import billboard
    want = d.isoformat()
    try:
        c = billboard.ChartData(slug, date=want)
        entries = list(c)
    except Exception:
        return d, [], "error"
    if not entries:
        return d, [], "blank"
    # Some charts expose no date at all; we can't verify those, so accept them.
    if c.date and c.date != want:
        return d, [], "fallback"
    return d, entries, "ok"


def library_years(window=1):
    """Years present in the Lexicon library, padded by `window` either side.
    Scraping decades your collection doesn't touch is pure waste."""
    years = set()
    for t in fetch_library():
        y = t.get("year")
        if y:
            try:
                y = int(y)
            except (TypeError, ValueError):
                continue
            if 1900 < y < 2100:
                years.update(range(y - window, y + window + 1))
    return years


def phase_fetch(only_slugs=None, max_weeks=None, cache_path=None,
                start_year=None, workers=None, use_library_years=False):
    import billboard

    cache_path = cache_path or CACHE
    start_year = start_year or START_YEAR

    seen = json.loads(cache_path.read_text()) if cache_path.exists() else {}
    if seen:
        print(f"resuming: {len(seen)} songs already in {cache_path}")
    progress = _load_progress(cache_path)
    if progress:
        tot = sum(len(v) for v in progress.values())
        print(f"resuming: {tot} chart-weeks already fetched, will be skipped")

    if only_slugs:
        # Naming a slug explicitly overrides the dataset skip - that's how you
        # gap-fill the years a dataset doesn't cover.
        wanted = {x.strip() for x in only_slugs.split(",")}
        unknown = wanted - set(CHART_MAP.values())
        if unknown:
            sys.exit(f"not in CHART_MAP: {', '.join(sorted(unknown))}")
        slugs = sorted(wanted)
    else:
        slugs = sorted(set(CHART_MAP.values()) - set(DATASET_SOURCES))
        covered = sorted(set(CHART_MAP.values()) & set(DATASET_SOURCES))
        if covered:
            print(f"skipping (covered by `load`): {', '.join(covered)}")
            print("name one with --charts to gap-fill years it doesn't cover")

    workers = workers or WORKERS
    years = None
    if use_library_years:
        print("reading library years...")
        years = library_years()
        print(f"  library spans {min(years)}-{max(years)} "
              f"({len(years)} distinct years)")

    def weeks_for(slug):
        step = STEP_WEEKS_BY_SLUG.get(slug, STEP_WEEKS)
        w = [d for d in week_dates(start_year, step)
             if years is None or d.year in years]
        if max_weeks:
            w = w[:max_weeks]
        done = progress.get(slug, set())
        return [d for d in w if d.isoformat() not in done]

    total = sum(len(weeks_for(sl)) for sl in slugs)
    print(f"{len(slugs)} chart(s), up to {total} requests total")
    secs = total * SECONDS_PER_WEEK / max(1, workers * 0.55)
    print(f"~{secs / 60:.0f} min with {workers} worker(s) "
          f"(measured {SECONDS_PER_WEEK}s/week sequential)")
    print(f"cache: {cache_path}\n")

    for slug in slugs:
        print(f"=== {slug} ===")
        # Never trust chart.date - several charts don't expose one. Drive the
        # loop from dates we generate ourselves.
        empty, done, found_any = 0, 0, False
        t0 = time.time()
        stop = False
        rescued = fallbacks = 0
        weeks = weeks_for(slug)
        step = STEP_WEEKS_BY_SLUG.get(slug, STEP_WEEKS)
        if step != STEP_WEEKS:
            print(f"  (sampling every {step} week(s) for this chart)")
        batch_size = max(1, workers * 2)

        done_weeks = progress.setdefault(slug, set())

        for start in range(0, len(weeks), batch_size):
            if stop:
                break
            batch = weeks[start:start + batch_size]
            if workers > 1:
                with ThreadPoolExecutor(max_workers=workers) as ex:
                    results = list(ex.map(lambda d: _fetch_week(slug, d), batch))
            else:
                results = [_fetch_week(slug, d) for d in batch]

            if VERIFY_BLANKS:
                # Only blank/error are ambiguous - a fallback is a definitive
                # "outside this chart's lifetime" and needs no second look.
                # This runs before ANY data is found too: throttling at the
                # start of a chart is exactly how a whole chart gets lost.
                checked = []
                for d, entries, status in results:
                    if status in ("blank", "error"):
                        time.sleep(REQUEST_DELAY)
                        d, entries, status = _fetch_week(slug, d)
                        if entries:
                            rescued += 1
                    checked.append((d, entries, status))
                results = checked

            for d, entries, status in results:
              if True:
                if status == "fallback":
                    # Billboard served a different week: this date is outside
                    # the chart's lifetime. That is information, not failure -
                    # walking backward we simply haven't reached the archive
                    # yet, so it must NOT count toward giving up.
                    fallbacks += 1
                    done_weeks.add(d.isoformat())
                    continue
                # Record the attempt either way - a verified blank is a real
                # answer and shouldn't be re-fetched on the next run.
                done_weeks.add(d.isoformat())
                if not entries:
                    empty += 1
                    # A chart that ended years ago has a long blank stretch
                    # before its archive begins, so only enforce the tight
                    # limit once we've actually seen data.
                    limit = EMPTY_STREAK_LIMIT if found_any else LEAD_BLANK_LIMIT
                    if empty >= limit:
                        why = ("chart predates this" if found_any
                               else "no data and no fallbacks - dead slug?")
                        print(f"  {empty} blank weeks at {d} - {why}, moving on")
                        stop = True
                        break
                    continue

                if not found_any:
                    found_any = True
                    print(f"  archive starts around {d}")
                empty = 0
                iso = d.isoformat()
                for entry in entries:
                    k = key(entry.artist, entry.title)
                    if not k.strip("|"):
                        continue
                    rec = seen.setdefault(k, {"artist": entry.artist,
                                              "title": entry.title,
                                              "charts": {}})
                    c = rec["charts"].setdefault(slug, {"first": iso,
                                                        "last": iso,
                                                        "peak": entry.rank})
                    c["first"] = min(c["first"], iso)
                    c["last"] = max(c["last"], iso)
                    if entry.rank and entry.rank < c["peak"]:
                        c["peak"] = entry.rank

                done += 1
                if done % PROGRESS_EVERY == 0:
                    rate = (time.time() - t0) / done
                    print(f"  {d}  week {done}  {len(seen)} songs  "
                          f"{rate:.1f}s/week effective")
                    cache_path.write_text(json.dumps(seen, indent=1, sort_keys=True))
                    _save_progress(cache_path, progress)

            # Flush every batch. A long blank stretch captures nothing but
            # still represents work we shouldn't repeat on the next run.
            _save_progress(cache_path, progress)
            time.sleep(REQUEST_DELAY)

        cache_path.write_text(json.dumps(seen, indent=1, sort_keys=True))
        _save_progress(cache_path, progress)
        note = ""
        if rescued:
            note = (f"   [{rescued} blanks were throttling, not missing data"
                    f" - consider fewer workers]")
        if fallbacks:
            note += f"   [{fallbacks} dates outside this chart's lifetime]"
        print(f"  {slug}: {done} weeks captured{note}\n")

    print(f"done - {len(seen)} unique songs in {cache_path}")


# ------------------------------------------------------------ the dry-run plan

def era_label(slug, first_date, slug_to_label):
    """Pick the tag that was correct when the song charted."""
    for start, end, label in ERA_MAP.get(slug, []):
        if (start is None or first_date >= start) and \
           (end is None or first_date < end):
            return label
    return slug_to_label.get(slug)


def phase_plan(only_changes=False, cache_path=None, plan_path=None,
               plugin_out=None):
    from rapidfuzz import fuzz, process

    cache_path = cache_path or CACHE
    plan_path = plan_path or PLAN

    if not cache_path.exists():
        sys.exit(f"no {cache_path} - run `fetch` first")

    chart = json.loads(cache_path.read_text())

    # Billboard lists double A-sides as "Side One/Side Two". Your files hold
    # one side, so index each half against the same record. Done here at match
    # time rather than during fetch, so it costs no re-scraping.
    extra = 0
    for k, rec in list(chart.items()):
        raw = rec.get("title") or ""
        if "/" not in raw:
            continue
        artist_part = k.partition("|")[0]
        for half in raw.split("/"):
            nt = norm_title(half)
            if len(nt) > 3:
                vk = f"{artist_part}|{nt}"
                if vk not in chart:
                    chart[vk] = rec
                    extra += 1
    if extra:
        print(f"  +{extra} double-A-side title variants indexed")

    # Same idea, for titles where Billboard's own parens hold a real
    # subtitle rather than noise - see norm_title_flat's docstring.
    extra2 = 0
    for k, rec in list(chart.items()):
        raw = rec.get("title") or ""
        if "(" not in raw and "[" not in raw:
            continue
        artist_part = k.partition("|")[0]
        flat = norm_title_flat(raw)
        if len(flat) > 3:
            vk = f"{artist_part}|{flat}"
            if vk not in chart:
                chart[vk] = rec
                extra2 += 1
    if extra2:
        print(f"  +{extra2} parenthetical-subtitle title variants indexed")

    chart_keys = list(chart.keys())
    slug_to_label = {}
    for label, slug in CHART_MAP.items():
        slug_to_label.setdefault(slug, label)   # first label wins per slug
    print(f"{len(chart_keys)} charted songs in cache")

    by_id, by_label = fetch_tag_index()
    print("reading library (read-only)...")
    tracks = fetch_library()
    print(f"{len(tracks)} tracks\n")

    rows = []
    plugin_auto, plugin_review = [], []
    for t in tracks:
        artist, title = t.get("artist") or "", t.get("title") or ""
        current = track_tag_names(t, by_id)
        excluded = is_excluded(current, title)

        proposed, score, bb, detail = [], 0, "", ""
        k = key(artist, title)

        if k.strip("|") and not excluded:
            match_key = None
            if k in chart:
                match_key, score = k, 100
            else:
                hit = process.extractOne(k, chart_keys, scorer=fuzz.ratio,
                                         score_cutoff=FUZZ_THRESHOLD)
                if hit:
                    match_key, score = hit[0], round(hit[1])
                else:
                    alt = fallback_key(artist, title)
                    if alt and alt in chart:
                        match_key, score = alt, 100
                    elif alt:
                        hit = process.extractOne(alt, chart_keys, scorer=fuzz.ratio,
                                                 score_cutoff=FUZZ_THRESHOLD)
                        if hit:
                            match_key, score = hit[0], round(hit[1])
            if match_key:
                rec = chart[match_key]
                bb = f"{rec['artist']} — {rec['title']}"
                labels, bits = set(), []
                for slug, info in rec["charts"].items():
                    lab = era_label(slug, info["first"], slug_to_label)
                    if lab:
                        labels.add(lab)
                    bits.append(f"{slug} #{info['peak']} {info['first'][:4]}")
                proposed = sorted(labels)
                detail = "; ".join(sorted(bits))

        to_add = [p for p in proposed if p not in current]

        if excluded:
            action, default = "SKIPPED", 0
        elif not proposed:
            action, default = "NO MATCH", 0
            if NO_MATCH_TAG and NO_MATCH_TAG not in current:
                to_add, action = [NO_MATCH_TAG], "MARK NONE"
        elif not to_add:
            action, default = "ALREADY TAGGED", 0
        elif score >= AUTO_APPROVE:
            action, default = "ADD", 1
        else:
            action, default = "REVIEW", 0

        if only_changes and action in ("NO MATCH", "ALREADY TAGGED"):
            continue

        rows.append({
            "track_id": t.get("id"),
            "my_artist": artist,
            "my_title": title,
            "score": score,
            "action": action,
            "tags_to_add": ", ".join(to_add),
            "chart_tags_now": ", ".join(c for c in current
                                        if c in CHART_MAP or c == NO_MATCH_TAG),
            "all_tags_now": ", ".join(current),
            "billboard_match": bb,
            "charted_on": detail,
            "apply": default,
        })

        if plugin_out and action in ("ADD", "REVIEW") and to_add:
            tag_ids = [by_label[p.lower()] for p in to_add if p.lower() in by_label]
            if len(tag_ids) == len(to_add):   # only queue rows where every
                entry = {                     # tag already exists in Lexicon -
                    "track_id": t.get("id"),  # the plugin never creates tags,
                    "artist": artist,         # same rule as `apply`
                    "title": title,
                    "score": score,
                    "tags_to_add": to_add,
                    "tag_ids": tag_ids,
                    "billboard_match": bb,
                    "charted_on": detail,
                }
                (plugin_auto if action == "ADD" else plugin_review).append(entry)

    order = {"REVIEW": 0, "ADD": 1, "MARK NONE": 2, "ALREADY TAGGED": 3,
             "SKIPPED": 4, "NO MATCH": 5}
    rows.sort(key=lambda r: (order[r["action"]], r["score"]))

    with plan_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    counts = {}
    for r in rows:
        counts[r["action"]] = counts.get(r["action"], 0) + 1

    print("DRY RUN — nothing written.\n")
    print(f"  {plan_path}  ({len(rows)} rows)\n")
    for a in sorted(counts, key=lambda x: order[x]):
        print(f"  {a:<16} {counts[a]:>6}")
    print(f"\n  would tag now: {sum(1 for r in rows if r['apply'] == 1)}")

    unknown = {p for r in rows for p in r["tags_to_add"].split(", ")
               if p and p.lower() not in by_label}
    if unknown:
        print("\nWARNING — these proposed tags don't exist in Lexicon:")
        for u in sorted(unknown):
            print(f"  {u}")
        print("Create them, or fix the labels in CHART_MAP.")

    if plugin_out:
        plugin_out.parent.mkdir(parents=True, exist_ok=True)
        plugin_out.write_text(json.dumps({
            "version": 1,
            "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "auto": plugin_auto,
            "review": plugin_review,
        }, indent=1))
        print(f"\n  plugin queue -> {plugin_out}  "
              f"({len(plugin_auto)} auto, {len(plugin_review)} review)")


# ------------------------------------------------------------------- applying

def _patch_shapes(tid, tags):
    """Candidate PATCH bodies. The API requires an 'edits' wrapper; the exact
    nesting isn't documented, so try each once and lock in whichever works."""
    return [
        ("id+edits",      {"id": tid, "edits": {"tags": tags}}),
        ("edits list",    {"edits": [{"id": tid, "tags": tags}]}),
        ("edits object",  {"edits": {"id": tid, "tags": tags}}),
        ("ids+edits",     {"ids": [tid], "edits": {"tags": tags}}),
    ]


def _write_track(tid, tags, shape=None):
    """Returns (ok, shape_name, detail). Negotiates the body shape once."""
    candidates = _patch_shapes(tid, tags)
    if shape:
        candidates = [c for c in candidates if c[0] == shape]
    errors = []
    for name, body in candidates:
        r = requests.patch(f"{LEXICON}/track", json=body, timeout=30)
        if r.ok:
            return True, name, None
        errors.append(f"{name}: HTTP {r.status_code} {r.text[:160]}")
    return False, None, " | ".join(errors)


def phase_apply(dry, plan_path=None, limit=None, min_score=0, yes=False):
    plan_path = plan_path or PLAN
    if not plan_path.exists():
        sys.exit(f"no {plan_path} - run `plan` first")

    by_id, by_label = fetch_tag_index()

    targets = []
    with plan_path.open(encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row["apply"].strip() != "1":
                continue
            if min_score:
                try:
                    if int(row.get("score") or 0) < min_score:
                        continue
                except ValueError:
                    continue
            labels = [x.strip() for x in row["tags_to_add"].split(",") if x.strip()]
            if labels:
                targets.append((row, labels))

    if not targets:
        print("nothing to apply")
        return
    if min_score:
        print(f"only rows scoring >= {min_score}")

    missing = {l for _, ls in targets for l in ls if l.lower() not in by_label}
    if missing:
        sys.exit("These tags don't exist in Lexicon - create them first:\n  "
                 + "\n  ".join(sorted(missing)))

    # One library read instead of a request per track. The API has no
    # /tracks/{id} route, and 556 round trips would be slow anyway.
    print("reading current tags for the whole library...")
    live = {t.get("id"): list(t.get("tags") or []) for t in fetch_library()}
    print(f"  {len(live)} tracks\n")

    # Anything already carrying all its proposed tags is done - from an
    # earlier wave, or from your own manual tagging.
    pending = [(r, ls) for r, ls in targets
               if any(by_label[l.lower()] not in (live.get(int(r["track_id"])) or [])
                      for l in ls)]
    already = len(targets) - len(pending)
    print(f"{len(targets)} tracks in plan: {already} already done, "
          f"{len(pending)} pending")
    if limit and len(pending) > limit:
        print(f"this wave: {limit} of them (--limit {limit})")
    targets = pending
    if not dry and not yes:
        print("\nBack up your Lexicon library first.")
        if input("type 'yes' to write: ").strip().lower() != "yes":
            sys.exit("aborted - nothing written")

    ok = fail = skip = 0
    shape = None
    preview = []
    for i, (row, labels) in enumerate(targets, 1):
        tid = int(row["track_id"])
        add_ids = [by_label[l.lower()] for l in labels]

        existing = live.get(tid)
        if existing is None:
            fail += 1
            print(f"  track {tid} not in library - skipping")
            continue

        # Merge, never replace. `tags` is a flat array; sending a bare list
        # would wipe every genre and energy tag on the track.
        merged = existing + [x for x in add_ids if x not in existing]
        if merged == existing:
            skip += 1
            continue

        if dry:
            r2 = dict(row)
            r2["tags_before"] = len(existing)
            r2["tags_after"] = len(merged)
            preview.append(r2)
            if len(preview) <= 10:
                print(f"[dry-run] id={tid}  {len(existing)} -> {len(merged)} tags  "
                      f"+{len(labels)}  {row['my_artist']} - {row['my_title'][:34]}")
            if limit and len(preview) >= limit:
                print(f"[dry-run] reached --limit {limit}")
                break
            continue

        if limit and ok >= limit:
            print(f"\n  reached --limit {limit}, stopping here")
            break

        try:
            good, used, detail = _write_track(tid, merged, shape)
            if not good:
                raise RuntimeError(detail)
            if shape is None:
                shape = used
                print(f"  API accepts the '{used}' body shape")
            ok += 1
        except Exception as e:
            fail += 1
            print(f"  failed on {tid}: {e}")
            if fail >= 3 and ok == 0:
                sys.exit("\nThree failures, zero successes - stopping before this "
                         "does damage. Paste the errors above.")
        if i % 50 == 0:
            print(f"  {i}/{len(targets)}")

    if dry:
        out = plan_path.with_name("wave_preview.csv")
        if preview:
            with out.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(preview[0].keys()))
                w.writeheader()
                w.writerows(preview)
            writes = sum(len([x for x in r["tags_to_add"].split(", ") if x])
                         for r in preview)
            print(f"\nDRY RUN - nothing written.")
            print(f"  this wave: {len(preview)} tracks, {writes} tag writes")
            print(f"  full list -> {out}  (open it and review)")
            print(f"  to drop a track, set its apply to 0 in {plan_path.name}")
        else:
            print(f"\nDRY RUN - nothing pending.")
    else:
        left = len(targets) - ok - skip - fail
        print(f"\ndone - {ok} updated, {fail} failed")
        if left > 0:
            print(f"{left} still pending - run the same command again for the "
                  f"next wave")
        else:
            print("no tracks left pending in this plan")


def phase_init(write=False):
    """Propose a chart_map.json by matching Billboard chart names against the
    tags this library already has. Prints for review; --yes writes the file."""
    from rapidfuzz import fuzz

    by_id, _ = fetch_tag_index()
    labels = sorted({v for v in by_id.values() if v})
    print(f"{len(labels)} tags in this Lexicon library\n")

    # Score every chart against every tag, then assign greedily best-first.
    # One tag per chart AND one chart per tag - otherwise a generic tag like
    # "Rock Airplay" gets claimed by four different charts and the last one
    # silently wins.
    pairs = []
    for slug, canonical in KNOWN_CHARTS.items():
        for label in labels:
            sc = fuzz.token_set_ratio(canonical, label)
            if sc >= 70:
                pairs.append((sc, slug, canonical, label))
    pairs.sort(reverse=True)

    proposed, used_slug, used_label, low = {}, set(), set(), []
    for sc, slug, canonical, label in pairs:
        if slug in used_slug or label in used_label:
            continue
        used_slug.add(slug); used_label.add(label)
        if sc >= 85:
            proposed[label] = slug
            print(f"   {canonical:<30} -> {label}   ({sc:.0f})")
        else:
            low.append((sc, canonical, label, slug))

    if low:
        print("\nLow confidence - NOT written. Add by hand if correct:")
        for sc, canonical, label, slug in low:
            print(f"   {canonical:<30} -> {label}   ({sc:.0f})   \"{label}\": \"{slug}\"")

    missing = [(s_, c) for s_, c in sorted(KNOWN_CHARTS.items())
               if s_ not in used_slug]
    if missing:
        print("\nNo tag matched these charts. Create the tag in Lexicon and")
        print("re-run `init` if you want them:")
        for slug, canonical in missing:
            print(f"   {canonical:<30} ({slug})")

    print(f"\n{len(proposed)} confident mappings of {len(KNOWN_CHARTS)} charts.")

    if not write:
        print(f"Re-run with --yes to write {CHART_MAP_FILE}.")
        return

    payload = {
        "_comment": "Your Lexicon tag label -> Billboard chart slug. "
                    "Generated by `init`; edit freely. Remove a line to skip "
                    "that chart. Run `charts` afterwards to verify each slug.",
        "charts": proposed,
    }
    CHART_MAP_FILE.write_text(json.dumps(payload, indent=2))
    print(f"wrote {CHART_MAP_FILE} - review it, then run `charts`")


def phase_probe():
    for path in ("/tracks", "/tags"):
        print(f"\n=== GET {path} ===")
        try:
            print(json.dumps(lexicon_get(path, limit=2), indent=2)[:3000])
        except Exception as e:
            print(f"  error: {e}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("phase", choices=["init", "probe", "tags", "charts", "verify",
                                     "load", "fetch", "plan", "apply"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--only-changes", action="store_true")
    p.add_argument("--filter", default=None)
    p.add_argument("--charts", default=None,
                   help="on `fetch`: comma-separated slugs only")
    p.add_argument("--weeks", type=int, default=None,
                   help="on `fetch`: stop after N weeks per chart")
    p.add_argument("--start-year", type=int, default=None,
                   help="on `fetch`: override START_YEAR")
    p.add_argument("--cache", default=None, help="alternate cache file")
    p.add_argument("--plan", default=None, help="alternate plan CSV")
    p.add_argument("--plugin-out", default=None,
                   help="on `plan`: also write a JSON queue for the Lexicon "
                        "plugin (e.g. the plugin's own data/pending.json)")
    p.add_argument("--tracks", default=None,
                   help="on `verify`: comma-separated Lexicon track ids")
    p.add_argument("--sample", type=int, default=0,
                   help="on `verify`: pick N random tracks instead")
    p.add_argument("--workers", type=int, default=None,
                   help="on `fetch`: parallel requests (default 1)")
    p.add_argument("--library-years", action="store_true",
                   help="on `fetch`: only scrape years your library covers")
    p.add_argument("--refresh", action="store_true",
                   help="on `load`: re-download even if the file is present")
    p.add_argument("--limit", type=int, default=None,
                   help="on `apply`: stop after N successful writes")
    p.add_argument("--min-score", type=int, default=0,
                   help="on `apply`: only rows scoring at least this (100 = "
                        "exact matches only; for unattended runs)")
    p.add_argument("--yes", action="store_true",
                   help="on `apply`: skip the confirmation prompt (scripts)")
    p.add_argument("--window", type=int, default=1,
                   help="on `verify`: +/- years around each track's year")
    a = p.parse_args()
    print(f"billboard_tag v{VERSION}\n")

    if a.phase == "init":
        phase_init(write=a.yes)
    elif a.phase == "probe":
        phase_probe()
    elif a.phase == "tags":
        phase_tags(a.filter)
    elif a.phase == "charts":
        phase_charts(only_slugs=a.charts)
    elif a.phase == "load":
        phase_load(cache_path=Path(a.cache) if a.cache else None,
                   refresh=a.refresh)
    elif a.phase == "verify":
        phase_verify(track_ids=a.tracks, sample=a.sample,
                     only_slugs=a.charts, window=a.window)
    elif a.phase == "fetch":
        phase_fetch(only_slugs=a.charts, max_weeks=a.weeks,
                    cache_path=Path(a.cache) if a.cache else None,
                    start_year=a.start_year, workers=a.workers,
                    use_library_years=a.library_years)
    elif a.phase == "plan":
        phase_plan(only_changes=a.only_changes,
                   cache_path=Path(a.cache) if a.cache else None,
                   plan_path=Path(a.plan) if a.plan else None,
                   plugin_out=Path(a.plugin_out) if a.plugin_out else None)
    else:
        phase_apply(dry=a.dry_run,
                    plan_path=Path(a.plan) if a.plan else None,
                    limit=a.limit, min_score=a.min_score, yes=a.yes)
