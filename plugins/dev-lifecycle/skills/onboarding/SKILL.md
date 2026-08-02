---
name: "onboarding"
description: "Bring an EXISTING repository into the firm so you can work it through the pipeline — detecting whether you own it (full setup) or are a guest (zero-footprint setup). Use this skill WHENEVER you point Claude at a repo that already exists and want it firm-ready: \"onboard this repo\", \"set me up to work on this project\", \"I have access to this repo, get it wired up\", \"bring this client repo in\", or when you clone someone else's project and want to work it your way without changing it. For a brand-new repo you own, use scaffolding instead. The defining decision this skill makes is owned vs guest, because guest repos get NOTHING committed — no Claude footprint anywhere, including in commit and PR attribution."
---

# Onboarding

Take a repository that already exists and make it workable through the firm's pipeline. The first and most important thing this skill decides is **owned vs guest**, because the two paths are completely different:

- **Owned** (you have admin / can install the GitHub App and commit): set up the in-repo pipeline, same as a fresh scaffold, plus inventory the existing stack and fill the reference library.
- **Guest** (a repo you don't own but have access to): leave **zero footprint**. Nothing committed, no Claude anywhere in the record — not in files, not in commit trailers, not in PR bodies, not in branch names. The firm operates *around* the repo, not inside it.

## Core rules

- **Decide owned vs guest first, and be conservative.** If you can't confirm you own it (or you're working someone else's project), treat it as **guest**. When unsure, ask.
- **Guest means invisible.** On a guest repo, Claude leaves no trace: config lives in untracked local files, work is authored as the user, and every commit/PR is scrubbed of AI attribution before it leaves the machine (see the guest workflow). This is a hard requirement, not a preference.
- **Detect and conform.** Read the existing stack, conventions, and CI before changing or adding anything. Never impose the firm's structure on a repo that already has one.
- **Intake only, no composing.** This skill wires the pipeline around the repo as it already is; it never composes the starter kit's blocks/components into it. Moving an owned repo's app code onto the kit is `modernization`'s job.
- **Work context-efficiently.** Inventory from manifests/lockfiles, not by reading the tree. See `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`.
- **Fill the reference library eagerly.** Inventory the repo's significant direct dependencies; for any framework/library with no reference yet, generate one grounded in **current official docs** and open a PR to *your* plugin repo (this works in both modes — it touches your marketplace, never the onboarded repo). Skip transitive utilities without real idioms.

## Workflow

### 1. Determine the mode
Check ownership/permissions: can you commit to the default branch (admin rights)? Yes → **owned**. No, or it's someone else's project → **guest**. State which mode you're in and why before proceeding.

### 2a. Owned path
Run the same wiring as `scaffolding`, adapted to what's already there:
- Generate a lean `CLAUDE.md` from the detected stack, layout, and commands (see `scaffolding`).
- Wire the pipeline exactly as `scaffolding` does: copy the firm's workflow templates from `${CLAUDE_PLUGIN_ROOT}/assets/workflows/` (`security.yml` + `epic-checkoff.yml`) into `.github/workflows/`. These are self-contained — no owner substitution, no secret, no plugin. There is no Action to wire: the repo picks up and reviews work exclusively through the interactive `coding-session` skill. Then add CI gates (`${CLAUDE_PLUGIN_ROOT}/references/devops/cicd.md`) — PR gates run on ready-for-review PRs only, so any gate you add carries the `ready_for_review` trigger type and a job-level draft guard — plus branch protection requiring CI + review, and a committed `.claude/settings.json`. Conform to any CI that already exists rather than replacing it; if it gates every push, leave it alone and note the difference (the local gate applies either way).
- In the committed `.claude/settings.json`, include the **cloud plugin-freshness** block (marketplace `autoUpdate` + a `SessionStart` hook that runs `claude plugin marketplace update <repo> && claude plugin update dev-lifecycle@<repo>`) — exactly as `scaffolding` does — so cloud sessions don't freeze the firm plugin at an old version behind the environment snapshot. See `Keeping cloud sessions current` in `docs/SETUP-AND-USAGE.md`. **Owned path only** — never on the guest path.
- Confirm the merge gate is human (`${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`).

### 2b. Guest path (zero footprint)
Set up so nothing about Claude touches the repo:
- **Untracked local config only.** Put project notes in `CLAUDE.local.md` and any settings in `.claude/settings.local.json`, and add both to **`.git/info/exclude`** (a per-clone ignore that is never committed and invisible to the repo owner) — never to the repo's tracked `.gitignore`.
- **Blank attribution in your user settings** (`~/.claude/settings.json`): `{ "attribution": { "commit": "", "pr": "" } }`. User-scoped, so it applies with zero footprint on the repo.
- **Author as yourself.** Work runs under your own git identity and your own `gh` auth (on your home server/local, or a Web sandbox configured with your credentials), so commits are authored by you and any PR is opened by your account — no Claude App, no bot identity. If you only have read access, fork and PR from the fork.
- **Neutral branch names** (`feature/…`, `fix/…`) — never `claude/…`.
- **Scrub before every push.** Before pushing or opening a PR, grep the outgoing commit messages and the PR body for `Claude`, `Co-Authored-By`, and `Generated with`, and strip any that appear (amend/rewrite). The blanked attribution is the first line of defense; this scrub is the guarantee.
- **No in-repo Action.** The pipeline runs from your side via an interactive `coding-session`; you drive builds yourself. Get changes to merge-ready against *the repo's own standards and CI*, then hand the PR to whoever owns the merge.

### 3. Hand off
State the mode, what was set up (and, for guest, confirm nothing was committed to the repo), and any references generated for your plugin. The next move is a plan (`planning`), then a `coding-session` to build it — on both owned and guest repos, you drive it yourself.

## What this skill does NOT do
- Leave any Claude footprint on a guest repo — no committed files, no attribution in commits/PRs/branches.
- Commit to or install the GitHub App on a repo you don't own.
- Replace a repo's existing structure or CI instead of conforming to it.
- Assume ownership when it's unconfirmed — default to guest.
