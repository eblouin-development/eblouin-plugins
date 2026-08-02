---
name: "scaffolding"
description: "Bootstrap a new or empty repository into the firm by COMPOSING it from the starter kit — the monorepo skeleton, template blocks (backend/frontend/mobile/infra), catalog components, and feature recipes under ${CLAUDE_PLUGIN_ROOT}/templates/ and references/recipes/ — plus the CI gates and branch protection so the repo is ready to build through the interactive coding-session's plan → PR → review → merge loop. Use this skill WHENEVER starting a greenfield project or standing up a repo you own: \"start a new project\", \"set up the repo\", \"scaffold this\", \"initialize the backend/frontend\", \"get this repo ready\", or as Stage 0 right after a product plan. This is for repositories you OWN and can commit to — for bringing in a repo you don't own without a footprint, use the onboarding skill instead. Backend defaults to FastAPI, frontend to vite-spa (or nextjs for SSR), but it detects and conforms to any stack already present or intended."
---

# Scaffolding

Turn an empty (or barely-started) repository you own into a project that runs on the firm's pipeline. Scaffolding decides the structure, installs the tooling, writes the persistent project context, and wires the automation — so that from here on, work flows through the normal loop: a plan becomes an issue, the `coding-session` skill builds it and opens a PR, review takes it to merge-ready, CI gates it, and you merge.

The highest-leverage thing this skill produces is a lean `CLAUDE.md`: the one artifact that stops every later task from re-discovering the stack, so the whole pipeline stays cheap.

## Core rules

- **Owned repos only.** This skill commits infrastructure into the repo. If the repo isn't yours to commit to, stop and use the `onboarding` skill (guest mode), which sets everything up without a footprint.
- **Greenfield only.** This skill composes the kit into a new or empty repo. Migrating an existing owned repo's app code onto the kit incrementally is `modernization`'s job, not scaffolding's.
- **Detect before you generate — and compose before you hand-write.** Even "greenfield" often has a partial setup. Read what exists first and conform; don't overwrite a convention that's already there. When nothing exists yet, compose from the starter kit's `templates/`/`components/`/`references/recipes/` catalog rather than generating a monorepo, an auth flow, or a Dockerfile from scratch — the kit ships those already built, version-pinned, and secure-by-default.
- **Right-size, don't over-engineer.** Match the actual stack and scale. A solo/freelance app gets Docker + a PaaS target and simple CI — not Kubernetes, multi-env promotion, or ceremony it won't use. (See `${CLAUDE_PLUGIN_ROOT}/references/devops/deploy-operate.md`.)
- **Everything committed is deliberate.** The CI workflows, branch protection, `.claude/settings.json`, and `CLAUDE.md` are the repo's contract with the pipeline. Never commit secrets.
- **Keep CLAUDE.md lean.** It's always in context — stable, high-value facts only (stack, versions, layout, commands, conventions), never a changelog or a tutorial. This is the token-efficiency payoff; see `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`.
- **Wire to the firm's gate.** The pipeline enforces `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`: CI green + a review, and **the human merges** — no self-merge.

## Workflow

### 1. Create or confirm the repo
Confirm this is (or will be) a repo you own — admin rights / able to install the GitHub App and commit workflows. If it's someone else's repo → hand to `onboarding`.
- **New app with no repo yet:** create it. `git init`, an initial commit, then `gh repo create <owner>/<name> --private --source=. --push` (or the API), and confirm the default branch. This is scaffolding's job — a brand-new app gets its repo here.
- **Repo already exists (empty or partial):** use it and conform to what's there.
This is the step that makes `product-planning` able to file its epic and milestones, so for a greenfield product, **scaffolding runs before the product plan is recorded**. If a `product-planning` epic already exists, this is Stage 0 of that roadmap — read the epic so the scaffold matches the planned architecture.

### 2. Detect intent → pick blocks
Detect anything already present (manifests, lockfiles, framework config) and conform — don't re-scaffold over a project that has already diverged. For a true greenfield, map the requested product onto the kit's catalog rather than inventing a layout:

- **Backend:** `backend/fastapi` (default) or `backend/django` (server-rendered + HTMX). Ask if the request doesn't make the track obvious; otherwise default to FastAPI.
- **Frontend:** `frontend/vite-spa` (default, client-rendered SPA) or `frontend/nextjs` (SSR/App Router — pick this when the product plan calls for server rendering, SEO-sensitive public pages, or an App-Router-specific need). Ask if ambiguous, default to `vite-spa`.
- **Admin app (opt-in):** `frontend/nextjs-admin` → `apps/admin`, a second standalone Next.js app, whole-app-gated on the backend's `admin` role. Ask yes/no; default no.
- **Mobile (opt-in):** `mobile/expo` → `apps/mobile`. Ask yes/no; default no.
- **Infra (opt-in):** `infra/aws-fargate` → `infra/aws-fargate`. Ask yes/no; default no (right-size — see "Right-size" above; a solo/early project may not need it yet).

Only the backend track and the admin/mobile/infra yes/no axes are genuinely ambiguous — ask about those specifically. Everything else (the frontend flavor when nothing signals SSR, the exact versions, the catalog components each block already wires) defaults per the kit; state the chosen blocks in a line before composing, e.g. "backend/fastapi + frontend/vite-spa, no admin/mobile/infra — composing."

### 3. Compose from the kit, don't generate
This is the core of scaffolding: **materialize and `cp -R` the kit's blocks — never hand-write a monorepo layout, a Dockerfile, or an auth flow the kit already ships.** Model A composition (the pattern the kit's own `just add-mobile` uses internally) — inline `cp -R`, not a justfile recipe:

1. **Materialize the monorepo skeleton.** `cp -R ${CLAUDE_PLUGIN_ROOT}/templates/monorepo/. <project>/` — this lays down `package.json`, `pnpm-workspace.yaml`, `justfile`, `docker-compose.yml`, lint/format config, `.gitignore`/`.dockerignore`/`.env.example`, and the `apps/`/`packages/` skeleton with the standard task-runner surface (`test`, `lint`, `dev`, `build`, `deploy`, `docs-generate`, `docs-check`, `client-generate`).
2. **Strip the `.tmpl` suffixes and substitute.** `README.md.tmpl` → `README.md`, `CLAUDE.md.tmpl` → `CLAUDE.md`; replace `<project-name>` with the real project name in both. These become the project's own root docs (see steps 5-6).
3. **Compose each chosen block** into its target with `cp -R ${CLAUDE_PLUGIN_ROOT}/templates/<layer>/<name>/. <project>/<target>/`:
   - `backend/fastapi` or `backend/django` → `apps/api`
   - `frontend/vite-spa` or `frontend/nextjs` → `apps/web`
   - `frontend/nextjs-admin` (if chosen) → `apps/admin`
   - `mobile/expo` (if chosen) → `apps/mobile`
   - `infra/aws-fargate` (if chosen) → `infra/aws-fargate`
4. **Compose `packages/api-client`** whenever a frontend, admin app, or mobile block is present: `cp -R ${CLAUDE_PLUGIN_ROOT}/templates/packages/api-client/. <project>/packages/api-client/`. Any React web block also needs `templates/components/frontend/` (`@repo/web-shared`) composed to `packages/web-shared/` — check that block's own README `needs` list rather than assuming.
5. **Wire `components/*` per each block's own README.** Every template block's README states its composition contract (`needs`/`exposes`) up front — read it and follow it rather than guessing. A backend block already vendors its baseline `templates/components/backend/*` and `templates/components/security/*` components internally (see that block's own "Vendored components" section); scaffolding's job is the blocks it composes at the monorepo level (`api-client`, `web-shared`) and anything a block's README says it still needs supplied.
6. **`pnpm install`** to register the new workspace packages.
7. **`just client-generate`** (when a backend + a frontend/mobile/admin consumer are both present) — exports the backend's live OpenAPI schema and regenerates `packages/api-client`'s typed hooks from it, so the committed client never drifts from the backend it targets.
8. **`just docs-generate`** — see the next step.

Don't hand-roll a Dockerfile, an auth flow, a pagination scheme, or a monorepo layout when a kit block already ships it — that's the whole point of composing.

### 4. Generate the root README
`just docs-generate` (see `templates/monorepo/scripts/docs-aggregate.mjs`) aggregates every composed block's/component's co-located `docs/fragment.md` into the Setup/Deployment/Maintenance/Secrets sections of the root README, inside that block's own `<!-- BEGIN block:<layer>/<name> -->`/`<!-- END -->` marker region — see `${CLAUDE_PLUGIN_ROOT}/references/authoring/documentation-standard.md` for the full aggregation-marker and doc-fragment spec. Scaffolding's own job here is narrow:
- Run `just docs-generate` after composition (step 3) so those four sections populate from what's actually composed in.
- **Hand-write only the Structure section** — a short orientation to the monorepo layout and where each composed block lives. It is not an aggregation region; nothing fills it automatically.
- **Verify the Secrets table is non-sentinel** — once at least one block is composed in, the `<layer>/<name>` sentinel marker pair must be gone and replaced by real rows (e.g. `DATABASE_URL`, `JWT_SIGNING_KEY`) pointing at where to get each secret. A sentinel still present after composing real blocks means a fragment is missing or `docs-generate` wasn't run — fix it before handing off.
- Never hand-edit inside an aggregated region directly — if a block's contribution is wrong, fix that block's `docs/fragment.md` and re-run `just docs-generate`.

### 5. Write the lean CLAUDE.md
Materialize `CLAUDE.md.tmpl` (already done in step 3.2) per `${CLAUDE_PLUGIN_ROOT}/references/authoring/documentation-standard.md`'s "The project CLAUDE.md template": what the project is (purpose + the blocks composed into it), a pointer to the root README's Structure section (don't duplicate it), and the "Keeping the README current" discipline (fragment first, `just docs-generate`, `just docs-check` before committing, never hand-edit an aggregated region). Fill in "What this project is" with the real one-or-two-sentence purpose and block list; the rest of the template body is already correct as materialized. Keep it short — this is still the lean, always-in-context artifact, not a tutorial.

### 6. Wire the pipeline
- **Issue and PR templates:** copy `${CLAUDE_PLUGIN_ROOT}/assets/pr/pull_request_template.md` to the repo's `.github/pull_request_template.md` — substituting its `<owner>` placeholder (the `cc @<owner>` line) with the repo owner's GitHub handle — and `${CLAUDE_PLUGIN_ROOT}/assets/issue-templates/*.yml` to the repo's `.github/ISSUE_TEMPLATE/` (`feature.yml`, `bug.yml`, `epic.yml`, `config.yml`) as-is; the issue templates carry no owner placeholder. These give the repo GitHub-native forms that mirror what `planning` and `product-planning` file, so hand-filed and skill-filed issues/PRs stay consistent.
- **CI/support workflows:** copy the firm's workflow templates from `${CLAUDE_PLUGIN_ROOT}/assets/workflows/` into the repo's `.github/workflows/`:
  - `security.yml` (the required security-scan gate) is a self-contained copied workflow — no owner substitution, no secret, no plugin; it runs on the default `GITHUB_TOKEN`.
  - `epic-checkoff.yml` (ticks an epic's checkbox when a stage/feature issue closes on merge) is likewise self-contained — but it only does anything once `planning`/`product-planning` link issues to their epic (the `Epic: #<n>` marker + the issue number on the checklist line).
  - There is no Action workflow to install here: the repo picks up and reviews work exclusively through the interactive `coding-session` skill (run locally or from Claude Code on the web), never from a GitHub mention or PR/issue event.
  - Any **additional PR gate** you write for the repo (a test or lint workflow for the composed stack) follows the firm convention the copied `security.yml` already carries: `ready_for_review` in the `pull_request` `types:` plus a job-level draft guard, so gates run on ready-for-review PRs and drafts are verified in the container instead (`${CLAUDE_PLUGIN_ROOT}/references/devops/cicd.md`).
- **CI gates:** lint → type-check → test → security scan, per `${CLAUDE_PLUGIN_ROOT}/references/devops/cicd.md`. A red gate blocks merge.
- **Branch protection:** require the CI checks (lint/type-check/test/security) and at least one review; disallow direct pushes to the default branch. Merge stays manual — yours.
- **Committed `.claude/settings.json`:** cost/efficiency env (`DISABLE_NON_ESSENTIAL_MODEL_CALLS`), sensible permissions (deny reads of `.env*`), attribution per your preference for owned repos, and **cloud plugin-freshness** (below). This is what cloud sessions and the scheduled-audit Actions inherit (they don't carry your user settings).
- **Cloud plugin-freshness (owned repos):** cloud (web) sessions [snapshot the filesystem](https://code.claude.com/docs/en/claude-code-on-the-web#environment-caching) after the environment's setup script runs once, so `~/.claude/plugins` freezes at the first-installed version and new firm skills silently never appear — even in a fresh session started after a release. Guard against it in the committed `.claude/settings.json`: register the marketplace with `autoUpdate` and add a `SessionStart` hook (matchers `startup` + `resume`) that runs `claude plugin marketplace update <repo> && claude plugin update dev-lifecycle@<repo>` (guarded with `command -v claude`, output to `/dev/null`, `|| true`, `timeout` ~120) so the plugin refreshes each session regardless of the snapshot. See `Keeping cloud sessions current` in `docs/SETUP-AND-USAGE.md` for the exact block and the environment-level alternative. Do **not** add this on the guest path — guest repos take no committed footprint.

### 7. Hand off
Summarize what was scaffolded and how the repo now works: which blocks were composed, how to run it locally, what CI gates on, how a plan gets built through the interactive `coding-session` skill, and how a change reaches merge. If this is Stage 0 of a product roadmap, point to the next stage. The next move is a plan (`planning`), not code.

## What this skill does NOT do
- Scaffold a repo you don't own — that's `onboarding` (guest mode), which leaves no footprint.
- Hand-roll a monorepo, a Dockerfile, an auth flow, or a doc-aggregation scheme when a kit block exists — compose it, don't generate it from scratch.
- Over-engineer infrastructure beyond the project's real scale.
- Write feature code — that happens after a plan, via the build skills.
- Commit secrets, or bake them into images or settings.
