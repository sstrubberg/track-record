# Attribution

Track Record is licensed AGPL-3.0 (see [LICENSE](LICENSE)). It also uses
pretrained third-party models under their own, separate terms:

## MTG/Essentia pretrained models

- **discogs-maest** - genre/subgenre classification on raw audio,
  Discogs-400-style taxonomy
- **mood-jamendo** - mood/theme classification (v2, not yet integrated)

Both are produced by the [Music Technology Group (MTG)](https://www.upf.edu/web/mtg)
and distributed for use with [Essentia](https://essentia.upf.edu/), under
**CC BY-NC-ND 4.0**: non-commercial use, unmodified weights, credit
required. Track Record uses them exactly as published - no fine-tuning
or modification - which is why NC/ND is compatible with the project:
Track Record itself is free and non-commercial, distributed to other DJs
at no cost.

If Track Record is ever monetized, this would need to change - the NC
term forbids it. There is no such plan; this note exists so the
constraint isn't lost if that ever comes up.

## Lexicon

Track Record integrates with [Lexicon](https://www.lexicondj.com/) via
its Local API and plugin system. Lexicon's own terms only become
relevant if this toolkit is ever monetized, which it will not be.
