"""Local audio-model fetch source: discogs-maest via Essentia.

Runs directly on the audio file against the Discogs-400 style taxonomy.
No internet dependency, so it's the only source that returns anything
for untraceable tracks (transition edits, mashups, bootlegs, DJ tools
with no web presence). Model source, so each result's score is the
model's own reported probability for that tag (see ../scoring.py).

Pretrained weights: MTG/Essentia discogs-maest, CC BY-NC-SA 4.0,
unmodified, non-commercial use, credit required. See ../../NOTICE.md.
Weights (~330 MB) are downloaded on first use into ../models/ (gitignored),
not committed to this repo.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.request import urlretrieve

import numpy as np

MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "discogs-maest"
WEIGHTS_URL = "https://essentia.upf.edu/models/feature-extractors/maest/discogs-maest-30s-pw-2.pb"
METADATA_URL = "https://essentia.upf.edu/models/feature-extractors/maest/discogs-maest-30s-pw-2.json"
WEIGHTS_PATH = MODEL_DIR / "discogs-maest-30s-pw-2.pb"
METADATA_PATH = MODEL_DIR / "discogs-maest-30s-pw-2.json"

SAMPLE_RATE = 16000

# The model exposes two output nodes (see the metadata JSON's "schema"):
# "PartitionedCall/Identity" (op: Linear) is raw logits, unbounded: the
# TensorflowPredictMAEST default. "PartitionedCall/Identity_13" (op:
# Sigmoid) is the actual per-class probability - the one that matches
# scoring.py's "score = model's own reported probability" contract.
SIGMOID_OUTPUT = "PartitionedCall/Identity_13"

# Sigmoid over 400 classes puts most of them near zero; this is a noise
# floor, not a top-N cutoff, so it doesn't violate "no cap on candidate
# tags per track" - review/scoring still sees every tag that clears it.
MIN_PROBABILITY = 0.05

_model = None
_classes: list[str] | None = None


def _ensure_model() -> None:
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    if not WEIGHTS_PATH.exists():
        print(f"Downloading discogs-maest weights (~330 MB) to {WEIGHTS_PATH} ...")
        urlretrieve(WEIGHTS_URL, WEIGHTS_PATH)
    if not METADATA_PATH.exists():
        urlretrieve(METADATA_URL, METADATA_PATH)


def _load_classes() -> list[str]:
    global _classes
    if _classes is None:
        _ensure_model()
        _classes = json.loads(METADATA_PATH.read_text())["classes"]
    return _classes


def _load_model():
    global _model
    if _model is None:
        import essentia.standard as es

        _ensure_model()
        _model = es.TensorflowPredictMAEST(
            graphFilename=str(WEIGHTS_PATH),
            output=SIGMOID_OUTPUT,
        )
    return _model


def fetch_genres(audio_path: str) -> list[dict]:
    """Return candidate genre tags for a track from the local audio model.

    Each candidate: {"tag": str, "score": float, "source": "audio_model",
    "url": None, "note": str}
    """
    import essentia.standard as es

    classes = _load_classes()
    model = _load_model()

    audio = es.MonoLoader(filename=audio_path, sampleRate=SAMPLE_RATE, resampleQuality=4)()
    predictions = model(audio)
    # Patch-based inference: one row of 400 probabilities per ~30s patch,
    # wrapped in extra singleton dims. Flatten to (n_patches, 400) and
    # average across patches for one whole-track probability per class.
    per_patch = predictions.reshape(-1, len(classes))
    probabilities = np.mean(per_patch, axis=0)

    candidates = []
    for cls, prob in zip(classes, probabilities):
        if prob < MIN_PROBABILITY:
            continue
        genre, _, style = cls.partition("---")
        candidates.append({
            "tag": style,
            "score": float(prob),
            "source": "audio_model",
            "url": None,
            "note": f"discogs-maest: {genre} — {style} ({prob:.0%})",
        })
    return sorted(candidates, key=lambda c: c["score"], reverse=True)


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        sys.exit("usage: python audio_model.py <audio-file>")
    for candidate in fetch_genres(sys.argv[1]):
        print(candidate)
