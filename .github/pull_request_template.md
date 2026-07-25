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
- [ ] Exactly one `release:major` / `release:minor` / `release:patch` / `release:none` label is set on this PR
- [ ] This PR carries the version bump the label implies, computed from `main`'s current version, in all three fields (`plugin.json`, `marketplace.json` `metadata.version`, and the `dev-lifecycle` entry) — or the version is untouched for `release:none`
- [ ] Docs updated where relevant (root `README.md`, `docs/SETUP-AND-USAGE.md`, or an ADR for a significant decision)

There is no post-merge release job: the version this PR carries is published the
moment it merges, and can only be corrected by another PR (see
`.claude/skills/library-contribution/SKILL.md`).

## Validator output
```
<paste `python scripts/validate_plugin.py` output>
```

---
cc @eblouin876
