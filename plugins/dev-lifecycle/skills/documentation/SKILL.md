---
name: "documentation"
description: "Write and maintain technical documentation for a codebase — READMEs, API references, architecture decision records (ADRs), changelogs, contributing/onboarding guides, and inline docstrings/comments. Use this skill WHENEVER the work involves documenting code or a project: \"write a README\", \"document this API/endpoint/module\", \"add docstrings\", \"write an ADR\", \"update the changelog\", \"explain how to set this up\", or when a feature needs docs before it ships. It detects the project's existing documentation style and conforms. This is technical documentation — for resumes, proposals, or general prose, use the dedicated writing skills instead."
---

# Documentation

Write docs a real reader can act on, and that stay true to the code. The failure mode of documentation isn't being too sparse — it's being *wrong*: stale docs that describe code that no longer exists are worse than none. So the job is twofold: write the right doc for the reader, and keep it close enough to the code that it stays current.

Guiding idea: **document the *why*, not the *what* — and always the present, never the past.** The code shows what it does; documentation captures intent, tradeoffs, constraints, and the reasoning behind a decision. A comment that restates the code is noise; one that explains why the obvious approach was rejected is gold; one that narrates what the code used to do or how it changed is worse than either — the change's story belongs in git history and the PR, not the source. For inline comments and doc-comment coverage the firm-wide rule is `${CLAUDE_PLUGIN_ROOT}/shared/code-comments.md`.

## Core rules

- **Audience first.** Identify who reads this and what they need to do with it; write for that person's task, not a generic info dump.
- **Document the why.** Capture intent, tradeoffs, non-obvious constraints, rejected alternatives.
- **Keep docs current and close to code.** Put docs near the code and update them in the *same* change as the code they describe. A doc you won't keep updated shouldn't be written.
- **Right-size to the project.** A solo side project, a client deliverable, and an OSS library need very different amounts. Don't generate ceremonial docs nobody reads.
- **Show, don't just tell.** A concrete usage example or command beats paragraphs. Examples must actually work.
- **Detect and conform.** Match the project's existing doc style, structure, tone, and tooling.
- **Write like a human.** Clear, direct, specific; avoid filler and AI-sounding boilerplate. For heavier prose polish, the `humanizer` / `ruthless-edit` skills apply after.
- **Work context-efficiently.** Verify claims against the specific code, not the whole tree. See `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`.

## Workflow

### 1. Detect existing docs & the need
Read what's there (`README`, `docs/`, `CHANGELOG`, `CONTRIBUTING`, ADRs, docstring style) and match its conventions. Figure out which artifact the request needs and at what depth.

### 2. Identify the artifact
- **README / contributing / changelog / ADR** → `${CLAUDE_PLUGIN_ROOT}/references/docs/project-docs.md`.
- **API documentation / inline docstrings & comments** → `${CLAUDE_PLUGIN_ROOT}/references/docs/code-and-api-docs.md`.

### 3. Write it
Load the relevant reference. Lead with what the reader needs most; prefer working examples and real commands; be accurate over comprehensive — verify against the actual code. Produce real files in conventional locations (`README.md`, `CHANGELOG.md`, `docs/adr/NNNN-title.md`).

### 4. Hand off
Note what you documented and where, and flag anything you couldn't verify against the code. If it documents new code, remind that doc and code move together going forward.

## The starter-kit documentation model
A project scaffolded from the firm's starter kit (`scaffolding`, composing `${CLAUDE_PLUGIN_ROOT}/templates/`) does not have a hand-maintained root README — it has an **assembled** one. Recognize this model (a `justfile` with `docs-generate`/`docs-check` targets, a root README with `<!-- BEGIN block:<layer>/<name> -->` marker comments) and work with it rather than against it:

- **The root README is assembled, not written.** `just docs-generate` aggregates every composed block's/component's co-located `docs/fragment.md` into the Setup/Deployment/Maintenance/Secrets sections, each wrapped in that block's own BEGIN/END marker region. The **Structure** section is the one exception — hand-written directly, never aggregated.
- **Edit the fragment, never an aggregated region.** A block's contribution lives at `docs/fragment.md` inside its own materialized directory (e.g. `apps/api/docs/fragment.md`, `packages/api-client/docs/fragment.md`). Fix inaccurate or stale content there, then re-run `just docs-generate` — a hand-edit made directly inside a `<!-- BEGIN block:... -->`/`<!-- END -->` region is silently overwritten on the next regeneration.
- **`just docs-check` catches drift.** Run it (exit non-zero on drift) before treating root-README docs as done; it's the mechanical backstop for the model, the same way a linter backstops style.
- **Keep the project `CLAUDE.md` accurate** — it explains the project's structure and points at the root README's Structure section; it also carries the "Keeping the README current" discipline (fragment first, then `docs-generate`, then `docs-check`) that every future contributor — human or agent — should follow.
- **Full spec:** `${CLAUDE_PLUGIN_ROOT}/references/authoring/documentation-standard.md` — the canonical doc-fragment format, the aggregation-marker syntax, and the root README/CLAUDE.md template bodies.
- **The weekly freshness audit is the drift detector at the library level**: it's what keeps a template block's *own* authoring-time README (and thus the fragment it ships) honest against the kit's pinned versions, upstream of any one project's `docs-check`. This skill's job on a single project is the same idea scoped down — don't let a project's assembled docs drift from the blocks composed into it.

## How this works with the other skills
- **planning / product-planning** decisions → **ADRs** (an epic's architecture decisions map onto an ADR). **backend** defines the API contract (FastAPI generates OpenAPI) → this writes the narrative API docs. **code-review** flags missing/stale docs → this addresses them. **scaffolding** composes the starter kit and runs the first `docs-generate`; this skill maintains the result afterward.

## What this skill does NOT do
- Write comments/docs that merely restate the code. Produce docs it won't keep current. Over-document a small project. Invent behavior instead of describing what the code verifiably does. Replace the dedicated writing skills for non-technical prose.
