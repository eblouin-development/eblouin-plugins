# eblouin-plugins

A Claude Code plugin marketplace: the development firm's lifecycle skills and a self-extending, version-aware reference library. This repo is itself a project in the firm — changes go through the same plan → PR → review → merge → release pipeline as any other repo.

> Names (`eblouin-plugins`, `dev-lifecycle`) are placeholders — rename to taste before first publish.
>
> **Renaming the org/repo?** Most of the codebase already derives the owner
> and repo dynamically (`${{ github.repository }}` / `${{ github.repository_owner }}`
> in workflows that run inside this repo) and needs no edit. Where a literal
> has to exist, it follows one placeholder convention — `<owner>` for the
> GitHub handle/org, `<repo>` for this repo's name — and you fill both in at
> the same time you do the rename:
> - `plugins/dev-lifecycle/skills/{scaffolding,onboarding}/SKILL.md` — the wiring
>   instructions reference the same `<owner>`/`<repo>` pair
> - `.github/fleet-repos.txt` — replace the commented placeholder examples with
>   your own fleet's `owner/repo` entries
> - `.claude/settings.json` and this repo's own literal mentions (`README.md`,
>   `docs/SETUP-AND-USAGE.md`, `.claude-plugin/marketplace.json`'s `name`) —
>   these describe *this* repo's actual identity, so update them to match
>   wherever you rename to
> - `CLAUDE.md` / `.github/pull_request_template.md` — this repo's own `cc
>   @eblouin876` convention is a literal for *this* repo's owner; update the
>   handle to your own, or derive it if you script the rename (see #84's
>   `${{ github.repository_owner }}` pattern used in the shared workflows)

## Install

```
/plugin marketplace add <owner>/eblouin-plugins
/plugin install dev-lifecycle@eblouin-plugins
```

Enable background auto-update in `/plugin` → Marketplaces, or refresh manually with `/plugin marketplace update`. (Auto-pull of git-sourced marketplaces can lag; a periodic manual update is the reliable fallback.)

## Layout

```
eblouin-plugins/
├── .claude-plugin/marketplace.json      # catalog (semver in metadata.version)
├── plugins/dev-lifecycle/
│   ├── .claude-plugin/plugin.json       # plugin manifest (semver)
│   ├── skills/<skill>/SKILL.md          # one folder per skill, incl. template-author, recipe-author
│   ├── assets/workflows/                # workflows copied into each repo: epic-checkoff (ticks an epic's box on stage-issue close) and security.yml (the self-contained CI security gate — SAST/secret/dep-CVE/IaC/image scans)
│   ├── assets/scripts/                  # operator one-shots — retrofit-epic.sh backfills a pre-existing epic so epic-checkoff works on it
│   ├── assets/pr/                       # shippable templates `scaffolding` copies into project repos, e.g. pull_request_template.md
│   ├── shared/                          # cross-skill references
│   │   ├── token-efficiency.md          # the efficiency doctrine every skill follows
│   │   ├── worker-cadence.md            # how an orchestrator watches dispatched subagents (backstop cadence, not polling)
│   │   ├── parallelization.md           # what work can run concurrently, and the worktree isolation that makes it safe
│   │   ├── ci-convergence.md            # push → CI → fix → review → fix loop to green, with watchdogs and a CI-down fallback
│   │   └── definition-of-done.md        # the shared merge-ready bar
│   ├── templates/                       # golden-path starter kit: composable blocks + catalog components
│   │   ├── <layer>/<name>/README.md     # a block (e.g. backend/fastapi) — composition contract (needs/exposes) + doc fragment
│   │   ├── components/<domain>/         # lighter drop-in slices / shared packages (e.g. the generated api-client)
│   │   └── _TEMPLATE-README.md          # composition-contract schema + house format every block/component fills
│   └── references/                      # the self-extending library, by domain
│       ├── frontend/  {react,typescript,tailwind,material-ui,htmx}.md
│       ├── backend/   {fastapi,sqlalchemy,postgres,pydantic,django,drf,celery,redis,stripe,pandas,websockets,anthropic,slack-bolt}.md
│       ├── devops/    {cicd,containers,deploy-operate,kubernetes,uv}.md
│       ├── mobile/    {expo,react-native,navigation,native-modules}.md
│       ├── testing/ · debugging/ · docs/ · infra/ · review/
│       ├── security/     {secure-baseline,attack-surfaces,secrets-management,payments-security,data-protection,owasp}.md — the firm security standard every block/recipe inherits
│       ├── authoring/    {documentation-standard}.md — the co-located-doc-fragment model templates/recipes ship, and its aggregation into a project's root README + CLAUDE.md
│       ├── recipes/      <name>.md — feature recipes (e.g. stripe-checkout) that wire existing blocks/components into one capability; _RECIPE-TEMPLATE.md is the schema exemplar
│       ├── wiring/       {api-client-generation,auth-end-to-end,frontend-backend-contract,infra-app,mobile-backend}.md — cross-artifact seams no single block's README fully owns
│       ├── compatibility-matrix.md      # the keystone pinned version set every template block and component pins to
│       └── _TEMPLATE.md                 # house format + metadata header
├── docs/adr/                            # numbered, immutable Architecture Decision Records (see project-docs.md)
├── .claude/skills/
│   └── library-contribution/SKILL.md    # house rules for changing THIS repo (layout, conventions, the release contract)
├── scripts/
│   ├── validate_plugin.py               # structural validator (manifests + SKILL.md frontmatter)
│   └── check_version_bump.py            # checks a PR's version bump against its release:* label
└── .github/
    ├── pull_request_template.md         # this repo's own PR template (plugin-specific gates)
    └── workflows/
        ├── validate.yml                     # runs the validator on every push/PR (merge gate)
        ├── template-tests.yml               # runs the template blocks' + catalog components' own test suites on every PR (merge gate)
        ├── version-bump.yml                 # every PR must carry the bump its release:* label implies (merge gate)
        ├── freshness-audit.yml              # weekly: references, templates/components, recipes, the matrix, and doc drift gone stale → tracking issue
        └── coverage-audit.yml               # weekly: fleet libraries with no reference → PR (reads .github/fleet-repos.txt)
```

`.github/fleet-repos.txt` lists the repos the coverage audit scans for gaps — edit it as the fleet changes.

Skills reference shared files and library references by plugin-root path, e.g. `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md` and `${CLAUDE_PLUGIN_ROOT}/references/frontend/react.md`. (Resolution is verified in the local plugin test; the fallback, if a path form doesn't resolve, is to copy the shared file into the skill's own folder.)

## Reference library conventions

References are **per-library**, not per-skill: one shared library the whole plugin loads from, so a doc is written once and every skill benefits. A build skill loads only the references for libraries actually detected in the project (progressive disclosure — see `shared/token-efficiency.md`).

Every reference carries a metadata header (see `references/_TEMPLATE.md`):

```
library · versions-covered · last-verified · provenance · sources
```

This header is load-bearing: it's what the freshness audit reads to decide staleness cheaply, and what distinguishes reviewed canon (`provenance: manual`) from machine-drafted docs (`provenance: auto-generated (pending review)`).

### Four ways a reference gets created or refreshed
1. **Onboarding (eager):** when a repo joins the firm, its significant direct dependencies are inventoried and any missing references are generated as a batch. Target frameworks/libraries with real idioms and version-sensitivity — not every transitive utility.
2. **Build-time (lazy):** if a build skill meets a library with no reference, it generates one for immediate use and opens a PR to add it here. This is model-driven and only fires when a build session has this repo in scope — the coverage audit (#4) is its reliable backstop.
3. **Scheduled audit (freshness):** `freshness-audit.yml` weekly flags references whose covered version or last-verified date has fallen behind; regeneration follows as a reviewed PR. Keeps the library **current**.
4. **Scheduled audit (coverage):** `coverage-audit.yml` weekly reads `.github/fleet-repos.txt`, inventories each fleet repo's significant dependencies, diffs them against the library, and opens a PR adding references for any framework in use but undocumented. Keeps the library **complete** — the automatic form of #2. Requires a `FLEET_READ_TOKEN` secret with read access to the (mostly private) fleet repos.

All four **ground generation in current official docs, never recall**, and all four land as **PRs you review and merge** — nothing enters canon silently.

### Templates and recipes (the starter kit)

Two further artifact types extend the library into runnable starter code, alongside plain references:

- **Templates** — composable golden-path **blocks** (`templates/<layer>/<name>/`, e.g. `backend/fastapi`) and lighter drop-in **catalog components** (`templates/components/<domain>/`), each declaring a composition contract (what it `needs` from the monorepo, what it `exposes`) so independently-authored blocks stitch together — see `templates/_TEMPLATE-README.md`. Authored via the `template-author` skill.
- **Feature recipes** (`references/recipes/<name>.md`) — portable how-tos that wire an already-authored block/component into one feature (Stripe checkout, S3 uploads, an auth provider) without inventing new infrastructure — see `references/recipes/_RECIPE-TEMPLATE.md`. Authored via the `recipe-author` skill.

Both carry the same kind of metadata header as a reference (`last-verified`, `provenance`, plus their own fields — `needs`/`exposes`/`versions-pinned-to` for a block, `applies-to` for a recipe) and pin to `references/compatibility-matrix.md`, the keystone pinned version set every block/component is built against. Both must clear **four acceptance bars** before they ship — composition-contract present, documented, version-pinned, secure-by-default (per `references/security/secure-baseline.md`) — enforced by their authoring skill, not left to review to catch. Documentation is co-located and portable: every block/component/recipe ships a doc fragment that aggregates into a scaffolded project's root README, per `references/authoring/documentation-standard.md`.

`docs/STARTER-KIT.md` is the index of the whole kit — every block, catalog component, recipe, and wiring ref, enumerated with what it materializes to and what it needs/exposes.

## Releases (semver)

Version lives in `plugin.json` and `marketplace.json` (three fields, which must agree). **The PR carries its own bump** — you compute the new version from `main`'s current one and write it into the change, labelled `release:major` / `release:minor` / `release:patch`, or `release:none` for a change consumers never see. `version-bump.yml` gates every PR on that label matching the bump. Nothing bumps or tags after merge: `main` changes only through a reviewed pull request, so the merged version *is* the release. Users only receive updates when the version bumps, so an in-progress push never destabilizes a working session. Roll back by pointing the catalog entry at a prior commit (or tag one by hand at the point you want to keep). The full contract is in `.claude/skills/library-contribution/SKILL.md`.

## Owned vs guest repos

- **Owned:** full in-repo pipeline — CI workflows, branch protection, committed `.claude/settings.json` + `CLAUDE.md`. Set up by the `scaffolding` skill. Work is picked up and reviewed through the interactive `coding-session` skill, not a GitHub Action.
- **Guest (repos you don't own):** zero footprint. The skill library rides in via this user-installed plugin — nothing added to the repo. Project-local config goes in untracked files excluded via `.git/info/exclude`. Work runs under your own identity, so commits and PRs are yours; attribution is blanked (`{"attribution":{"commit":"","pr":""}}` in `~/.claude/settings.json`) and the `onboarding` skill scrubs any residual Claude attribution before push. Set up by the `onboarding` skill.

## Validation

`scripts/validate_plugin.py` checks the JSON manifests and the YAML frontmatter of every `SKILL.md` — the things Claude Code rejects on install. It runs in CI (`validate.yml`) on every push and PR, so a malformed skill can't be merged or shipped. Run it locally before pushing:

```bash
python scripts/validate_plugin.py
```

Make the `validate` job a **required status check** in branch protection so nothing merges without it — and `version-bump` alongside it, since that's what enforces that each PR carries the version bump its `release:*` label implies (see `.claude/skills/library-contribution/SKILL.md`). The workflow also runs the official `claude plugin validate` as a best-effort cross-check.

`validate.yml` is structural only — it never executes a single test. `template-tests.yml` is what actually runs the template blocks' and catalog components' own test suites: the FastAPI and Django backend blocks' pytest suites (`templates/backend/{fastapi,django}/tests/`, each via its own `pyproject.toml` + `uv sync`), the 6 backend + 9 security catalog components under `templates/components/{backend,security}/` (each an ephemeral `uv run --with <matrix-pinned deps> -- pytest`, per that component's README), and the frontend catalog component's vitest suite (`templates/components/frontend`, pnpm + vitest). Every suite runs pinned to `references/compatibility-matrix.md` — never floating latest — so a version bump can't silently start failing template tests without also updating the matrix. It path-filters to `templates/**` (plus itself and the matrix file) at the job level, not via a top-level workflow trigger filter — a PR that doesn't touch those paths gets every test job reported as **skipped**, not silently never-run, which is what lets it stay a required status check without blocking merges on unrelated PRs. The weekly schedule run always runs everything, unfiltered, as a drift backstop.

Its three job groups matrix-expand into one check per block/component, so make the single aggregate `template-tests-required` job (which depends on, and fails if any of, the others) a **required status check** in branch protection alongside `validate` — that's the one check to add, and it stays correct as components are added without touching branch protection again.
