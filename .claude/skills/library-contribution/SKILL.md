---
name: "library-contribution"
description: "The house rules for changing THIS repository — the eblouin-plugins marketplace and its dev-lifecycle plugin. Use this skill WHENEVER a change to this library is being made or proposed: adding or editing a skill, a shared doctrine, a reference, a template block, a recipe, a workflow, a script, or the docs; reviewing someone else's change to it; or planning one (\"should this be a skill or a reference\", \"where does this go\", \"add a skill for X\", \"update the plugin\"). It covers where each kind of content lives, the frontmatter and header conventions the validator enforces, which docs must move with the change, and the release contract every PR must satisfy — the `release:*` label that drives the version bump, the post-merge check that the bump actually landed, and the branch-rule bypass it depends on. Not for work in projects that merely *use* the plugin — those use the lifecycle skills themselves."
---

# Contributing to this library

This repo is both a **marketplace** (`.claude-plugin/marketplace.json`) and the **dev-lifecycle
plugin** it publishes. Every change here ships to every installed consumer on the next
`/plugin marketplace update`, so a change is not done when the diff looks right — it's done when
the validator passes, the docs match, and a version actually got published.

Changes go through the same plan → PR → review → merge → release pipeline as any other repo. What
follows is what's specific to *this* one.

## Core rules

- **Run the validator before every push.** `python scripts/validate_plugin.py` must pass with **0
  warnings**. It's deterministic, needs no auth or network, and it catches exactly what Claude Code
  rejects at install time — invalid manifests and invalid `SKILL.md` frontmatter. A red validator is
  a broken install for every consumer, not a lint nit.
- **Never hand-edit a version field.** `plugins/dev-lifecycle/.claude-plugin/plugin.json` and
  `.claude-plugin/marketplace.json` carry the semver, and **only the release workflow writes them**.
  A version bump in a feature PR collides with the workflow's own commit and desynchronizes the tag
  history. If a version looks wrong, fix the release process, not the file.
- **Every PR carries exactly one `release:*` label.** `release:major` / `release:minor` /
  `release:patch` — this is what the release workflow reads to decide the bump. Unlabeled defaults
  to patch, which silently under-versions a feature. See "The release contract" below.
- **Docs move with the change.** The layout tree in `README.md`, the feature summary in
  `docs/SETUP-AND-USAGE.md`, and an ADR in `docs/adr/` for a significant decision. A new shared
  doctrine or a new skill that isn't in the docs is invisible to the person deciding whether to use it.
- **Keep the always-on surface small.** Skill `description` fields sit in context for *every*
  request in *every* consumer session. Descriptions earn their length by improving trigger
  accuracy; body content and `references/` are loaded on demand. This is the progressive-disclosure
  rule the plugin preaches — it applies hardest to the plugin itself.
- **Check the release landed after merge.** The merge is not the end of the change; the published
  version is. See "After the merge".

## Where things go

| Adding… | Goes in | Also required |
| --- | --- | --- |
| A lifecycle skill | `plugins/dev-lifecycle/skills/<name>/SKILL.md` | frontmatter `name` **exactly** matches the directory name; README + `docs/SETUP-AND-USAGE.md` skill count/list updated |
| Cross-skill doctrine (applies to several skills) | `plugins/dev-lifecycle/shared/<name>.md` | linked from the skills that follow it, and from the README layout tree; cross-link the sibling doctrines |
| Library/tool documentation | `plugins/dev-lifecycle/references/<domain>/<lib>.md` | a current `last-verified:` header — the freshness audit reads it |
| A composable template block | `plugins/dev-lifecycle/templates/<layer>/<name>/README.md` | the composition contract (needs/exposes) per `_TEMPLATE-README.md`; `versions-pinned-to:` must resolve; use `template-author` |
| A feature recipe | per `recipe-author`'s layout | `last-verified:`; use `recipe-author` |
| A workflow shipped **to consumer repos** | `plugins/dev-lifecycle/assets/workflows/` | it's an asset, not this repo's CI |
| CI **for this repo itself** | `.github/workflows/` | keep it distinct from the shipped assets above |

If a change doesn't obviously belong to one row — most often "is this a skill or a shared doctrine?"
— the test is reuse: **one job, one workflow → skill; a rule several skills must follow →
`shared/`.** Doctrine that lives inside a skill can't be pointed at by the others.

## Conventions the validator enforces

Don't discover these from a red CI run:

- A skill's frontmatter `name` must equal its directory name.
- `marketplace.json` needs `name`, an object `owner`, and a non-empty `plugins` array; each plugin
  entry needs `name`, `source`, `version`, `description`.
- Every path a skill references must exist — the validator cross-checks them, so a
  `${CLAUDE_PLUGIN_ROOT}/...` pointer to a file you haven't written yet is a hard error.
- Template blocks: `versions-pinned-to:` must resolve to a real file.
- References/templates/recipes: a `last-verified:` header, updated when you touch the content.

## The release contract

Merging is what publishes. `.github/workflows/release.yml` runs on PR close, reads the `release:*`
label, bumps both version files, commits `chore(release): vX.Y.Z [skip ci]` to `main`, and pushes
the `vX.Y.Z` tag. Consumers pick it up on `/plugin marketplace update`.

**Choosing the level:**

- `release:major` — a breaking change for consumers: a skill removed or renamed, a shared doctrine
  whose meaning inverts, a template contract that no longer composes with existing blocks.
- `release:minor` — new capability or a default-behavior change: a new skill, a new shared
  doctrine, a new template block or recipe, a workflow-behavior change consumers will notice.
- `release:patch` — corrections that don't change what the library does: typos, clarifications,
  a reference refreshed to a new library version, a validator fix.

**Set the label on the PR before merge.** Unlabeled defaults to patch — fine for a fix, wrong for
anything in the first two rows, and not retroactively correctable without a manual tag.

**The branch-rule dependency.** `main` is governed by a ruleset whose "Require a pull request
before merging" rule applies to *every* pusher, so the release job's push is rejected with `GH013`
unless its identity is on the ruleset's **bypass list**. `GITHUB_TOKEN` cannot be granted that
bypass — a repo-role bypass doesn't apply to it — so the workflow mints a **GitHub App token**
when the app is configured:

- Repo variable `RELEASE_APP_ID` and secret `RELEASE_APP_PRIVATE_KEY`, from a GitHub App installed
  on this repo with **Contents: write**.
- That app added to Settings → Rules → Rulesets → the `main` ruleset → **Bypass list**.

Without both halves the job falls back to `GITHUB_TOKEN`, the push is rejected, and **no version is
published even though the PR merged cleanly**. That failure is silent from the PR's point of view,
which is exactly why the post-merge check below exists.

## After the merge

A merged PR is not a published change. Confirm both:

1. The **release** run for that merge is green — not just `validate` and `changes`.
2. `main` has a new `chore(release): vX.Y.Z` commit and the matching `vX.Y.Z` tag exists.

If the run failed with `GH013: Repository rule violations found` on `refs/heads/main`, the bypass
is the cause — fix the app/bypass configuration rather than pushing the bump by hand. If versions
have already drifted (merges that published nothing), reconcile deliberately in one PR and say so
in the description; don't let the next successful run paper over the gap silently.

## PR conventions

- Fill `.github/pull_request_template.md` — summary, what changed/why, the decision log (judgment
  calls a reviewer should sanity-check rather than assume), the checklist, and the validator output.
- **`cc @eblouin876`** in the description, per this repo's `CLAUDE.md` — the template ends with it.
- Paste the real validator output. "It passes" is not evidence.

## What this skill does NOT do

- Govern work in repos that merely *use* the plugin — those run the lifecycle skills themselves.
- Replace `template-author` / `recipe-author` for authoring a block or recipe — it says where they
  go and what must ship with them; those skills own the format.
- Authorize hand-editing versions or hand-pushing a bump to `main` to route around a failed release.
