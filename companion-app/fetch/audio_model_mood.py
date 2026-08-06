"""Local audio-model fetch source: MTG-Jamendo mood/theme via Essentia.

Runs directly on the audio file, no internet dependency - same shape as
audio_model.py's genre/style model, just a different pretrained model
and taxonomy. Model source, so each result's score is the model's own
reported probability for that tag (see ../scoring.py).

Unlike discogs-maest (a single end-to-end model), this is a two-stage
pipeline: a general-purpose embedding extractor (discogs-effnet, itself
trained as a 400-style genre classifier - we only use its penultimate
layer, not its own genre predictions) feeding a small mood/theme
classification head trained on top of those embeddings. Both stages
are required; the classification head alone doesn't take raw audio.

Pretrained weights: MTG/Essentia discogs-effnet + mtg_jamendo_moodtheme,
CC BY-NC-SA 4.0, unmodified, non-commercial use, credit required. See
../../NOTICE.md. Weights (~21 MB total, much lighter than discogs-maest)
are downloaded on first use into ../models/ (gitignored), not committed
to this repo.

Worth knowing going in: this task is intrinsically noisier than genre
classification. MTG's own published metrics for this exact model put
test PR-AUC at 0.14 (vs. discogs-maest's typical 0.2+ on a comparably
sized label set) - mood and theme are more subjective and harder to
learn from audio alone than genre/style. That's not a bug to fix here;
it's a reason every prediction still goes through the same review gate
as everything else, not a reason to trust this source less carefully
than that gate already assumes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.request import urlretrieve

import numpy as np

# Must be set before TensorFlow's C++ side initializes (transitively, via
# essentia.standard below) - otherwise it ignores this and logs anyway.
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "mood-theme"

EMBEDDING_WEIGHTS_URL = "https://essentia.upf.edu/models/feature-extractors/discogs-effnet/discogs-effnet-bs64-1.pb"
EMBEDDING_WEIGHTS_PATH = MODEL_DIR / "discogs-effnet-bs64-1.pb"

HEAD_WEIGHTS_URL = "https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.pb"
HEAD_METADATA_URL = "https://essentia.upf.edu/models/classification-heads/mtg_jamendo_moodtheme/mtg_jamendo_moodtheme-discogs-effnet-1.json"
HEAD_WEIGHTS_PATH = MODEL_DIR / "mtg_jamendo_moodtheme-discogs-effnet-1.pb"
HEAD_METADATA_PATH = MODEL_DIR / "mtg_jamendo_moodtheme-discogs-effnet-1.json"

SAMPLE_RATE = 16000

# discogs-effnet exposes two output nodes (confirmed against its own
# metadata JSON's "schema"): "PartitionedCall:0" (op: Sigmoid) is that
# model's own 400-style genre prediction, which we don't want here;
# "PartitionedCall:1" (op: Flatten, shape [.., 1280]) is the penultimate
# layer's activations - the actual embedding the mood/theme head expects.
# The algorithm's own default output ("PartitionedCall", no index) is
# neither of these explicitly, so this has to be set, not left implicit -
# same lesson as discogs-maest's output-node default being wrong there.
EMBEDDING_OUTPUT = "PartitionedCall:1"

# TensorflowPredict2D's own default ("model/Sigmoid") already matches
# this head's real output node - set explicitly anyway, so a future
# model swap can't silently start reading the wrong node the way
# discogs-effnet's embedding output would if left on its own default.
HEAD_OUTPUT = "model/Sigmoid"

# Much noisier task than genre (see module docstring) - kept at the same
# floor as discogs-maest anyway, since this is a noise floor spinning to
# avoid the review screen from doing that job worse, not a judgment
# about how much to trust any given prediction above it.
MIN_PROBABILITY = 0.05

_embedding_model = None
_head_model = None
_classes: list[str] | None = None


def _ensure_model() -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if not EMBEDDING_WEIGHTS_PATH.exists():
        print(f"Downloading discogs-effnet embedding weights (~18 MB) to {EMBEDDING_WEIGHTS_PATH} ...")
        urlretrieve(EMBEDDING_WEIGHTS_URL, EMBEDDING_WEIGHTS_PATH)
    if not HEAD_WEIGHTS_PATH.exists():
        print(f"Downloading mood/theme classification head (~3 MB) to {HEAD_WEIGHTS_PATH} ...")
        urlretrieve(HEAD_WEIGHTS_URL, HEAD_WEIGHTS_PATH)
    if not HEAD_METADATA_PATH.exists():
        urlretrieve(HEAD_METADATA_URL, HEAD_METADATA_PATH)


def _load_classes() -> list[str]:
    global _classes
    if _classes is None:
        _ensure_model()
        _classes = json.loads(HEAD_METADATA_PATH.read_text())["classes"]
    return _classes


def _load_models():
    global _embedding_model, _head_model
    if _embedding_model is None or _head_model is None:
        import essentia
        import essentia.standard as es

        # Same noisy-warning silencing as audio_model.py - harmless
        # per-patch setup chatter that otherwise reads as a hang.
        essentia.log.warningActive = False
        essentia.log.infoActive = False

        _ensure_model()
        _embedding_model = es.TensorflowPredictEffnetDiscogs(
            graphFilename=str(EMBEDDING_WEIGHTS_PATH),
            output=EMBEDDING_OUTPUT,
        )
        _head_model = es.TensorflowPredict2D(
            graphFilename=str(HEAD_WEIGHTS_PATH),
            output=HEAD_OUTPUT,
        )
    return _embedding_model, _head_model


def fetch_moods(audio_path: str) -> list[dict]:
    """Return candidate mood/theme tags for a track from the local audio
    model.

    Each candidate: {"tag": str, "score": float, "source": "audio_model_mood",
    "url": None, "note": str}
    """
    import essentia.standard as es

    classes = _load_classes()
    embedding_model, head_model = _load_models()

    audio = es.MonoLoader(filename=audio_path, sampleRate=SAMPLE_RATE, resampleQuality=4)()
    embeddings = embedding_model(audio)
    predictions = head_model(embeddings)
    # Same patch-averaging shape as audio_model.py: one row of 56
    # probabilities per embedding patch, averaged into one whole-track
    # probability per class.
    per_patch = np.array(predictions).reshape(-1, len(classes))
    probabilities = np.mean(per_patch, axis=0)

    candidates = []
    for cls, prob in zip(classes, probabilities):
        if prob < MIN_PROBABILITY:
            continue
        candidates.append({
            "tag": cls,
            "score": float(prob),
            "source": "audio_model_mood",
            "url": None,
            "note": f"mtg_jamendo_moodtheme: {cls} ({prob:.0%})",
        })
    return sorted(candidates, key=lambda c: c["score"], reverse=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        sys.exit("usage: python audio_model_mood.py <audio-file>")
    for candidate in fetch_moods(sys.argv[1]):
        print(candidate)
