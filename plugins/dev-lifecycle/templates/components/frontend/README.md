<!--
block: components/frontend                 # catalog component (shared pnpm workspace package @repo/web-shared)
needs:
  - shared workspace package: @repo/api-client (workspace:*) — generated hooks/models + the fetch mutator
  - peers from the app: react, @tanstack/react-query, react-hook-form, zod, @hookform/resolvers (one instance each)
  - app wiring: configureApiClient({ baseUrl, mode: "session" }) + the QueryClientProvider/AuthProvider mount
  - a session-mode auth backend (references/wiring/auth-end-to-end.md)
exposes:
  - workspace package: @repo/web-shared — portable session-mode AuthProvider + guards, QueryClient factory, error/JWT/form helpers
  - its co-located doc fragment: docs/fragment.md
versions-pinned-to: references/compatibility-matrix.md
last-verified: 2026-07-24
provenance: manual
-->

# @repo/web-shared

The shared React building blocks every **web** frontend imports on top of `@repo/api-client`: the session-mode `AuthProvider` + route guards, a `QueryClient` factory with auth-aware error handling, error/JWT helpers, and zod form helpers. Lives at `templates/components/frontend/` in this repo; scaffolding materializes it into `<project>/packages/web-shared/`, a sibling of `packages/api-client` under the same pnpm workspace (see "Materialized-location paths").

**This provider holds no token of any kind.** It authenticates against the backend's server-side session (an `HttpOnly` cookie), the DEFAULT and preferred transport for a browser client — see `references/wiring/auth-end-to-end.md` and the auth component's `_sessions.py` module docstring for why. An earlier version of this component implemented the JWT/cookie-mode lifecycle (an access token in React state, single-flight refresh, decoded claims); that machinery is gone, not merely disabled — there is no refresh endpoint on the session path to call. A project that still needs a bearer-authenticated web client (unusual — that's normally the mobile posture) wires `@repo/api-client` directly rather than through this provider.

It is deliberately **framework-portable**: no `react-router`, no `import.meta`, and no `document`/`window` access at module top level anywhere in the package. That's what lets the *same* package import cleanly into a Vite SPA (Stage 6's app block) and into a Next.js client component (Stage 7) — the guards are render-gate primitives the app supplies its own router redirects to.

## Contents
- Composition contract
- What it is / isn't
- The export surface
- Wiring (what the app does)
- The cookie-mode auth lifecycle
- Portability constraints
- Dep vs peerDep
- Materialized-location paths
- Testing

## Composition contract

**NEEDS**
- **`@repo/api-client`** (`workspace:*`) — the generated hooks (`useLoginAuthLoginPost`, `useLogoutAuthLogoutPost`, `useMeAuthMeGet`, `adminPingAdminPingGet`, …), the models (`ErrorEnvelope`/`ErrorCode`/`TokenResponse`/`PrincipalOut`), and the `configureApiClient` seam. The app must have run `just client-generate` so those tags exist; the `auth`/`admin` barrel exports landed in Stage 6.
- **Peer instances from the consumer** — `react`, `@tanstack/react-query`, `react-hook-form`, `zod`, `@hookform/resolvers`. One instance of each, owned by the app (see "Dep vs peerDep").
- **Runtime wiring by the app** — `configureApiClient({ baseUrl, mode: "session" })` once at startup, and the provider mount (see "Wiring"). This package does not call `configureApiClient` itself.
- **A session-mode auth backend** — the `/auth/login|logout|me` endpoints (session mode is the backend's default — no `X-Auth-Mode` header needed at login), at least one role-gated route (`/admin/ping`), and credentialed CORS naming the web origin. See `references/wiring/auth-end-to-end.md`.

**EXPOSES**
- **Workspace package `@repo/web-shared`** — import from its root (`index.ts`); it re-exports every public symbol. See "The export surface".
- **Its co-located doc fragment** — `docs/fragment.md`, aggregated into the project root README by `just docs-generate`.

## What it is / isn't
- **Is:** the portable web layer between `@repo/api-client` and a specific app — auth lifecycle, query defaults, error mapping, and form plumbing that every web frontend needs identically, written once so a Vite SPA and a Next.js app share it.
- **Isn't:** an app. It ships no routes, no pages, no styling system, and no router. The guards render `children` vs a `fallback` — the *app* decides what the fallback is (a redirect, a login prompt). It also isn't a place for API-calling code that belongs in `@repo/api-client`'s mutator.

## The export surface
Everything is a root export of `@repo/web-shared`:

| Area | Exports |
| --- | --- |
| **auth** | `AuthProvider` (+ `AuthProviderProps`), `useAuth`, `AuthContext`, `AuthContextValue`/`AuthState`, `RequireAuth`, `RequireRole` |
| **query** | `createQueryClient` (+ `CreateQueryClientOptions`) |
| **errors** | `ApiError`, `isApiError`, `unwrap` (+ `ApiResult`), `isErrorEnvelope`, `getErrorCode`, `errorCodeToMessage`, `ApiErrorBoundary` |
| **jwt** | `decodeAccessTokenClaims` (+ `AccessTokenClaims`) — a standalone utility this provider no longer uses itself (see "The session-mode auth lifecycle"); kept for a project that still runs a bearer-authenticated web client. |
| **forms** | `useZodForm`, `FieldError`, `applyEnvelopeToForm` |

No token getter is exported — session mode holds no token for `configureApiClient`'s `getAccessToken` to read (see `@repo/api-client`'s own README). The one seam that still matters for wiring:
- **`unwrap`** — wrap a generated call in your `queryFn`/`mutationFn` (`unwrap(await meAuthMeGet())`) so orval's "401-resolves-as-data" becomes a thrown `ApiError`. Without it, react-query never sees a 401 as an error and the session-invalidated notification (see below) can't fire.

## Wiring (what the app does)
`AuthProvider` must be mounted **inside** a `QueryClientProvider` (it uses the generated hooks). One-time startup wiring:

```ts
// apps/web/src/main.tsx (Vite) — before rendering
import { configureApiClient } from "@repo/api-client";
configureApiClient({
  baseUrl: import.meta.env.VITE_API_BASE_URL ?? "",
  mode: "session",   // the web posture — see @repo/api-client's README
});
```

```tsx
// The provider tree
import { QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider, createQueryClient } from "@repo/web-shared";

const queryClient = createQueryClient(); // no-retry-on-401/403 + auth-aware onError

export const App = () => (
  <QueryClientProvider client={queryClient}>
    <AuthProvider onAuthExpired={() => router.navigate("/login")}>
      {/* routes; guards get router redirects as their `fallback` */}
    </AuthProvider>
  </QueryClientProvider>
);
```

The guards never navigate — the app supplies the redirect:

```tsx
<RequireRole role="admin" fallback={<Navigate to="/" replace />}>
  <AdminPage />
</RequireRole>
```

Next.js (Stage 7): identical, but the wiring lives in a `"use client"` module — this package intentionally omits its own `"use client"` directive so the consumer owns that boundary, and reads `process.env.NEXT_PUBLIC_API_BASE_URL` instead of `import.meta.env`.

## The session-mode auth lifecycle
`AuthProvider` implements `references/wiring/auth-end-to-end.md`'s web (session) flow verbatim — and there is meaningfully LESS of it than the JWT/cookie-mode version this component used to implement, because a session has no refresh step and holds no token to manage:
- **`GET /auth/me` IS the "am I logged in" signal.** With a cookie-borne credential there is no in-memory flag the way a held access token used to be, so `useMeAuthMeGet` runs unconditionally (`retry: false`, so an honest "not logged in" 401 isn't retried) and `isAuthenticated`/`principal` are derived straight from its result.
- **Login** (`useLoginAuthLoginPost`) — no `X-Auth-Mode` header (session is the backend's default); the response body carries no token (see `TokenResponse`'s own docstring: both fields are `""` in session mode) — the backend already set the `HttpOnly` cookie. On success this refetches `/auth/me` and invalidates every other cached query, so nothing from a prior identity in this tab leaks into the freshly-authenticated one.
- **No refresh, ever.** A session's idle deadline slides forward as a side effect of the backend resolving the cookie on every authenticated request (see the auth component's `_sessions.py`) — there is no endpoint to call and nothing for this provider to single-flight.
- **Session invalidation** — a 401 from ANY call (surfaced via `unwrap` → `ApiError` → the `QueryClient`'s `onError`) notifies through the module-scoped auth bridge (`authBridge.ts`): `AuthProvider` clears its cached principal and fires `onAuthExpired` (the app typically redirects to login). There is no retry and no recovery attempt — a 401 on the session path always means the session is already gone.
- **Logout** (`useLogoutAuthLogoutPost`, called with no body — a session client has no refresh token to send) — best-effort server call (revokes the session server-side, the half that actually matters), then `queryClient.clear()`.

`createQueryClient` supplies the other half: **no retry on 401/403** (there's nothing to retry against on the session path), and a `QueryCache`/`MutationCache` `onError` that notifies the same bridge and runs any injected `onAuthExpired`. `decodeAccessTokenClaims` is exported but **not used by this provider** — it remains a standalone, UX-only utility (no signature check — the server's 403 is the real gate) for a project that still runs a bearer-authenticated web client.

## Portability constraints
Enforced by the "no router / no bundler globals / no SSR-unsafe module-load" rule:
- **No `react-router`** — the guards are render-gates; the app owns navigation.
- **No `import.meta` / `process.env`** in this package — `baseUrl` comes via `configureApiClient` in the app, per framework.
- **No top-level `document`/`window`** — `decodeAccessTokenClaims` calls `atob`/`TextDecoder` *inside* the function; the auth bridge is plain module state (a listener set, nothing session-specific), which behaves identically whether or not a server render ever runs it.

## Dep vs peerDep
`react`, `@tanstack/react-query`, `react-hook-form`, `zod`, and `@hookform/resolvers` are **peerDependencies** (pinned again as `devDependencies` for this package's own build/lint/test), for the same reason `@repo/api-client` makes react/react-query peers: hooks, a single `QueryClient`, and RHF's `FormProvider` context all require exactly one instance in the consumer's tree. `@repo/api-client` is a real (`workspace:*`) **dependency** — this package is a layer on top of it, not a peer of it. All version lines follow `references/compatibility-matrix.md` (Frontend/web + Frontend testing), not independent bumps.

## Materialized-location paths
`tsconfig.json`'s `extends` and `eslint.config.mjs`'s import of the root config are written as `../../<file>` — correct for the **materialized** location (`<project>/packages/web-shared/`, two levels below the project root), exactly as `@repo/api-client` does it. Don't "fix" them to be relative to the plugin marketplace repo. `tsconfig.json` also overrides the base's `NodeNext` module resolution to `bundler` (this package is consumed by Vite/Metro/Next bundlers, and its imports — like the generated client's — are extensionless), and `tsconfig.build.json` excludes `*.test.tsx` so `dist/` ships no test files while `typecheck` still checks them.

## Testing
`pnpm run test` runs `vitest run` under a jsdom environment (`vitest.config.ts`) with `@testing-library/react` and **MSW** (`setupServer`) intercepting the api-client mutator's `fetch` at the network boundary — the real data-fetching path, per `references/testing/frontend-testing.md`. The suite proves the load-bearing behavior: login sends no `X-Auth-Mode` header (session is the default), holds no token, and surfaces the `/auth/me` principal including `roles`; a user without the required role does not see role-gated UI; logout echoes `X-CSRF-Token` and clears the principal; and a 401 from a non-auth call notifies session-invalidated and fires `onAuthExpired` with **no** `/auth/refresh` call ever attempted (MSW's `onUnhandledRequest: "error"` would fail the test if the provider tried) — plus `applyEnvelopeToForm` mapping a 422 and `ApiErrorBoundary` catching an `ApiError`.
