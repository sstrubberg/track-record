"""Local audio-model fetch source: discogs-maest via Essentia.

Runs directly on the audio file against the Discogs-400-style
taxonomy. No internet dependency, so it's the only source that
returns anything for untraceable tracks (transition edits, mashups,
bootlegs, DJ tools with no web presence). Model source, so each
result's score is the model's own reported probability for that tag
(see ../scoring.py).

Pretrained weights: MTG/Essentia discogs-maest, CC BY-NC-ND 4.0,
unmodified, non-commercial use, credit required. See ../../NOTICE.md.
"""

from __future__ import annotations


def fetch_genres(audio_path: str) -> list[dict]:
    """Return candidate genre tags for a track from the local audio model.

    Each candidate: {"tag": str, "score": float, "source": "audio_model",
    "url": None, "note": str | None}
    """
    raise NotImplementedError
