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
this for a different library. `chart_map.json` in this directory was
generated exactly that way, against this library's real tags - see its
own `_comment` field for what got hand-excluded and why.

Now wired into the shared pipeline as `../charts_plan.py` /
`../charts_apply.py` - `review_ui.py`'s "Charts" checkbox, alongside
Genre/Subgenre and Mood/Theme, rather than this file's own
`phase_plan`/`phase_apply`. `charts_plan.py` imports this module for
its stable matching primitives (`key()`, `fallback_key()`,
`norm_title()`, `era_label()`, `is_excluded()`, ...) and its own
`phase_load`/`phase_fetch` (surfaced in Settings' "Chart Cache" card),
but never calls `phase_plan`/`phase_apply` themselves - those still
work standalone exactly as before, just no longer the only path in.
See `charts_plan.py`'s own docstring for the two module-level path
constants (`CACHE`, `CHART_MAP_FILE`) it has to re-point at runtime,
since they're bare relative paths correct only when this file's own
CLI is run from inside this directory, as above.

Still not done: the `lexicon-plugin` action `.js` file that would
trigger a scan from inside Lexicon itself, rather than from
`review_ui.py` directly.
