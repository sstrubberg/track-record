"""Local audio-model fetch source: genre_discogs400 via Essentia -
a second, independent Genre/Subgenre audio model alongside
audio_model.py's discogs-maest.

Same Discogs 400-style Genre---Style taxonomy as discogs-maest (this
is Discogs' own taxonomy either way, not a second one to reconcile),
but a genuinely different architecture: an EfficientNet classification
head on top of discogs-effnet's embeddings, vs. discogs-maest's
end-to-end transformer. Same taxonomy means no new alias-mapping
anywhere (genre_taxonomy.yaml, scoring.py); different architecture
means this is real second-opinion corroboration for
auto_include.min_agreeing_sources, not the same model asked twice.

Shares its embedding extractor with audio_model_mood.py rather than
downloading a second copy - see audio_model_mood.load_embedding_model()
for why. That's the only coupling between the two modules; this one's
own classification head (genre_discogs400) is independent of Mood/
Theme's (mtg_jamendo_moodtheme) - different weights, different output
node, different taxonomy.

Pretrained weights: MTG/Essentia genre_discogs400 (classification head)
+ discogs-effnet (embedding extractor, shared with audio_model_mood.py),
CC BY-NC-SA 4.0, unmodified, non-commercial use, credit required. See
../../NOTICE.md. The head itself is tiny (~2 MB); the embedding weights
(~18 MB) are the same file audio_model_mood.py already downloads for
Mood/Theme, so this adds ~2 MB on top if that's already been fetched,
or the full ~20 MB combined if this is the first audio-model source
ever run in a given library scan.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import urlretrieve

import numpy as np

# Must be set before TensorFlow's C++ side initializes (transitively,
# via essentia.standard below) - otherwise it ignores this and logs
# anyway. Also set in audio_model.py/audio_model_mood.py; harmless and
# necessary to repeat here since this module can run without either of
# those having imported first.
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

try:
    from . import audio_model_mood  # noqa: E402 - see TF_CPP_MIN_LOG_LEVEL comment above
except ImportError:
    # Run directly as a script ("python fetch/audio_model_genre_effnet.py
    # ..."), not imported through the `fetch` package - there's no
    # relative-import parent to resolve `.` against. Fall back to a
    # plain absolute import instead, same as every other fetch module
    # supports for its own ad hoc standalone testing. This does mean a
    # standalone run gets its own separate audio_model_mood module
    # instance rather than sharing plan.py's - harmless here (nothing
    # else is using audio_model_mood in the same process to share
    # with), and irrelevant to the real pipeline, which always imports
    # this properly through the package and never takes this branch.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import audio_model_mood  # type: ignore[no-redef]

MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "genre-discogs400"

HEAD_WEIGHTS_URL = "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.pb"
HEAD_METADATA_URL = "https://essentia.upf.edu/models/classification-heads/genre_discogs400/genre_discogs400-discogs-effnet-1.json"
HEAD_WEIGHTS_PATH = MODEL_DIR / "genre_discogs400-discogs-effnet-1.pb"
HEAD_METADATA_PATH = MODEL_DIR / "genre_discogs400-discogs-effnet-1.json"

# This head's own input/output node names (confirmed against its
# metadata JSON's "schema") - neither matches TensorflowPredict2D's own
# defaults ("model/Placeholder" / "model/Sigmoid"), which mtg_jamendo_
# moodtheme's head happens to (that one only needed `output` set
# explicitly, as a matter of principle rather than necessity - see
# audio_model_mood.py's own comment on HEAD_OUTPUT). This graph
# actually needs both set, or TensorflowPredict2D fails immediately
# with "'model/Placeholder' is not a valid node name of this graph" -
# confirmed by hitting that exact error before adding HEAD_INPUT.
HEAD_INPUT = "serving_default_model_Placeholder"
HEAD_OUTPUT = "PartitionedCall:0"

# Sigmoid over 400 classes puts most of them near zero - a noise floor,
# not a top-N cutoff, same rationale and same value as audio_model.py's
# own MIN_PROBABILITY.
MIN_PROBABILITY = 0.05

_head_model = None
_classes: list[str] | None = None


def _ensure_model() -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if not HEAD_WEIGHTS_PATH.exists():
        print(f"Downloading genre_discogs400 classification head (~2 MB) to {HEAD_WEIGHTS_PATH} ...")
        urlretrieve(HEAD_WEIGHTS_URL, HEAD_WEIGHTS_PATH)
    if not HEAD_METADATA_PATH.exists():
        urlretrieve(HEAD_METADATA_URL, HEAD_METADATA_PATH)


def _load_classes() -> list[str]:
    global _classes
    if _classes is None:
        _ensure_model()
        _classes = json.loads(HEAD_METADATA_PATH.read_text())["classes"]
    return _classes


def _load_head_model():
    global _head_model
    if _head_model is None:
        import essentia.standard as es

        _ensure_model()
        _head_model = es.TensorflowPredict2D(
            graphFilename=str(HEAD_WEIGHTS_PATH),
            input=HEAD_INPUT,
            output=HEAD_OUTPUT,
        )
    return _head_model


def fetch_genres(audio_path: str) -> list[dict]:
    """Return candidate genre tags for a track from the local audio
    model.

    Each candidate: {"tag": str, "score": float,
    "source": "audio_model_genre_effnet", "url": None, "note": str}
    """
    import essentia.standard as es

    classes = _load_classes()
    embedding_model = audio_model_mood.load_embedding_model()
    head_model = _load_head_model()

    audio = es.MonoLoader(filename=audio_path, sampleRate=audio_model_mood.SAMPLE_RATE, resampleQuality=4)()
    embeddings = embedding_model(audio)
    predictions = head_model(embeddings)
    # Same patch-averaging shape as audio_model.py/audio_model_mood.py:
    # one row of 400 probabilities per embedding patch, averaged into
    # one whole-track probability per class.
    per_patch = np.array(predictions).reshape(-1, len(classes))
    probabilities = np.mean(per_patch, axis=0)

    candidates = []
    for cls, prob in zip(classes, probabilities):
        if prob < MIN_PROBABILITY:
            continue
        genre, _, style = cls.partition("---")
        note = f"genre_discogs400: {genre} — {style} ({prob:.0%})"
        # Same Genre---Style split as audio_model.py - both halves are
        # their own candidate tag. scoring.py's group_by_tag already
        # combines duplicate tags (same style proposed by both this
        # source and discogs-maest, or repeated across multiple
        # Genre---Style pairs sharing a genre) via noisy-OR, so no
        # aggregation needed here.
        candidates.append({
            "tag": style,
            "score": float(prob),
            "source": "audio_model_genre_effnet",
            "url": None,
            "note": note,
        })
        candidates.append({
            "tag": genre,
            "score": float(prob),
            "source": "audio_model_genre_effnet",
            "url": None,
            "note": note,
        })
    return sorted(candidates, key=lambda c: c["score"], reverse=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        sys.exit("usage: python audio_model_genre_effnet.py <audio-file>")
    for candidate in fetch_genres(sys.argv[1]):
        print(candidate)
