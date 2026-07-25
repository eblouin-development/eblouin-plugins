# CLAUDE.md

Guidance for any Claude instance working in this repository.

## Changing this library

Any change to this repo — a skill, a shared doctrine, a reference, a template
block, a recipe, a workflow, or the docs — follows the **`library-contribution`**
skill (`.claude/skills/library-contribution/SKILL.md`). Invoke it whenever you
are making, planning, or reviewing a change here. It covers where each kind of
content lives, the conventions `scripts/validate_plugin.py` enforces, the
`release:*` label every PR must carry, and the fact that the merged version is
the release — nothing runs after merge to fix it.

Two rules from it that are easy to violate before reading it:

- **The PR carries the version bump** — compute it from `main`'s current version
  and write it into `plugin.json` and both version fields in
  `marketplace.json`, as the last thing before marking the PR ready. Nothing
  bumps it after merge.
- **Every PR carries exactly one `release:major|minor|patch|none` label** — CI
  checks the bump against it; there is no default.

## Pull requests

When you create a pull request, **tag @eblouin876 in the PR description** so the
repository owner is notified and can pull it up quickly (e.g. on mobile). Add a
line such as:

> cc @eblouin876

This applies to every PR opened in this repo, regardless of which Claude
instance or workflow creates it.
