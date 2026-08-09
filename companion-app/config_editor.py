"""Round-trip YAML load/save for review_ui.py's Settings view.

scoring.py's own load_weights() reads source_weights.yaml/
mood_weights.yaml with plain pyyaml elsewhere in this app - that's
fine for reading, but pyyaml.dump() can't preserve comments, and both
files are full of hand-written explanatory ones (why each weight is
what it is, why a threshold sits where it does). A GUI editor that
saved through plain pyyaml would flatten every one of those the first
time anyone clicked Save. ruamel.yaml's round-trip mode can load a
file, let specific values be edited in place, and write it back out
with comments and formatting intact - confirmed directly: loading and
immediately re-saving source_weights.yaml with zero edits produces a
byte-for-byte identical file.

width is set wide specifically so ruamel doesn't re-wrap this
project's routinely-long comment lines to fit some default column
width on save - same "confirmed identical round-trip" test would fail
without it.
"""

from __future__ import annotations

from pathlib import Path

from ruamel.yaml import YAML

_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.width = 4096


def load(path: Path):
    """Returns a ruamel CommentedMap - behaves like a plain dict for
    reading/assigning values, but remembers the comments/formatting
    around them for save() to write back out unchanged."""
    with open(path) as f:
        return _yaml.load(f)


def save(path: Path, data) -> None:
    with open(path, "w") as f:
        _yaml.dump(data, f)
