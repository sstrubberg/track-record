# charts

`billboard_tag.py` is a verbatim copy from the `billboard-tag` repo -
same CLI, same phases, no behavior changes. `billboard-tag` stays
untouched; this is a copy, not a move.

It already exposes `phase_load` / `phase_fetch` / `phase_plan` /
`phase_apply`, which is exactly the shared pipeline's
`load -> fetch -> score/plan -> review -> apply` shape - so it doubles
as the reference implementation for how any other action plugs in.

Run it the same way as in `billboard-tag`, from this directory (it
writes `billboard_cache.json`, `billboard_plan.csv`, and reads/writes
`chart_map.json` relative to the current working directory):

```
python billboard_tag.py tags
python billboard_tag.py charts
python billboard_tag.py load
python billboard_tag.py fetch
python billboard_tag.py plan
python billboard_tag.py apply --dry-run
python billboard_tag.py apply
```

The built-in `DEFAULT_CHART_MAP` is the original author's own tag
mapping, kept only as a fallback - run `init` to generate a
`chart_map.json` against your own Lexicon tag names before relying on
this for a different library.

Not yet done: wiring this into the shared `scoring.py` module (it has
its own fuzzy-match scoring today, separate from the noisy-OR scheme
used by the genre/subgenre action) and the `lexicon-plugin` action
`.js` file that would trigger it from Lexicon.
