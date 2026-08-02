# lexicon-plugin

Thin trigger/status layer only. Lexicon plugins run in a sandboxed JS
environment - no `require()`, no spawning processes, no native modules -
so none of the actual work (fetching, scoring, the review UI, applying
tags) happens here. That all lives in `../companion-app`, a separate
local Python process the user runs directly, closer to how
`billboard_tag.py` works today than to a fully plugin-driven flow.

`config.json` defines the two v1 actions:

- **Tag Charts** - port of `billboard-tag`'s existing
  `billboardtag.reviewapply` action, no behavior changes.
- **Tag Genres + Subgenres** - reviews candidates the companion app has
  already fetched and scored, same merge-never-replace apply rule.

A third action, **Tag Mood + Theme**, is v2 and not part of this build.

Action `.js` files are not yet ported/written.
