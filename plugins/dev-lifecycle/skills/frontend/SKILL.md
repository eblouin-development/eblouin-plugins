---
name: "frontend"
description: "Build, modify, or review frontend web UI following modern best practices. Use this skill WHENEVER the work involves the client-facing layer of a web app — React components, pages, hooks, state, forms, styling, or server-rendered HTML/templates with HTMX and Tailwind. Trigger it for \"build the UI for X\", \"add a component\", \"wire up this page\", \"make this form work\", \"style this\", or any frontend task that follows a plan. Before writing any frontend code, this skill ALWAYS detects the project's existing stack and the exact versions and conforms to what's already there rather than imposing a different framework."
---

# Frontend

Build frontend that fits the project as it actually is — not a generic template. The single biggest source of bad frontend work is assuming a stack and a version instead of checking. This skill front-loads that check, then writes code that matches the project's real conventions, framework, and version.

## Core rules

- **Detect before you build.** Never assume the framework, version, styling system, or conventions. Read the project first (step 1). Not optional.
- **Conform, don't convert.** If the project uses React, write React. If it uses server-rendered templates + HTMX, write that. Never introduce a new framework, state library, or styling system into an existing app without the user asking. Match existing patterns even when you'd personally choose differently.
- **Version dictates idiom.** React 18 and 19 are written differently; Tailwind v3 and v4 are configured differently; MUI v6 and v7 import differently. The "modern" way depends on the installed version — check it, then write for that version.
- **Work context-efficiently.** Detect from `package.json`/lockfile, not by reading the tree; read one or two representative components for house style; load only the reference(s) for the libraries actually present. See `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`.
- **Implement against a plan when one exists.** Build to the plan; if there's no plan and the work is non-trivial, suggest planning first.

## Workflow

### 1. Detect the stack (always)
Read **`package.json`** and the **lockfile** for: React and its major (`react`/`react-dom`), the meta-framework (`next`, `react-router`, `astro`, `vite`), state libs, styling (`tailwindcss`, MUI, CSS modules), TypeScript, and the test runner. **No `package.json` + server-side templates** (Django/Jinja, `*.html` with `hx-*`) → server-rendered path. Read config files and one or two representative components to mirror house style.

**Detect a kit-composed monorepo first.** `apps/web` (and possibly `apps/admin`) + `packages/api-client` + `packages/web-shared` (`@repo/web-shared`) alongside a root `pnpm-workspace.yaml`/`justfile` mean this project was scaffolded from the starter kit (`scaffolding`) rather than hand-rolled. In that case routing/auth/query-client wiring already follows the kit's provider-wiring pattern (see that app's own README) — mirror it rather than reinventing it. Note whether `apps/admin` exists (the whole-app admin-gated Next.js tool) and whether a public read surface like `/blog/posts` is already exposed — both change what a given task actually needs to build.

State what you found in a line, e.g. "React 19 + Vite + TypeScript + MUI v7 + Tailwind v4 — kit-composed (apps/web + packages/api-client) — following those."

### 2. Choose the approach (greenfield / open-ended only)
If a stack exists, step 1 already decided. Only when starting fresh: reach for **React (+ meta-framework)** for rich client state and app-like interactivity; reach for **server-rendered HTML + HTMX + Tailwind** when interactivity is mostly request/response (forms, partial swaps) and you want minimal JS. Don't over-engineer — a form-driven CRUD app doesn't need a SPA. State the choice and reason.

### 3. Build

**Pull from the catalog first.** Before hand-writing a cross-cutting concern, check whether the starter kit already ships it:
- **Catalog components** — `${CLAUDE_PLUGIN_ROOT}/templates/components/frontend/*` (`@repo/web-shared`: session-mode `AuthProvider` + route guards — the browser default; the backend issues a revocable, `HttpOnly` session cookie, not a JWT — plus a `QueryClient` factory, error/JWT helpers, zod form helpers) and `${CLAUDE_PLUGIN_ROOT}/templates/components/security/*` for anything with a frontend half (e.g. `auth`, `webhook-signature`). A kit-composed `apps/web`/`apps/admin` already wires most of these — check before reimplementing.
- **Feature recipes** — `${CLAUDE_PLUGIN_ROOT}/references/recipes/*` (12 recipes: `end-to-end-auth`, `audit-logging`, `transactional-email`, `file-upload-s3`, `stripe-payments`, `background-jobs`, `realtime-websockets`, `caching`, `feature-flags`, `search`, `data-export`, `push-notifications`). Each is an ordered how-to composing existing pieces across backend/frontend/mobile — read the matching recipe before hand-building auth, payments, uploads, etc.
- **Consume `packages/api-client`, don't hand-write `fetch`.** A kit-composed project ships `@repo/api-client`, a typed React Query client generated from the backend's OpenAPI schema (`just client-generate`). Import its generated hooks/models instead of writing `fetch`/axios calls by hand — that's the one typed contract the whole monorepo shares, and hand-written calls drift from it.
- **`apps/admin` and the public `/blog/posts` read surface exist** in a kit-composed project with the admin app composed in — check before assuming a moderation/admin screen or a public content-read endpoint needs to be built from nothing; it may already be there to extend.
- **Library references stay the fallback** — reach for them once the catalog/recipe/generated-client layer is in place and the remaining work is stack-specific:
  - **React** → `${CLAUDE_PLUGIN_ROOT}/references/frontend/react.md`; if TypeScript, `typescript.md`; if MUI, `material-ui.md`; if Tailwind, `tailwind.md`.
  - **Server-rendered + HTMX** → `${CLAUDE_PLUGIN_ROOT}/references/frontend/htmx.md` (+ `tailwind.md` for styling). Pairs with the backend skill's Django path.
- If a significant UI library has **no reference yet**, generate one from current official docs, use it now, and PR it into the plugin (self-extend flow).

Both paths: match existing conventions; accessibility is not optional (semantic HTML, labels, keyboard operability, focus management, contrast, alt text); handle loading/empty/error states; keep components/templates small; if TypeScript is present, type honestly (no `any` escape hatch).

### 4. Hand off
Summarize what changed (files, components, routes) so it's reviewable. Note anything to verify (a visual result, a route needing a backend endpoint) and follow-ups left out of scope. The bar for merge-ready is `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`.

**Doc upkeep.** If the change touches what a kit-composed `apps/web`/`apps/admin` exposes or needs (a new env var, a new route surface), update its `docs/fragment.md` and run `just docs-generate` so the root README stays accurate — never hand-edit an aggregated region of the root README directly (see `${CLAUDE_PLUGIN_ROOT}/references/authoring/documentation-standard.md`).

## What this skill does NOT do
- Assume a framework or version without checking.
- Introduce a new framework, state library, or styling system into an existing project unprompted.
- Hand-write a cross-cutting concern (auth, forms/query wiring, a payments/email/upload flow) the catalog or a recipe already ships — pull from `templates/components/*`/`references/recipes/*` first.
- Hand-write `fetch` calls when `packages/api-client`'s generated hooks already cover that endpoint.
- Build the backend/API (that's the backend skill — this skill consumes APIs, it doesn't define them).
- Skip accessibility or error/loading states to "keep it simple."
