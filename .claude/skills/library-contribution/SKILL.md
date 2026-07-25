---
name: "library-contribution"
description: "The house rules for changing THIS repository — the eblouin-plugins marketplace and its dev-lifecycle plugin. Use this skill WHENEVER a change to this library is being made or proposed: adding or editing a skill, a shared doctrine, a reference, a template block, a recipe, a workflow, a script, or the docs; reviewing someone else's change to it; or planning one (\"should this be a skill or a reference\", \"where does this go\", \"add a skill for X\", \"update the plugin\"). It covers where each kind of content lives, the frontmatter and header conventions the validator enforces, which docs must move with the change, and the release contract every PR must satisfy — the version bump the PR itself must carry, the `release:*` label CI checks it against, and the post-merge tag that publishes it. Not for work in projects that merely *use* the plugin — those use the lifecycle skills themselves."
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
- **The PR carries the version bump.** Nothing bumps the version after merge — you write the new
  version into both manifests as part of the change, and CI checks it against the PR's label. A PR
  that ships something to consumers without a bump is incomplete. See "The release contract" below.
- **Every PR carries exactly one `release:*` label.** `release:major` / `release:minor` /
  `release:patch`, or `release:none` for a change consumers never see. There is no default —
  an unlabeled PR fails the `version-bump` check.
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
- The three version fields (plugin manifest, marketplace metadata, marketplace plugin entry) must
  all carry the same value — a half-applied bump is a hard error.
- Template blocks: `versions-pinned-to:` must resolve to a real file.
- References/templates/recipes: a `last-verified:` header, updated when you touch the content.

## The release contract

**The bump lives in the PR.** Nothing writes to `main` after merge — `main`'s ruleset ("changes
must be made through a pull request") stays absolute, with no bot commits and no bypass. The PR
that changes the library is also the PR that raises its version; `release.yml` merely tags the
merge commit with the version `main` already carries, so the bump and the tag can never disagree.

**Doing the bump — the last thing before you mark the PR ready:**

1. Read the current version from `main` (not from your branch — it may be behind):
   `git show origin/main:plugins/dev-lifecycle/.claude-plugin/plugin.json | jq -r .version`
2. Decide the level (below) and compute the new version from *that* base.
3. Write it in **all three** places, which must always agree:
   - `plugins/dev-lifecycle/.claude-plugin/plugin.json` → `.version`
   - `.claude-plugin/marketplace.json` → `.metadata.version`
   - `.claude-plugin/marketplace.json` → the `dev-lifecycle` entry's `.version`
4. Apply the matching `release:*` label to the PR.
5. Run `python scripts/validate_plugin.py` (catches a half-applied bump) and, if you want the
   exact CI check locally, `python scripts/check_version_bump.py --base <main's> --head <yours>
   --level <level>`.

**Choosing the level:**

- `release:major` — a breaking change for consumers: a skill removed or renamed, a shared doctrine
  whose meaning inverts, a template contract that no longer composes with existing blocks.
- `release:minor` — new capability or a default-behavior change: a new skill, a new shared
  doctrine, a new template block or recipe, a workflow-behavior change consumers will notice.
- `release:patch` — corrections that don't change what the library does: typos, clarifications,
  a reference refreshed to a new library version, a validator fix.
- `release:none` — nothing reaches consumers: this repo's own CI, its PR template, its
  `CLAUDE.md`, or this skill. The version must **not** move; CI enforces that too.

**What CI enforces** (`version-bump.yml`, re-run on every push *and* every label change):

- Exactly one `release:*` label — no default, no guessing.
- The head version is exactly what the label implies, computed from the PR's own base.
- All three version fields agree.

**Bump last, and expect the occasional rebase.** Two open PRs both bumping from the same base will
collide — the second to merge fails the check with "base moved under you." That's the intended
behavior: rebase, recompute from the new base, re-push. Because the bump is the last thing you do,
this is a one-line conflict rather than a rewrite.

## After the merge

Confirm the **release** run went green and that the `vX.Y.Z` tag now exists — that tag is the
published artifact consumers pin to. If the PR was `release:none`, the job logs "already tagged —
nothing to release," which is the correct outcome, not a failure.

If versions have drifted (merges that published nothing), reconcile deliberately in one PR and say
so in the description; don't let the next bump paper over the gap silently.

## PR conventions

- Fill `.github/pull_request_template.md` — summary, what changed/why, the decision log (judgment
  calls a reviewer should sanity-check rather than assume), the checklist, and the validator output.
- **`cc @eblouin876`** in the description, per this repo's `CLAUDE.md` — the template ends with it.
- Paste the real validator output. "It passes" is not evidence.

## What this skill does NOT do

- Govern work in repos that merely *use* the plugin — those run the lifecycle skills themselves.
- Replace `template-author` / `recipe-author` for authoring a block or recipe — it says where they
  go and what must ship with them; those skills own the format.
- Authorize pushing anything directly to `main` — including a version bump or a tag fix — to route
  around a failed check. `main` changes only through a pull request.
