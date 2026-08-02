# Setup & Usage — the development firm

How to stand up this plugin and run your projects through it: delegate work to agents, review and merge yourself, from any device, whether or not the road has good signal.

This is the operator's manual. The plugin's internal conventions are in the repo `README.md`; this doc is about *using* it.

---

## What you've got

A Claude Code plugin marketplace (`eblouin-plugins`) containing one plugin (`dev-lifecycle`) with:

- **24 skills** spanning the whole project lifecycle — `technical-proposal` → `product-planning` → `scaffolding` / `onboarding` → `planning` → `ui-exploration` / `design-system` → `backend` / `frontend` / `mobile` → `data` → `copywriting` → `testing` → `code-review` → `devops` / `infrastructure` → `dependency-maintenance` / `security-audit` → `documentation` / `debugging` / `walkthrough` — plus `coding-session`, the conductor that runs the whole plan → build → review → merge loop for you, and `template-author` / `recipe-author`, the two meta-skills for extending the plugin's own starter kit (below).
- A **self-extending reference library** of per-library, version-aware docs (React, TypeScript, MUI, Tailwind, HTMX, FastAPI, Pydantic, SQLAlchemy, Postgres, Django, plus testing/devops/security/docs/debugging) — now growing a **starter-kit direction** alongside it: composable template blocks and feature recipes that `template-author`/`recipe-author` add, pinned to a shared compatibility matrix and a standardized security baseline (see the root `README.md`'s "Reference library conventions" for the full model).
- A **token-efficiency doctrine** every skill follows, a **worker-cadence doctrine** for how an orchestrator watches the subagents it dispatches (backstop cadence, not polling — used by `coding-session`), a **parallelization doctrine** that has a session work out the dependency graph and run independent tracks concurrently by default (in isolated worktrees), a **CI-convergence doctrine** where the session owns the gate → fix → review → fix loop to green — verified in the container while the PR is a draft (CI runs on ready-for-review PRs only), then flipped to ready, which starts CI and hands the PR to you, with the post-flip run watchdogged and triaged before the PR's state changes, a **context-continuity doctrine** that has a long session hand off to itself at ~75% of its window — flush state to the issue/PR, refresh a continuation brief, reload the skills in play, resume without involving you — an **issue-cross-check doctrine** that runs every generated plan against the open backlog (what it closes, duplicates, or conflicts with) before you're asked to approve it, and a shared **definition-of-done** that is the merge-ready bar.
- Four automations: **semver release** on merge, a weekly **freshness audit** that flags stale references (and the rest of the starter kit), a weekly **coverage audit** that PRs references for fleet libraries still undocumented, and **epic checkoff** — a per-project workflow that ticks an epic's checkbox when a stage/feature issue closes on merge.

The spine is GitHub: issues are the task queue, PRs are work-in-review, CI is QA, **merge is yours**.

---

## One-time setup

### 1. Publish the marketplace
Push this repo to GitHub (private is fine):

```bash
cd eblouin-plugins
git init && git add . && git commit -m "firm plugin v0.1.0"
gh repo create <owner>/eblouin-plugins --private --source=. --push
```

### 2. Install the plugin
In Claude Code:

```
/plugin marketplace add <owner>/eblouin-plugins
/plugin install dev-lifecycle@eblouin-plugins
```

Turn on auto-update in `/plugin` → Marketplaces (or add `extraKnownMarketplaces` with `autoUpdate: true` to settings). Because the plugin is **user-scoped**, all 24 skills now follow you into *every* repo you open — including ones you don't own — with no per-repo install.

> **Cloud sessions are different — see [Keeping cloud sessions current](#keeping-cloud-sessions-current).** The `/plugin` UI toggle and `~/.claude/settings.json` don't carry into Claude Code on the web, so cloud sessions can silently pin an old plugin version.

### 3. Your user settings (`~/.claude/settings.json`)
Applies everywhere you work, zero repo footprint:

```json
{
  "attribution": { "commit": "", "pr": "" },
  "env": { "DISABLE_NON_ESSENTIAL_MODEL_CALLS": "1" }
}
```

The blanked `attribution` is what keeps Claude out of commit/PR bylines on guest repos; `DISABLE_NON_ESSENTIAL_MODEL_CALLS` trims auxiliary model calls for cost.

### 4. Secrets for the automations
The firm authenticates with a Claude subscription token (OAuth), not an Anthropic API key.
- **Plugin repo:** add `CLAUDE_CODE_OAUTH_TOKEN` as a repo secret so the scheduled freshness and coverage audits can run. No `ANTHROPIC_API_KEY` anywhere.
- **Each owned project repo:** work is picked up and built through the interactive `coding-session` skill — there's no per-repo Action secret to wire.

---

## Bringing a repo into the firm

### A repo you own → `scaffolding`
> "Scaffold this repo" / "set up a new project for X"

It detects (or defaults to Python back / TypeScript front), lays down structure and tooling, writes a **lean `CLAUDE.md`** (the thing that keeps every later task cheap), and wires the pipeline: CI gates, branch protection requiring CI + a review, and a committed `.claude/settings.json` the cloud sessions and scheduled-audit Actions inherit. Work is picked up and built exclusively through the interactive `coding-session` skill — there is no GitHub Action to wire per repo. Scaffolding now **composes a runnable monorepo** from the starter kit in the same pass — blocks, catalog components, and their doc fragments, not just bare structure — see `docs/STARTER-KIT.md` for the full catalog.

### A repo you don't own → `onboarding` (guest mode)
> "Onboard this client repo without touching it"

Zero footprint: local config goes in untracked files excluded via `.git/info/exclude`, work is authored as you under your own `gh` auth, commits/PRs carry no Claude attribution (blanked + scrubbed before push), branches are neutrally named, and there's no in-repo Action. You drive the builds yourself; the repo owner sees only your work, as you.

---

## Daily workflow

### A whole new product (greenfield)
1. **`technical-proposal`** — "What should we build this with, and what will it take?" Recommends the stack + architecture, justifies it, and gives an honest cost/timeline. The build/no-build decision.
2. **`scaffolding`** — creates and initializes the repo (`gh repo create`, structure, tooling, lean `CLAUDE.md`, pipeline wiring). This is what gives `product-planning` a place to file its epic.
3. **`product-planning`** — "Plan out the whole product." Produces the north star: vision, architecture/stack decisions, and a **staged roadmap** as a GitHub **epic + milestones + an ADR** in the repo. It stops there — no build.
4. **Per stage**, repeat: **`planning`** reads the epic + the stage stub, you go back and forth until you approve, then it files the stage issue as a **sub-issue of the epic** (with an `Epic: #<n>` marker and the issue number on the epic's checklist line) under its milestone. You then hand that issue to a **`coding-session`**, which builds it step by step on one branch, reviews each step internally, and — when the final whole-PR review is clean — flips the PR to ready and pings **you** to review, sign off, and **merge**. On merge the stage issue closes, and the **epic-checkoff** workflow ticks its box in the epic (and GitHub's native sub-issue rollup advances the epic's progress bar) — so the roadmap stays current without you touching it. Then **`devops`** deploys (a home beta server, or your chosen target) → next stage.

Because every stage plan references the epic and ADR, the whole product stays aligned to the north star.

> **Retrofitting an older epic.** Epics created before the epic-checkoff wiring have no `Epic:` markers or issue numbers on their lines, so their boxes won't auto-tick. Migrate one in place with the bundled one-shot — dry-run first, then `--apply`:
> ```bash
> plugins/dev-lifecycle/assets/scripts/retrofit-epic.sh <owner/repo> <epic#> [stage-issue#…]
> ```
> It adds the markers and numbers, links the sub-issues, and ticks any stage that's already merged. Idempotent, so re-running is safe.

### A feature or fix on an existing repo
1. **`planning`** — talk it through; before you're asked to approve, a Sonnet subagent cross-checks the draft against the repo's open issues and PRs and reports what the work closes, duplicates, or conflicts with. On your approval it files the issue.
2. **`coding-session`** picks up the issue and runs the whole loop from an interactive thread using local subagents — it scopes (or picks up the epic/issue), marks it in-progress, then builds the whole feature self-directed: steps land as commits on one branch under one draft PR, each reviewed internally before the next, with a decision log kept as it goes. You're in the loop at scope approval (where you also approve any mid-build checkpoints — manual test gates, known decision points; default none), at genuine escalations, and at the end, when a clean whole-PR review flips the PR ready and it pings you with the decision log to review, sign off, and **merge**. CI runs on ready-for-review PRs only, so the session verifies the branch against the pipeline's own checks *in the container* while the PR is a draft and flips to ready only once that's green — the flip is what starts CI, and it should pass. If that run comes back red the session triages it before touching anything: a failure the change caused sends the PR back to draft to be fixed and re-flipped, while a failure it didn't (Actions limits, a runner outage, an expired secret) leaves the PR ready and tells you what's wrong. One feature, one PR, no incremental merges. A long session manages its own context: at ~75% of the window it flushes state to the issue and PR, refreshes a continuation brief pinned as a comment on the draft PR, reloads the skills it's working under, and carries on — you shouldn't be able to tell it happened.

This is the only path — owned and guest repos both drive the build the same way, through a `coding-session`. There is no separate headless pipeline to hand off to.

### The one rule that never changes
Agents plan, build, review, and get to green — **you approve the plan and you merge.** No agent ever merges.

### From any device / bad signal
Once a `coding-session` is dispatched to a cloud sandbox (Claude Code on the web), it runs server-side — independent of your connection. Kick it off, close the laptop, lose Starlink; it keeps going. Check status, review the diff, and merge from the Claude mobile app's remote-control panel or GitHub mobile. Heavier or private work can run on **your home server** over Tailscale and be reviewed the same way.

---

## The reference library

References are **per-library and version-aware**, loaded only when that library is detected — so a task pulls in exactly the docs it needs and nothing more.

**Four ways the library grows/stays fresh** (all land as PRs to the plugin repo that *you* review and merge — nothing enters canon silently, and everything is grounded in current official docs, never recall):
- **Onboarding** inventories a repo's significant dependencies and generates any missing references in a batch.
- **Build-time** fills a gap on the spot if a skill meets a library with no reference.
- **The weekly freshness audit** compares each reference's `versions-covered` / `last-verified` header against the library's current release and flags stale ones in a tracking issue. Its remit now extends past references to the whole starter kit — template blocks/components, feature recipes, the compatibility matrix, and doc-fragment drift (`just docs-check`) — so staleness anywhere in the kit is caught the same way. See `docs/STARTER-KIT.md` ("How it stays fresh") for the full breakdown.
- **The weekly coverage audit** reads `.github/fleet-repos.txt`, inventories each fleet repo's significant dependencies, and PRs a grounded reference for any framework in use across the fleet but not yet documented — the automatic backstop to the build-time path. (Requires a `FLEET_READ_TOKEN` secret with read access to the fleet repos.)

---

## Maintaining the plugin

The plugin is itself a project in the firm — improve it through the same loop, governed by the repo's own **`library-contribution`** skill (`.claude/skills/library-contribution/SKILL.md`), which any Claude instance invokes whenever a change here is made, planned, or reviewed:
1. A change (new skill, edited reference, freshness fix) comes in as a **PR** to `eblouin-plugins`.
2. **The PR carries its own version bump**, labelled `release:major|minor|patch` (or `release:none` for changes consumers never see). `version-bump.yml` checks the bump matches the label, on every push and every label change.
3. CI validates; **you review and merge**.
4. The merged version *is* the release — nothing runs after merge, and nothing is ever pushed to `main`.
5. Your installs pick it up on auto-update, or run `/plugin marketplace update`.

Roll back by pointing the catalog at a prior commit. There is no automatic tagging: if you want a `vX.Y.Z` tag to pin to, create it by hand on the merge commit (`git tag v1.1.0 <sha> && git push origin v1.1.0`).

### Why the bump lives in the PR

`main` is governed by a ruleset requiring every change to go through a pull request, and that rule applies to **every** pusher — including a release job. A workflow that bumped the version on `main` after merge would need a bypass (`GITHUB_TOKEN` cannot be granted one, so it would mean a GitHub App token and an entry on the ruleset's bypass list), and when that isn't in place the push is rejected with `GH013` and **nothing publishes even though the PR merged cleanly** — a silent failure from the PR's point of view.

Putting the bump in the PR removes the problem rather than working around it: `main` only ever changes through a reviewed pull request, no bot has write access to it, and the version is visible in the diff you approve. With no post-merge job at all, there is nothing left that can fail silently between merge and release. The cost is that two PRs bumping from the same base collide — the second fails the check and rebases, which is why the bump is the last thing you do before marking a PR ready.

---

## Keeping cloud sessions current

**The trap:** a release bumps the version fine, but Claude Code on the web keeps serving an **old** plugin — new skills never appear even in a "fresh" session started after the release.

**Why:** cloud environments run a setup script (`claude plugin marketplace add …` + `claude plugin install …`) **once**, then [snapshot the filesystem](https://code.claude.com/docs/en/claude-code-on-the-web#environment-caching) — including `~/.claude/plugins` — and reuse it for every later session, **skipping the setup script**. So the plugin is frozen at whatever version was installed the first time. Worse, `add`/`install` are **no-ops against existing state** ("already on disk" / "already installed"), so re-running them never upgrades. The snapshot only rebuilds when you **edit the setup script** (or ~7-day expiry). GitHub Actions are immune — each run gets a clean runner and clones the marketplace fresh.

There are two independent knobs; use the one that matches the scope:

### Environment-level (covers every repo sharing that cloud environment)
Fix the environment's **setup script** so it actually upgrades. `add`/`install` alone don't — add the `update` commands:

```bash
claude plugin marketplace add <owner>/<repo> || true
claude plugin marketplace update <repo>        # pull marketplace → latest main
claude plugin install dev-lifecycle@<repo> || true
claude plugin update dev-lifecycle@<repo>       # upgrade the installed plugin
```

(Substitute `<owner>/<repo>` with the actual account and repo hosting the marketplace — `eblouin-development/eblouin-plugins` for this repo, unrenamed.)

Editing the setup script **is** what un-sticks the current stale snapshot (it forces one rebuild). This is the low-effort fleet-wide fix: **you do not copy anything per-repo** — every project that runs in that default environment picks up the new version, with staleness bounded to the ~7-day cache window.

### Repo-level (tighter freshness for one repo)
Commit a `.claude/settings.json` (which *does* carry into cloud sessions, unlike user settings) that registers the marketplace for **auto-update** and, as a belt-and-suspenders guarantee, refreshes on session start:

```json
{
  "extraKnownMarketplaces": {
    "eblouin-plugins": {
      "source": { "source": "github", "repo": "eblouin-development/eblouin-plugins" },
      "autoUpdate": true
    }
  },
  "hooks": {
    "SessionStart": [
      { "matcher": "startup", "hooks": [ { "type": "command", "command": "{ command -v claude >/dev/null 2>&1 && claude plugin marketplace update eblouin-plugins && claude plugin update dev-lifecycle@eblouin-plugins; } >/dev/null 2>&1 || true", "timeout": 120 } ] },
      { "matcher": "resume",  "hooks": [ { "type": "command", "command": "{ command -v claude >/dev/null 2>&1 && claude plugin marketplace update eblouin-plugins && claude plugin update dev-lifecycle@eblouin-plugins; } >/dev/null 2>&1 || true", "timeout": 120 } ] }
    ]
  }
}
```

`autoUpdate` is the native path (refreshes and prompts `/reload-plugins` in-session where honored); the SessionStart hook is the CLI-level fallback (applies on the next session, since `plugin update` needs a restart to take effect). This repo ships exactly this block. Do **not** hand-copy it into every project — the natural place to standardize it is `scaffolding`/`onboarding`, so every firm repo gets it when it's brought into the pipeline.

**Bottom line:** for "all my projects," fix the **environment setup script** once — that's the fleet-wide lever. Reserve the repo-level `.claude/settings.json` block for repos where you can't tolerate the ~7-day window (like this one).

---

## Which skill for what

| I want to… | Skill |
|---|---|
| Drive a feature/epic end to end (plan → build → review → merge) | `coding-session` |
| Decide whether/what/what-it-costs to build | `technical-proposal` |
| Plan a whole product and its stages | `product-planning` |
| Plan one feature/fix and kick off the build | `planning` |
| Start a new repo I own | `scaffolding` |
| Work a repo I don't own, invisibly | `onboarding` |
| Build server-side code | `backend` |
| Build UI | `frontend` |
| Explore new UI and turn it into a spec | `ui-exploration` |
| Define/enforce design tokens | `design-system` |
| Write product/UI copy | `copywriting` |
| Write/deepen tests | `testing` |
| Review a diff or a pull request | `code-review` |
| Containerize, wire CI, deploy (incl. a home server) | `devops` |
| Provision & maintain hosts/infra (AWS, home, Tailscale) | `infrastructure` |
| Keep dependencies current / patch a CVE | `dependency-maintenance` |
| Audit a whole project's security | `security-audit` |
| Run every audit lane on a whole repo in one pass | `repo-audit` |
| Seed data & build reports | `data` |
| Write docs / ADRs / API reference | `documentation` |
| Write a client status update, release notes, or handoff package | `client-comms` |
| Root-cause a failure | `debugging` |
| Understand code / a PR (read-only explainer) | `walkthrough` |

---

## What's not done yet

- **Testing pass:** each skill should get the skill-creator trigger-eval in Claude Code before you rely on it heavily, and the `${CLAUDE_PLUGIN_ROOT}/shared/...` reference paths should be confirmed to resolve once the plugin is installed (fallback: copy shared files into each skill's folder).
- The PR-carried semver bump (`version-bump.yml` + the `library-contribution` skill) is a solid v1 to refine in practice. The `freshness-audit.yml` prompt's extension from references-only to the whole starter kit (templates, recipes, the matrix, doc drift — see `docs/STARTER-KIT.md`) lands this stage, closing out that part of the v1 gap.
