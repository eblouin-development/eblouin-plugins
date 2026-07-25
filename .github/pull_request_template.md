<!--
PR template for the eblouin-plugins repo itself (the plugin/library, not a project
built with it). Keep it lean — fill what applies, delete what doesn't.
-->

## Summary
One or two sentences: what this PR adds/changes and why it belongs in the plugin.

## What changed / why
-

## Decision log
Judgment calls made while building this (naming, placement, anything a reviewer
should sanity-check rather than assume) — or "none" if there weren't any.

## Checklist
- [ ] `python scripts/validate_plugin.py` passes with 0 warnings (paste output below if any warnings were fixed)
- [ ] Every new/changed reference, template block, catalog component, or recipe has an updated `last-verified:` header
- [ ] Every new skill's frontmatter `name` matches its directory name
- [ ] A `release:major` / `release:minor` / `release:patch` label is set on this PR (see `release.yml`; default is patch if unset)
- [ ] No hand-edited version fields — `plugin.json` / `marketplace.json` versions are written only by the release workflow
- [ ] Docs updated where relevant (root `README.md`, `docs/SETUP-AND-USAGE.md`, or an ADR for a significant decision)

After merge, confirm the **release** run went green and that `main` has the new
`chore(release): vX.Y.Z` commit and matching tag — a clean merge does not by itself
mean a version published (see `.claude/skills/library-contribution/SKILL.md`).

## Validator output
```
<paste `python scripts/validate_plugin.py` output>
```

---
cc @eblouin876
