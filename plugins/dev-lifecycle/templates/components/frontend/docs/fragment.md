<!-- fragment: block:components/frontend -->

## Setup
`@repo/web-shared` is the shared web layer over `@repo/api-client` (session-mode
`AuthProvider` + route guards, a `QueryClient` factory, error/JWT/form helpers).
A consuming app wires it once at startup:

1. `configureApiClient({ baseUrl, mode: "session" })` — session mode holds no
   token, so there is no `getAccessToken` to wire. Source `baseUrl` from your
   framework's env var (`VITE_API_BASE_URL` / `NEXT_PUBLIC_API_BASE_URL`).
2. Mount `<QueryClientProvider client={createQueryClient()}><AuthProvider
   onAuthExpired={/* redirect to login */}>…</AuthProvider></QueryClientProvider>`
   — `AuthProvider` must sit inside the `QueryClientProvider`.
3. Gate protected UI with `<RequireAuth>` / `<RequireRole role="admin">`, passing
   your router's redirect as the `fallback` (the guards never navigate).
4. Wrap generated calls in `unwrap(...)` inside your `queryFn`/`mutationFn` so a
   401 surfaces as an error and drives the session-invalidated notification.

Requires a session-mode auth backend (`/auth/login|logout|me` + a role-gated
route) with credentialed CORS naming the web origin — see
`references/wiring/auth-end-to-end.md`.

## Maintenance
`react`, `@tanstack/react-query`, `react-hook-form`, `zod`, and
`@hookform/resolvers` are peer dependencies pinned via
`references/compatibility-matrix.md` (Frontend/web + Frontend testing rows), not
bumped independently. Run `pnpm --filter @repo/web-shared test` (vitest + jsdom +
MSW) after changing the auth/query/forms logic; the suite covers the session-mode
login/logout/expiry lifecycle — there is no refresh step to test, since a
session's idle deadline slides forward as a side effect of being used. When the
backend's OpenAPI schema changes, re-run `just client-generate` first so the
generated hooks this package imports stay current.
