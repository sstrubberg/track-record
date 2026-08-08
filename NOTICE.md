# Attribution

Track Record is licensed AGPL-3.0 (see [LICENSE](LICENSE)). It also uses
pretrained third-party models under their own, separate terms:

## MTG/Essentia pretrained models

- **discogs-maest** - genre/subgenre classification on raw audio,
  Discogs-400-style taxonomy (end-to-end transformer, its own weights)
- **discogs-effnet** (`discogs-effnet-bs64-1`) - used here purely as an
  embedding extractor, not for its own genre predictions - feeds both
  classification heads below
- **mtg_jamendo_moodtheme** (`mtg_jamendo_moodtheme-discogs-effnet-1`) -
  56-class mood/theme multi-label classification on discogs-effnet
  embeddings
- **genre_discogs400** (`genre_discogs400-discogs-effnet-1`) - a second,
  independent genre/subgenre classifier on the same Discogs-400-style
  taxonomy as discogs-maest, but an EfficientNet head on discogs-effnet
  embeddings rather than an end-to-end transformer - a second opinion
  with a different architecture, not the same model twice

All four are produced by the [Music Technology Group (MTG)](https://www.upf.edu/web/mtg)
and distributed for use with [Essentia](https://essentia.upf.edu/), under
**CC BY-NC-SA 4.0** ([confirmed on Essentia's model page](https://essentia.upf.edu/models.html),
which supersedes the CC BY-NC-ND 4.0 originally assumed in this project's
build spec): non-commercial use, credit required, and any redistributed
*modified* version of the weights must carry the same license. Track
Record uses the weights exactly as published - no fine-tuning or
modification - so the ShareAlike clause doesn't come into play; only
attribution and the non-commercial term apply here.

If Track Record is ever monetized, this would need to change - the NC
term forbids it. There is no such plan; this note exists so the
constraint isn't lost if that ever comes up.

## Lexicon

Track Record integrates with [Lexicon](https://www.lexicondj.com/) via
its Local API and plugin system. Lexicon's own terms only become
relevant if this toolkit is ever monetized, which it will not be.
