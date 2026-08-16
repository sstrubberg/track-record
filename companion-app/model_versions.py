#!/usr/bin/env python3
"""Checks Essentia's own model listing for a newer version of any audio
model this project uses, and can update to it.

fetch/audio_model*.py's own download logic is deliberately just "if not
already downloaded, fetch it" (see each module's own _ensure_model()) -
no version check, no re-download trigger of any kind, ever, once a file
is cached in models/. That's fine for not silently drifting underfoot
between runs, but it also means there was never any way to *find out*
a newer model exists, let alone get it, short of manually watching
https://essentia.upf.edu/models.html and hand-editing a URL constant.
This is that missing piece.

Each model's "version" is a number Essentia itself bakes into the
filename (discogs-maest-30s-pw-2.pb, mtg_jamendo_moodtheme-discogs-
effnet-1.pb, ...) - not something this project assigns. Checking for
an update means fetching models.html (a plain HTML page, confirmed
directly to list every published filename) and regex-searching it for
this model's own name with a higher version number than what's
currently cached, rather than guessing "current + 1" and probing - a
version scheme is free to skip a number, and probing would miss that.

Applying an update does two things, in order: downloads the new
version's weights (+ metadata, where that model has one) into the same
models/ directory under its own new filename, then edits the relevant
fetch/audio_model*.py source file in place - a precise, scoped string
replace of the old "<name>-<old version>" for the new one, confirmed
safe per-module (that exact substring appears nowhere else in any of
these three files - only in the WEIGHTS_URL/METADATA_URL/WEIGHTS_PATH/
METADATA_PATH constants a version bump needs to touch). That's what
actually makes review_ui.py/plan.py/mood_plan.py start using the new
version on their next run - just downloading a same-named-but-newer
file under a new filename wouldn't do that on its own, since those
modules resolve which file to load from their own hardcoded constants,
not by scanning models/ for whatever's newest. The old version's now-
unused file is deleted right after, so a DJ doesn't accumulate stale
multi-hundred-MB files release over release.

    python model_versions.py              # report only, changes nothing
    python model_versions.py --apply      # download + switch to newer ones
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.request import urlretrieve

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch import audio_model, audio_model_genre_effnet, audio_model_mood  # noqa: E402

MODELS_PAGE = "https://essentia.upf.edu/models.html"


@dataclass
class ModelFile:
    label: str
    module_path: Path  # the fetch/*.py file to edit on an update
    weights_url: str
    weights_path: Path
    metadata_url: str | None = None
    metadata_path: Path | None = None

    @property
    def base_name(self) -> str:
        """The filename with its trailing -<version> stripped, e.g.
        "discogs-maest-30s-pw" from ".../discogs-maest-30s-pw-2.pb" -
        what actually gets searched for in models.html and what a
        version bump's string replace is scoped to."""
        filename = self.weights_url.rsplit("/", 1)[-1]
        return re.sub(r"-\d+\.pb$", "", filename)

    @property
    def current_version(self) -> int:
        filename = self.weights_url.rsplit("/", 1)[-1]
        match = re.search(r"-(\d+)\.pb$", filename)
        return int(match.group(1)) if match else 0


# One entry per real model file this project loads - the shared
# discogs-effnet embedding extractor (audio_model_mood.py's own
# EMBEDDING_WEIGHTS_*) is listed once here even though
# audio_model_genre_effnet.py also uses it, since that module only
# ever calls audio_model_mood.load_embedding_model() rather than
# hardcoding a second copy of the URL/path - one entry, one place to
# bump, matches the one real file on disk.
MODEL_FILES = [
    ModelFile(
        "discogs-maest (Genre/Subgenre audio model)",
        Path(audio_model.__file__),
        audio_model.WEIGHTS_URL, audio_model.WEIGHTS_PATH,
        audio_model.METADATA_URL, audio_model.METADATA_PATH,
    ),
    ModelFile(
        "discogs-effnet (embedding extractor shared by Mood/Theme and genre_discogs400)",
        Path(audio_model_mood.__file__),
        audio_model_mood.EMBEDDING_WEIGHTS_URL, audio_model_mood.EMBEDDING_WEIGHTS_PATH,
    ),
    ModelFile(
        "mtg_jamendo_moodtheme (Mood/Theme audio model)",
        Path(audio_model_mood.__file__),
        audio_model_mood.HEAD_WEIGHTS_URL, audio_model_mood.HEAD_WEIGHTS_PATH,
        audio_model_mood.HEAD_METADATA_URL, audio_model_mood.HEAD_METADATA_PATH,
    ),
    ModelFile(
        "genre_discogs400 (second Genre/Subgenre audio model)",
        Path(audio_model_genre_effnet.__file__),
        audio_model_genre_effnet.HEAD_WEIGHTS_URL, audio_model_genre_effnet.HEAD_WEIGHTS_PATH,
        audio_model_genre_effnet.HEAD_METADATA_URL, audio_model_genre_effnet.HEAD_METADATA_PATH,
    ),
]


def _latest_published_version(model: ModelFile, page_html: str) -> int | None:
    versions = [int(v) for v in re.findall(re.escape(model.base_name) + r"-(\d+)\.pb", page_html)]
    return max(versions) if versions else None


def check_all() -> list[dict]:
    """Read-only: one GET of models.html, no writes. Returns one dict
    per model - {"label", "current", "latest", "update_available",
    "downloaded"} - "latest" is None if models.html couldn't be
    reached or this model's name wasn't found there at all (a real
    possibility if Essentia ever renames/retires it, not just an
    error), in which case update_available is always False rather than
    guessed at."""
    try:
        page_html = requests.get(MODELS_PAGE, timeout=15).text
    except requests.RequestException as e:
        return [
            {"label": m.label, "current": m.current_version, "latest": None,
             "update_available": False, "downloaded": m.weights_path.exists(), "error": str(e)}
            for m in MODEL_FILES
        ]
    results = []
    for model in MODEL_FILES:
        latest = _latest_published_version(model, page_html)
        results.append({
            "label": model.label,
            "current": model.current_version,
            "latest": latest,
            "update_available": latest is not None and latest > model.current_version,
            "downloaded": model.weights_path.exists(),
        })
    return results


def _bump(text: str, old_name_version: str, new_name_version: str) -> str:
    if old_name_version not in text:
        raise ValueError(f"{old_name_version!r} not found - refusing to touch this file")
    return text.replace(old_name_version, new_name_version)


def apply_update(model: ModelFile, new_version: int, on_status=None) -> None:
    """Downloads the new version's file(s), edits model.module_path in
    place to point at them, then deletes the old file(s). Raises
    rather than partially applying if any step fails - a DJ re-running
    with --apply after a failure just retries from scratch, since
    nothing here is destructive until the final delete."""
    def status(msg):
        if on_status:
            on_status(msg)

    old_base = f"{model.base_name}-{model.current_version}"
    new_base = f"{model.base_name}-{new_version}"

    new_weights_url = model.weights_url.replace(old_base, new_base)
    new_weights_path = model.weights_path.with_name(model.weights_path.name.replace(old_base, new_base))
    status(f"  downloading {new_weights_url} ...")
    urlretrieve(new_weights_url, new_weights_path)

    new_metadata_url = new_metadata_path = None
    if model.metadata_url is not None:
        new_metadata_url = model.metadata_url.replace(old_base, new_base)
        new_metadata_path = model.metadata_path.with_name(model.metadata_path.name.replace(old_base, new_base))
        status(f"  downloading {new_metadata_url} ...")
        urlretrieve(new_metadata_url, new_metadata_path)

    status(f"  updating {model.module_path.name} ...")
    text = model.module_path.read_text()
    text = _bump(text, old_base, new_base)
    model.module_path.write_text(text)

    old_weights_path, old_metadata_path = model.weights_path, model.metadata_path
    old_weights_path.unlink(missing_ok=True)
    if old_metadata_path is not None:
        old_metadata_path.unlink(missing_ok=True)
    status(f"  {model.label}: {model.current_version} -> {new_version}")


def print_report(results: list[dict]) -> None:
    for r in results:
        if not r["downloaded"]:
            state = "not downloaded yet"
        elif r.get("error"):
            state = f"couldn't check ({r['error']})"
        elif r["latest"] is None:
            state = "not found on models.html - check manually"
        elif r["update_available"]:
            state = f"UPDATE AVAILABLE: v{r['current']} -> v{r['latest']}"
        else:
            state = f"up to date (v{r['current']})"
        print(f"  {r['label']}: {state}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--apply", action="store_true", help="download + switch to newer versions (default: report only)")
    args = p.parse_args()

    print("Checking https://essentia.upf.edu/models.html against locally cached model versions...\n")
    results = check_all()
    print_report(results)

    updatable = [(m, r) for m, r in zip(MODEL_FILES, results) if r["update_available"]]
    if not updatable:
        print("\nnothing to update")
        return

    if not args.apply:
        print(f"\n(dry run - nothing changed; pass --apply to actually update {len(updatable)} model(s))")
        return

    print(f"\nupdating {len(updatable)} model(s)...")
    for model, r in updatable:
        apply_update(model, r["latest"], on_status=print)


if __name__ == "__main__":
    main()
