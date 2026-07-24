<!--
block: packages/api-client                # catalog component (shared pnpm workspace package), not an app-level block
needs:
  - runtime config: consumers call configureApiClient({ baseUrl }) once at startup, sourcing baseUrl from their own framework-prefixed env var (VITE_API_BASE_URL / NEXT_PUBLIC_API_BASE_URL / EXPO_PUBLIC_API_BASE_URL — see "Configuration" below); unconfigured resolves to "", i.e. relative URLs against a same-origin dev proxy
  - shared workspace package consumers: any app importing @repo/api-client (web, mobile) must supply react + @tanstack/react-query themselves (peer dependencies, see "Dep vs peerDep" below)
exposes:
  - workspace package: @repo/api-client — typed React Query hooks + models generated from an OpenAPI schema
  - its co-located doc fragment: docs/fragment.md (this README is the component's canon doc; the fragment is the narrow slice `just docs-generate` aggregates)
versions-pinned-to: references/compatibility-matrix.md
last-verified: 2026-07-23
provenance: manual
-->

# @repo/api-client

The shared typed API client every frontend/mobile block imports instead of hand-writing `fetch` calls. It is a generated package: React Query hooks and models produced by [orval](https://orval.dev) from an OpenAPI 3.1 schema, layered on a small hand-written `fetch` mutator (`src/mutator.ts`) — no axios. Lives at `templates/packages/api-client/` in this repo; scaffolding materializes it into `<project>/packages/api-client/`, a sibling of `apps/*` under the same pnpm workspace (see the "Materialized-location paths" note below — it affects `tsconfig.json` and `eslint.config.mjs`).

## Contents
- Composition contract
- What it is / isn't
- How `client-generate` works
- Configuration
- Auth modes
- Access-token injection (`getAccessToken`)
- Bundler-only package
- The mutator's response shape
- Dep vs peerDep
- Materialized-location paths
- Stage 3: the live schema
- Testing

## Composition contract

**NEEDS**
- **Runtime configuration** — `configureApiClient({ baseUrl })`, called once at app startup by the consuming app. Not an env var this package reads itself (see "Configuration" below for why) — unconfigured (or `baseUrl: ""`) resolves to `""`, i.e. same-origin relative URLs against a reverse proxy that forwards API paths to the backend.
- **Shared workspace packages** — none; this package has no internal workspace dependencies. It is depended *on*, not a dependent.
- **From consumers** — any app importing this package supplies its own `react` and `@tanstack/react-query` instances (peer dependencies — see "Dep vs peerDep").

**EXPOSES**
- **Workspace package** `@repo/api-client` — import hooks/types from its root export (`index.ts`), not by deep-importing `src/generated/*` (those paths are `client-generate` output and reshuffle across regenerations). The root export re-exports every generated tag — `auth` (login/refresh/logout/`useMeAuthMeGet`), `admin` (`useAdminPingAdminPingGet`), `health`, and `items` — so a consumer (the web SPA, the Expo app) gets the auth hooks from the package root without reaching into `src/generated/endpoints/*`. Add an `export *` line here whenever a new tag lands, mirroring the existing ones.
- **Its generated contract** — the hooks, request/response types, and models mirror `openapi.json`, a committed export of the live backend's actual OpenAPI schema (Stage 3 on — see "Stage 3: the live schema" below).

## What it is / isn't
- **Is:** the one typed client web and mobile both import, so request/response types are the API contract rather than hand-copied interfaces that drift (see `references/frontend/typescript.md`, "Types from a single source of truth").
- **Isn't:** a place to hand-write API-calling code. Everything under `src/generated/` is regenerated wholesale (`clean: true` in `orval.config.ts`) — edits there are lost on the next `client-generate`. Add hand-written logic (interceptors, retry policy, auth headers) to `src/mutator.ts` or a thin wrapper in `src/index.ts` instead.

## How `client-generate` works
`just client-generate` runs `pnpm --filter @repo/api-client run generate`, which runs `orval --config orval.config.ts`. Orval mode is `tags-split` + `client: 'react-query'` + `httpClient: 'fetch'`, with a custom mutator override pointing at `src/mutator.ts`'s `customFetch`. Output:
- `src/generated/models/` — one file per OpenAPI schema, plus a barrel `index.ts`.
- `src/generated/endpoints/<tag>/` — one file per OpenAPI tag, exporting the raw async function, a React Query `*QueryOptions`/`*MutationOptions` builder, and the `use*` hook itself, per operation.

The generated output **is committed** (small, since the fixture is small) so a clean clone builds offline without an orval run. Regenerate after any schema change and commit the diff — don't hand-edit generated files.

## Configuration
The mutator does **not** read `process.env` — a bare `process.env.API_BASE_URL` at module load breaks every documented consumer: Vite ships no `process` global in the browser bundle (`ReferenceError: process is not defined` at import time), and Next/Expo only statically inline framework-prefixed env vars (`NEXT_PUBLIC_*`/`EXPO_PUBLIC_*`) — a bare `API_BASE_URL` read there silently resolves to `""` even when the var is set in the shell/CI environment. Instead, call `configureApiClient({ baseUrl })` once at app startup, before any generated hook fires a request, using whatever env var naming convention the consuming framework requires:

```ts
// Vite (web) — apps/web/src/main.tsx, before rendering
import { configureApiClient } from "@repo/api-client";
configureApiClient({ baseUrl: import.meta.env.VITE_API_BASE_URL ?? "" });
```

```ts
// Next.js (App Router) — a client-side root layout/providers file
import { configureApiClient } from "@repo/api-client";
configureApiClient({ baseUrl: process.env.NEXT_PUBLIC_API_BASE_URL ?? "" });
```

```ts
// Expo (mobile) — apps/mobile/App.tsx, before rendering
import { configureApiClient } from "@repo/api-client";
configureApiClient({ baseUrl: process.env.EXPO_PUBLIC_API_BASE_URL ?? "" });
```

A trailing slash on `baseUrl` is trimmed automatically. Leaving it unconfigured (or passing `baseUrl: ""`) resolves every request to a same-origin relative URL — a sane default behind a reverse proxy that forwards API paths to the backend, and handy for local dev.

## Auth modes
`configureApiClient({ baseUrl, mode })` selects how this client authenticates — three modes, picked by RUNTIME, not preference:

| `mode` | Who | Credential | CSRF |
| --- | --- | --- | --- |
| `"session"` | **Every browser consumer — the preferred choice.** | Opaque `HttpOnly; Path=/` `session_id` cookie. No token, ever. | Echoed on every unsafe method. |
| `"bearer"` (the library default) | Expo/React Native, service-to-service. | Access token in memory (`Authorization: Bearer`), refresh in SecureStore. | None — no ambient credential. |
| `"cookie"` | Superseded — kept for a project mid-migration. | JWT refresh in an `HttpOnly; Path=/auth` cookie, access token still in memory. | Echoed on `/auth/refresh` + `/auth/logout` only. |

```ts
// Vite/Next (web) — before rendering, the browser default
import { configureApiClient } from "@repo/api-client";
configureApiClient({ baseUrl: import.meta.env.VITE_API_BASE_URL ?? "", mode: "session" });
```

```ts
// Expo (mobile) — App.tsx, before rendering; explicit for clarity
// (bearer is the library default, so omitting `mode` does the same thing)
import { configureApiClient } from "@repo/api-client";
configureApiClient({ baseUrl: process.env.EXPO_PUBLIC_API_BASE_URL ?? "", mode: "bearer" });
```

**Why `"session"` is preferred but not the library default.** This one module is shared by web and native consumers, and it cannot detect which runtime it's in — so the default has to be the mode that's safe to get wrong. A browser left on the `"bearer"` default simply fails to authenticate against a session-mode backend (a loud, obvious failure); a native app switched to `"session"` by a copy-pasted config would rely on cookie semantics Expo's HTTP client doesn't properly provide (a subtle one). Every web template in this catalog therefore passes `mode: "session"` explicitly — see `references/wiring/auth-end-to-end.md` for the full end-to-end flow and the reasoning behind preferring sessions for browsers at all.

### Session mode
1. **`credentials: "include"` on every request** — the browser attaches the `HttpOnly; Secure; SameSite=Lax; Path=/` `session_id` cookie and the non-HttpOnly `csrf_token` cookie.
2. **No `X-Auth-Mode` header at login** — session IS the backend's default, so there's nothing to declare.
3. **Double-submit CSRF echo on EVERY unsafe method** (`POST`/`PUT`/`PATCH`/`DELETE`), not just the auth endpoints — because the session cookie is `Path=/`, every state-changing endpoint is a CSRF target, unlike cookie mode's `Path=/auth`-scoped refresh cookie. `POST /auth/login` is exempt (the backend doesn't check it there either — see the auth component's README). Won't clobber a caller-supplied `X-CSRF-Token`.
4. **No `Authorization` header, ever** — `getAccessToken` is ignored entirely in this mode (see below). There is no token to inject; that's the property that leaves an XSS payload nothing to exfiltrate.

The login response body is `{ access_token: "", refresh_token: "", token_type: "session" }` — genuinely empty, not a placeholder. Check `token_type` to know which mode a response came from.

### Cookie mode (superseded)
`mode: "cookie"` (or the legacy `cookieMode: true`, still accepted — see below) turns on three things, and nothing else changes about the mutator's response shape:

1. **`credentials: "include"` on every request** — so the browser attaches the `HttpOnly; Secure; SameSite=Lax; Path=/auth` `refresh_token` cookie and the non-HttpOnly `csrf_token` cookie.
2. **`X-Auth-Mode: cookie` on `POST /auth/login`** — opts OUT of the backend's session default. Login returns `refresh_token: ""` in the body (the real refresh JWT is set as the `HttpOnly` cookie instead); the access token is still returned in the body and still travels in `Authorization`.
3. **Double-submit CSRF echo on `POST /auth/refresh` and `POST /auth/logout` only** — reads `csrf_token` from `document.cookie`, sends it back as `X-CSRF-Token`. Won't clobber a caller-supplied header.

**Migrating off cookie mode:** change `mode: "cookie"` to `mode: "session"` (or drop `cookieMode: true` entirely once `mode: "session"` is set) and stop wiring `getAccessToken` for that consumer — session mode ignores it. No other application code changes; the response shape difference (`token_type`) is the only thing to check for.

**`cookieMode: true` still works, mapped to `mode: "cookie"`**, so an existing call site keeps its prior behavior unchanged until it's migrated. An explicit `mode` always wins if both are passed.

**Security notes common to both cookie-bearing modes.** CSRF is the tradeoff a cookie brings — the browser attaches it automatically on any same-site request — so the guarded endpoints run the **double-submit** check: an attacker's forged cross-site request can't read the `csrf_token` cookie to echo it in the header, and `SameSite=Lax` blocks it besides. Reading `csrf_token` needs `document`, so the echo is a **safe no-op under SSR / React Native** (no `document`) — correct, because those are bearer-mode targets that never receive a CSRF cookie. Both modes require the backend's CORS to name **explicit origins with credentials enabled — never a `*` wildcard** (incompatible with `credentials: "include"`); see `references/security/secure-baseline.md` and the auth component's README.

## Access-token injection (`getAccessToken`)
Bearer and cookie mode send the short-lived access token as an `Authorization: Bearer` header — but by **default** the client sets that header for no one: it forwards whatever `Authorization` the caller passes and nothing more. A consumer that keeps the access token in memory can instead register a getter once, and the mutator injects the header on every generated call automatically:

```ts
// The token lives in the consumer's memory (e.g. Expo's own SecureStore-
// backed auth state). Wire it once:
import { configureApiClient } from "@repo/api-client";
configureApiClient({
  baseUrl: process.env.EXPO_PUBLIC_API_BASE_URL ?? "",
  mode: "bearer",
  getAccessToken,         // () => string | null
});
```

Semantics (see `src/mutator.ts`'s `ApiClientConfig.getAccessToken`):
- **Default-off.** Omit it and the mutator sets no `Authorization` header itself.
- **Injects only a real token.** The getter returning `null` or `""` injects nothing (logged-out state).
- **Never clobbers a caller header.** A per-call `Authorization` still wins, so an explicit override (or a different token for one request) is always honored.
- **Runs in bearer and cookie mode; IGNORED in session mode.** Session mode holds no token — a stray `Authorization` header would be meaningless at best, and against a backend that resolves either credential (see the auth component's `build_get_current_principal_either`), actively ambiguous about which one authenticated the request.

## Bundler-only package
`dist/` (this package's build output) uses extensionless relative imports, matching what orval generates — not the explicit `.js`-suffixed imports Node's own ESM loader (`NodeNext` resolution) requires. That's intentional (see "Materialized-location paths" below) and it means this package only resolves correctly under a bundler with Node-style extensionless resolution — Vite, Metro, webpack — not `node dist/index.js` directly. Don't add a build step to emit extensions; consume it from a bundler-based app as designed.

## The mutator's response shape
Orval's `fetch` client mode expects the mutator to resolve `{ data, status, headers }`, not just the parsed body — the generated response types (e.g. a 201-vs-422 union) are discriminated on `.status`, so callers pattern-match on it instead of relying on a thrown error for a documented non-2xx response. `customFetch` in `src/mutator.ts` builds that shape; a rejected promise is reserved for what the OpenAPI contract can't describe — a network failure, not a 4xx/5xx the schema documents.

**Response shape covers documented statuses only.** The generated union is built from whatever status codes the OpenAPI schema documents per operation (e.g. 200/201/422) — it does not include statuses the backend can still return but the schema doesn't declare (a proxy's 502/503, a load balancer's 429, an unhandled 500). Those resolve with `.status` outside the typed union, so any code that pattern-matches on `.status` needs a `default`/fallback branch alongside the documented cases, not an exhaustive switch that assumes the union is the full set of possible responses.

## Dep vs peerDep
`react` and `@tanstack/react-query` are **peerDependencies** here, pinned again as exact-ish `devDependencies` for this package's own build/lint/test. Rationale: this package is imported by both a web app and a mobile app, each with its own React tree and its own `QueryClient`. If this package declared `react`/`@tanstack/react-query` as regular `dependencies`, pnpm could resolve a second copy in a consumer whose own version differs even slightly — React's hook rules require exactly one `react` instance in the tree, and TanStack Query hooks require exactly one `QueryClient` provider matching the `@tanstack/react-query` instance the hooks were built against. Peer dependencies make the consumer supply (and own) that single instance; the `devDependencies` entries here exist only so this package's own `build`/`typecheck`/`lint`/`test` scripts have something to compile and test against locally.

## Materialized-location paths
`tsconfig.json`'s `extends` and `eslint.config.mjs`'s import of the root config are both written as `../../<file>` — correct for this package's **materialized** location (`<project>/packages/api-client/`, two levels below the project root), not for where this file sits inside the plugin marketplace repo (`templates/packages/api-client/`, a sibling of `templates/monorepo/`). Don't "fix" these to be relative to the plugin marketplace repo; they're intentionally scaffolding-relative. See the inline comment at the top of each file.

`tsconfig.json` also overrides the root's `module`/`moduleResolution` (`NodeNext`/`NodeNext`) to `ESNext`/`bundler`: this package is consumed by bundler-based apps (Vite for web, Metro for mobile) rather than executed directly under Node's ESM loader, and orval's generated imports don't carry the explicit `.js` extensions `NodeNext` resolution requires. `tsconfig.build.json` extends `tsconfig.json` and additionally excludes `*.test.ts`, so `pnpm run build`'s `dist/` doesn't ship test files while `pnpm run typecheck` still type-checks them.

## Stage 3: the live schema
`openapi.json` **is** the live schema now — not a hand-built fixture. It's a committed, point-in-time export of `plugins/dev-lifecycle/templates/backend/fastapi`'s actual OpenAPI 3.1 output, produced by that block's `python -m app.export_openapi` (see that block's README's "OpenAPI export" section): the real `ErrorEnvelope` error shape (not FastAPI's native `HTTPValidationError`/`ValidationError` pair — the backend block's `app/main.py` remaps its own schema to match what it actually sends, see `_install_error_envelope_openapi`), `Page[ItemOut]`, `/items`, `/health`, `/readyz`, the `/auth/*` stubs, and the bearer security scheme. `orval.config.ts`'s `input.target` points at this file directly.

The now-retired `openapi.sample.json` fixture served the same shaping purpose before a real backend existed to export from — that stand-in is gone; `client-generate`'s recipe and everything downstream of it (the mutator, `src/index.ts`'s exports) are unchanged by the swap.

**Regenerating from a real project (materialized, not this template repo):** `just client-generate` re-exports the schema fresh from `apps/api` (`python -m app.export_openapi > packages/api-client/openapi.json`, run from the FastAPI app's own environment) and then runs `pnpm --filter @repo/api-client run generate` (orval) against the freshly exported file — so the committed `openapi.json` and the generated client it drives never drift from what the backend block actually serves. Re-run it any time the backend's routes/schemas change, and commit both the updated `openapi.json` and the regenerated `src/generated/**` diff together.

## Testing
`pnpm run test` runs `vitest run` against `src/mutator.test.ts` — a smoke test of the mutator's request/response handling (JSON body parsing, the `{data, status, headers}` shape, default-`Content-Type` merging) using a stubbed global `fetch`. It does not exercise the generated hooks themselves (those are orval's output, not this package's logic to test) — a consuming app's component tests are where hook usage gets covered, per `references/testing/frontend-testing.md`.
