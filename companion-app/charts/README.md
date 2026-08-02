# charts

Reference implementation for how an action plugs into the shared
`load -> fetch -> score/plan -> review -> apply` pipeline.

Port target: `billboard_tag.py` from the `billboard-tag` repo, as-is,
no behavior changes. `billboard-tag` stays untouched; this is a copy,
not a move. `chart_map.json` and the existing cache approach carry over
unchanged.

Not yet ported.
