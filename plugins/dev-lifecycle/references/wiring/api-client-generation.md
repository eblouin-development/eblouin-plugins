<!--
wiring: api-client-generation
covers: FastAPI/Django OpenAPI export -> frozen packages/api-client/openapi.json -> orval-generated TS client <-> web (apps/web) <-> admin (apps/admin) <-> Expo mobile
last-verified: 2026-07-23
provenance: manual
versions-pinned-to: references/compatibility-matrix.md
sources:
  - https://orval.dev
  - https://www.openapis.org/
  - references/frontend/typescript.md
  - references/compatibility-matrix.md
-->

# Generating the typed API client

**How one generated TypeScript client, produced from the backend's OpenAPI schema, becomes the single request-making surface every frontend/mobile block imports** — the web SPA (`apps/web`), the admin tool (`apps/admin`), and the Expo mobile app all call the *same* generated hooks, so a request/response type only ever comes from one place. This is a wiring reference: it stitches together pieces that each have their own canon doc, and is **subordinate to the project's existing conventions** — when they conflict, the project wins.

The three pieces:
- **The exporter** — `templates/backend/fastapi/app/export_openapi.py`, which dumps the live FastAPI app's OpenAPI 3.1 schema. This is THE source of the frozen contract (see "Which backend generates the schema" below) — the Django block never generates `openapi.json` itself, it proves wire-conformance against it (`test_schema_conformance.py`).
- **The frozen contract** — `templates/packages/api-client/openapi.json`, a **committed** export, not regenerated implicitly on every build.
- **The generated client** — `@repo/api-client` (`templates/packages/api-client/`), orval's output over a hand-written `fetch` mutator (`src/mutator.ts`), consumed identically by `apps/web`, `apps/admin`, and `apps/mobile`.

## Contents
- Which backend generates the schema
- The `client-generate` workflow
- The custom fetch mutator and `configureApiClient`
- Three consumers, one client
- The manual barrel: `src/index.ts`
- Conformance discipline: `test_schema_conformance.py` and `_PENDING_PARITY_OPS`
- Wiring checklist
- Related canon

## Which backend generates the schema
Only the **FastAPI** block (`templates/backend/fastapi`) exports `openapi.json` — its `app/export_openapi.py` dumps the live app's real OpenAPI 3.1 output (including the remapped `ErrorEnvelope` shape and `Page[T]` pagination, not FastAPI's native validation-error shape; see `templates/backend/fastapi/README.md`'s "OpenAPI export"). If the project's backend is **Django** instead, `manage.py spectacular --format openapi-json` produces the equivalent schema, but the Django block does not overwrite the committed `openapi.json` as part of its own build — `templates/backend/django/tests/test_schema_conformance.py` treats the FastAPI-exported file as the frozen ground truth and asserts the Django schema matches it wire-for-wire (see "Conformance discipline" below). Whichever framework is the project's actual backend, there is exactly **one** frozen `openapi.json`, and it is the FastAPI export.

## The `client-generate` workflow
`just client-generate` (`templates/monorepo/justfile`) runs two steps back to back:

```
cd apps/api && uv run --no-dev python -m app.export_openapi ../../packages/api-client/openapi.json
pnpm --filter @repo/api-client run generate
```

1. **Export** — `app.export_openapi` writes the live FastAPI schema to `packages/api-client/openapi.json`, overwriting the committed file.
2. **Generate** — `pnpm --filter @repo/api-client run generate` runs `orval --config orval.config.ts` (`templates/packages/api-client/orval.config.ts`), which reads `openapi.json` (`input.target`) and emits `src/generated/models/` (one file per schema + a barrel) and `src/generated/endpoints/<tag>/` (one file per OpenAPI tag: the raw async function, a React Query `*QueryOptions`/`*MutationOptions` builder, and the `use*` hook). Orval mode is `tags-split` + `client: 'react-query'` + `httpClient: 'fetch'`, with the mutator override pointed at `src/mutator.ts`'s `customFetch`.

The generated output **is committed** (`clean: true` regenerates it wholesale each run — never hand-edit under `src/generated/`). Re-run `just client-generate` any time the backend's routes/schemas change and commit both the updated `openapi.json` and the regenerated `src/generated/**` diff together — they must move as one unit, since a stale `openapi.json` and a fresh generated client (or vice versa) silently reintroduce drift the conformance suite exists to catch.

## The custom fetch mutator and `configureApiClient`
Every generated hook calls through `src/mutator.ts`'s `customFetch`, configured once per app at startup via `configureApiClient`:

```ts
import { configureApiClient } from "@repo/api-client";
configureApiClient({ baseUrl, mode });
```

- **`baseUrl`** — the backend origin, sourced from the consuming app's own framework-prefixed env var (never a bare `process.env.API_BASE_URL` — see "Configuration" in `templates/packages/api-client/README.md` for why that breaks under Vite/Next/Expo). Unconfigured resolves to `""` (same-origin relative URLs).
- **`mode`** — `"session"` | `"bearer"` | `"cookie"`, library-defaults to `"bearer"` (the safe-to-get-wrong choice — see `templates/packages/api-client/README.md`'s "Auth modes" for why the library default isn't the same as the backend's own default). `apps/web`/`apps/admin` pass `mode: "session"` explicitly — the PREFERRED browser posture, an opaque `HttpOnly` session cookie the backend issues by default; `apps/mobile` passes `mode: "bearer"` explicitly (native has no ambient-cookie problem to begin with and a real OS-backed secret store instead — passing it explicitly matters because bearer is no longer the *backend's* default, only the client library's). `mode: "cookie"` (JWT refresh token in an `HttpOnly; Path=/auth` cookie) is superseded, kept for a project mid-migration off it.
- **`getAccessToken`** — an optional in-memory access-token getter (default-off, and IGNORED entirely in session mode, which holds no token); when supplied in bearer or cookie mode, the mutator injects `Authorization: Bearer <token>` on every call that doesn't already carry one.

The mutator's response shape is `{ data, status, headers }` (not a thrown error for a documented non-2xx) — see `references/wiring/frontend-backend-contract.md` for how `unwrap()` turns that into something react-query treats as an error. Full mode-by-mode detail (session vs bearer vs cookie, the CSRF echo, RBAC) lives in `references/wiring/auth-end-to-end.md`.

## Three consumers, one client
`apps/web` (`templates/frontend/vite-spa/` or `templates/frontend/nextjs/`), `apps/admin` (`templates/frontend/nextjs-admin/`), and `apps/mobile` (`templates/mobile/expo/`) all depend on `@repo/api-client` as a `workspace:*` package and call `configureApiClient` once at their own entry point — `apps/web/src/main.tsx` (Vite) or a client-side root layout (Next), `apps/admin`'s equivalent, and `apps/mobile/app/_layout.tsx`. None of them hand-write a `fetch` call or a request/response interface: the generated types **are** the contract (`references/frontend/typescript.md`'s "Types from a single source of truth"). The only difference between consumers is their `configureApiClient` args — `apps/web`/`apps/admin` pass `mode: "session"` (no token to hold, so no `getAccessToken` either); `apps/mobile` passes `mode: "bearer"` (token attached by its own auth engine per call — see `references/wiring/mobile-backend.md`).

## The manual barrel: `src/index.ts`
`templates/packages/api-client/src/index.ts` is **hand-maintained**, not generated — orval writes per-tag files under `src/generated/endpoints/<tag>/`, but nothing re-exports them from the package root automatically. Every tag needs an explicit `export *` line added here:

```ts
export * from "./generated/endpoints/auth/auth.js";
export * from "./generated/endpoints/admin/admin.js";
export * from "./generated/endpoints/blog/blog.js";
export * from "./generated/endpoints/health/health.js";
export * from "./generated/endpoints/items/items.js";
export * from "./generated/endpoints/moderation/moderation.js";
export * from "./generated/models/index.js";
export { configureApiClient, customFetch } from "./mutator.js";
export type { ApiClientResponse } from "./mutator.js";
```

Six tags exist today — `auth`, `admin`, `blog`, `health`, `items`, `moderation` — matching the six tags actually present in `templates/packages/api-client/openapi.json` (the `/admin/blog/*` routes carry the `blog` tag; `/admin/flags*` carries `moderation`). **A new generated tag needs its `export *` line added by hand** — `orval`'s `clean: true` regenerates `src/generated/**` on every run but never touches `src/index.ts`, so forgetting the line means the tag's hooks exist on disk but aren't importable from `@repo/api-client`'s public surface. Import from the root export (`@repo/api-client`), never by deep-importing `src/generated/*` — those paths reshuffle across regenerations.

## Conformance discipline: `test_schema_conformance.py` and `_PENDING_PARITY_OPS`
`templates/backend/django/tests/test_schema_conformance.py` is the proof that a Django backend serves the *identical* wire contract the frozen `packages/api-client/openapi.json` documents: it generates Django's own schema in-process (drf-spectacular's `SchemaGenerator`, no live server, no database), normalizes a narrow set of cosmetic differences (nullable representation, auto-generated component names), and asserts every `(path, method)`'s documented status codes and JSON-Schema request/response bodies match the frozen contract exactly — failing loudly, with a readable diff, on any real divergence. `_PENDING_PARITY_OPS` (a `set[tuple[str, str]]` near the top of the file) is the escape hatch for **staged rollout**: an operation listed there is excluded from the strict comparison while Django parity for it is still being built, so the suite can land incrementally without the whole gate going red — but it's a stale-guarded list, not a permanent skip (the suite also asserts every listed entry still genuinely diverges, so a fixed operation left in the set fails loudly rather than silently rotting). As of this writing `_PENDING_PARITY_OPS` is empty (`set()`) — full parity across all six tags — so treat any non-empty value you encounter as active, temporary, in-progress work, not a stable exemption list.

## Wiring checklist
1. **Backend** — export the schema: `python -m app.export_openapi packages/api-client/openapi.json` (FastAPI is always the export source, even on a Django project — see "Which backend generates the schema").
2. **Generate** — `just client-generate` (export + orval); commit `openapi.json` and the regenerated `src/generated/**` together.
3. **Barrel** — if a new OpenAPI tag landed, add its `export *` line to `templates/packages/api-client/src/index.ts` by hand.
4. **Consumers** — `apps/web`, `apps/admin`, `apps/mobile` each call `configureApiClient` once at startup with their own `baseUrl`/`mode`, then import hooks from `@repo/api-client`'s root, never `src/generated/*` directly.
5. **Django only** — run `templates/backend/django/tests/test_schema_conformance.py` and keep `_PENDING_PARITY_OPS` accurate (empty once the new surface has full parity).

## Related canon
- `templates/packages/api-client/README.md` — the package's full composition contract, `client-generate` mechanics, cookie-mode detail, and the mutator's response shape.
- `templates/backend/fastapi/README.md` — the "OpenAPI export" section (`export_openapi.py`, the `ErrorEnvelope`/`Page[T]` remapping).
- `templates/backend/django/README.md` — the drf-spectacular wiring and the conformance proof's own "Step 4" section.
- `references/wiring/auth-end-to-end.md` — how the `auth` tag's hooks are consumed in cookie vs bearer mode.
- `references/wiring/frontend-backend-contract.md` — the `ErrorEnvelope`/`Page[T]` shapes this generated client's types encode.
- `references/compatibility-matrix.md` — the pinned versions (orval 8.22.x, drf-spectacular 0.30.x).
