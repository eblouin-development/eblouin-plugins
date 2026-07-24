<!--
library: expo-router
versions-covered: "expo-router ~57.0.7 (via Expo SDK 57)"
last-verified: 2026-07-23
provenance: manual
versions-pinned-to: references/compatibility-matrix.md
sources:
  - https://docs.expo.dev/router/introduction/
  - https://docs.expo.dev/router/basics/layout/
  - https://docs.expo.dev/router/reference/authentication/
  - https://docs.expo.dev/router/advanced/root-layout/
-->

# Expo Router (navigation) conventions

Granular guidance for navigation in an Expo-managed app using **Expo Router** — the SDK-57 first-party, file-based router. Read this after detecting `expo-router` in `package.json`. Everything here is subordinate to the project's existing conventions. Pairs with `expo.md` and the auth wiring in `references/wiring/auth-end-to-end.md`.

## Contents
- Version check (do this first)
- File-based routing model
- Layouts and the root layout
- Route groups
- The auth-gate pattern (the important one)
- Navigating and required peers

## Version check (do this first)
Expo Router is versioned with the SDK — `npx expo install expo-router` resolves the SDK-57 version (`~57.0.7`), and its required peers **`react-native-screens`** and **`react-native-safe-area-context`** must be installed the same way (`npx expo install`), never hand-picked. `app.json` needs `scheme` set (deep-linking) and, in SDK 57, the `expo-router` config plugin. See `references/compatibility-matrix.md`'s Mobile section for the pinned versions.

## File-based routing model
Routes are **files under `app/`**, not a route config object — the file tree *is* the navigation tree (the same idea as Next.js App Router, which the web side already uses):
- `app/index.tsx` → the `/` route.
- `app/login.tsx` → `/login`.
- A file's default-exported React component is the screen.
- **`app/_layout.tsx`** is special: it's the layout wrapping every route in its directory (not itself a route).
- The **root `app/_layout.tsx`** is the app's entry — it wraps the whole tree and is where global providers (the auth context, React Query's `QueryClientProvider`, `SafeAreaProvider`) and the `configureApiClient({ baseUrl })` call belong, so they're mounted before any screen renders.

## Layouts and the root layout
A `_layout.tsx` exports a navigator — commonly `<Stack>` (from `expo-router`) — and renders child routes through it. The root layout is the single place to:
1. Configure `@repo/api-client` once (`configureApiClient({ baseUrl: process.env.EXPO_PUBLIC_API_BASE_URL ?? "", mode: "bearer" })`) — bearer mode, `mode` passed EXPLICITLY (the backend now defaults browser clients to a session cookie, so an omitted mode is no longer safe on native).
2. Mount providers: `QueryClientProvider`, `SafeAreaProvider`, and the app's `AuthProvider`.
3. Drive the **auth gate** (below).

## Route groups
A directory whose name is in **parentheses** — `app/(auth)/`, `app/(app)/` — is a **route group**: it organizes routes and gives them a shared layout **without adding a path segment** (`app/(app)/index.tsx` is still `/`, not `/(app)`). This kit uses two:
- **`(auth)`** — the public, unauthenticated group (e.g. `app/(auth)/login.tsx`).
- **`(app)`** — the protected group; every screen here assumes an authenticated user (e.g. `app/(app)/index.tsx`, the landing screen).

Each group gets its own `_layout.tsx`, so the protected group's layout can enforce the gate for everything under it in one place.

## The auth-gate pattern (the important one)
The protected/public split is enforced by **redirecting on auth state**, read from the auth context, in the layouts — not by hiding buttons. The SDK-57 idiom:

- The **root layout** subscribes to the auth context. While auth state is still resolving (reading the refresh token out of SecureStore on cold start is async), render a splash/loading state — do **not** flash a screen you might immediately redirect away from.
- Once resolved, redirect based on state and current group. The clean expression is a **`<Redirect>`** (declarative) or a `useEffect` + `router.replace`:
  - **Unauthenticated** and currently inside `(app)` → `<Redirect href="/login" />`.
  - **Authenticated** and currently inside `(auth)` → `<Redirect href="/" />` (into the protected landing).
- Use `replace`/`<Redirect>`, never `push` — a redirected-away auth screen must not sit on the back stack.
- The **protected landing** (`app/(app)/index.tsx`) proves end-to-end bearer auth by calling the generated **`useMeAuthMeGet()`** hook (`/auth/me`) and rendering the returned principal (id, email) plus the roles carried in the access token — the mobile analog of the web's `/admin/ping` smoke, the smallest proof the bearer token reaches a protected endpoint and comes back. No product screen is required for the template.

This keeps authorization in one auditable place and makes "can this user see this?" a property of the route tree, not scattered conditionals.

## Navigating and required peers
- Navigate declaratively with **`<Link href="...">`** (from `expo-router`) or imperatively with **`useRouter()`** → `router.push` / `router.replace` / `router.back`.
- `useLocalSearchParams()` reads route params; `useSegments()` tells a layout which group it's currently in (useful for the gate's "am I in `(auth)`?" check).
- `expo-router` **requires** `react-native-screens` and `react-native-safe-area-context` — they're not optional add-ons; install them via `npx expo install` alongside the router. The root layout wraps the tree in `SafeAreaProvider` from the latter.
