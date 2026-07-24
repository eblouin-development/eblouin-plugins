---
name: "mobile"
description: "Build, modify, or review a React Native / Expo mobile app following modern best practices. Use this skill WHENEVER the work involves the mobile client layer — Expo/React Native screens, navigation, native modules, mobile auth, or wiring the app to the shared API client. Trigger it for \"build the mobile screen for X\", \"add a route to the app\", \"wire up mobile login\", \"store the token securely\", or any mobile task that follows a plan. Before writing any mobile code, this skill ALWAYS detects the project's existing stack and the exact versions and conforms to what's already there rather than imposing a different framework."
---

# Mobile

Build a mobile app that fits the project as it actually is — not a generic template. The single biggest source of bad mobile work is assuming a stack, an SDK version, or a storage/auth posture instead of checking. This skill front-loads that check, then writes code that matches the project's real conventions, Expo SDK, and version.

## Core rules

- **Detect before you build.** Never assume the framework, Expo SDK, navigation library, storage approach, or conventions. Read the project first (step 1). Not optional.
- **Conform, don't convert.** If the project is Expo-managed, stay managed. If it uses Expo Router, write Expo Router — don't introduce React Navigation by hand, a different state library, or a bare-workflow escape without the user asking. Match existing patterns even when you'd personally choose differently.
- **Version dictates idiom.** RN is pinned *through* the Expo SDK — read `expo`, not `react-native`, and install SDK-governed packages with `npx expo install`, never `pnpm add` (which grabs `latest` and can pull a version RN's native side wasn't built against). The "modern" way depends on the installed SDK — check it, then write for that SDK.
- **Native is not web.** No DOM, no `localStorage`, no cookies. The refresh token goes in `expo-secure-store` (never AsyncStorage, never a cookie); the access token stays in memory; auth is **bearer mode, configured explicitly** (`configureApiClient({ mode: "bearer" })`) — the backend's own default is now a server-side session (the browser posture), so an unconfigured/omitted mode would hand this app a cookie it cannot properly store instead of the token pair it needs. Never import web components into React Native.
- **Work context-efficiently.** Detect from `package.json`/lockfile, not by reading the tree; read one or two representative screens for house style; load only the reference(s) for what's actually present. See `${CLAUDE_PLUGIN_ROOT}/shared/token-efficiency.md`.
- **Implement against a plan when one exists.** Build to the plan; if there's no plan and the work is non-trivial, suggest planning first.

## Workflow

### 1. Detect the stack (always)
Read **`package.json`** and the **lockfile** for: `expo` (and its SDK major — this governs everything), `react-native`, the router (`expo-router` vs `@react-navigation/*`), `react` and its major, TypeScript, secure-storage (`expo-secure-store`), the test runner, and whether `@repo/api-client` is a dependency. Check for `app.json`/`app.config.*` and whether `android/`/`ios/` are committed (**bare**) or absent (**managed**). Read the root `app/_layout.tsx` (or the navigation entry) and one or two representative screens to mirror house style.

State what you found in a line, e.g. "Expo SDK 57 (managed) + expo-router + React 19 + TypeScript + expo-secure-store — following those."

### 2. Choose the approach (greenfield / open-ended only)
If a stack exists, step 1 already decided. Only when starting fresh: reach for **Expo (managed workflow) + Expo Router** — the kit default (`templates/mobile/expo`). Stay managed unless a required native dependency genuinely forces bare (see `native-modules.md`'s decision order). State the choice and reason.

### 3. Build
Load only the references for what's actually present:
- **Always** → `${CLAUDE_PLUGIN_ROOT}/references/mobile/expo.md` (managed workflow, `app.json`, `EXPO_PUBLIC_*` env, EAS) and `react-native.md` (primitives, storage tiers, bearer+SecureStore, AppState).
- **Expo Router** → `${CLAUDE_PLUGIN_ROOT}/references/mobile/navigation.md` (file-based routes, route groups, the auth-gate pattern).
- **A native capability beyond JS** → `${CLAUDE_PLUGIN_ROOT}/references/mobile/native-modules.md` (SDK modules vs config plugins vs bare; dev builds).
- **Auth** → `${CLAUDE_PLUGIN_ROOT}/references/wiring/auth-end-to-end.md` — implement the **bearer half** exactly: `configureApiClient({ mode: "bearer" })` called EXPLICITLY (never omitted — the backend defaults browser clients to a session cookie now, so an unconfigured client silently gets the wrong credential shape); access token in memory; refresh token in SecureStore; `Authorization: Bearer`; silent single-flight refresh on 401 with one retry; rotation overwrites SecureStore; refresh-401 is terminal → clear + redirect to login; logout posts the refresh token then clears. Session mode is **never** enabled on native.
- **Pull from the catalog first** — check `${CLAUDE_PLUGIN_ROOT}/templates/components/*` and `${CLAUDE_PLUGIN_ROOT}/references/recipes/*` (e.g. `push-notifications`) before hand-building a cross-cutting feature; a recipe composes existing pieces across backend/frontend/mobile rather than inventing new wiring.
- If a significant mobile library has **no reference yet**, generate one from current official docs, use it now, and PR it into the plugin (self-extend flow).

All paths: match existing conventions; accessibility is not optional (`accessibilityRole`/`accessibilityLabel`, contrast, touch targets ≥ 44pt, keyboard/screen-reader operability); handle loading/empty/error states; keep screens small; if TypeScript is present, type honestly (no `any` escape hatch). Configure `@repo/api-client` once at the app entry (root layout) in **explicit** bearer mode (`mode: "bearer"`); consume the generated hooks — don't hand-write `fetch`.

### 4. Hand off
Summarize what changed (files, screens, routes) so it's reviewable. Be explicit about the **verification boundary**: hermetic checks (`tsc`, `eslint`, `vitest` on logic like the auth context) run here; **device/simulator builds, `expo prebuild`, and live auth against a running backend are documented-manual** — say so rather than claiming a green device build. Note follow-ups left out of scope. The bar for merge-ready is `${CLAUDE_PLUGIN_ROOT}/shared/definition-of-done.md`.

**Doc upkeep.** If the change touches what `apps/mobile` exposes or needs (a new env var, a new native capability), update `apps/mobile/docs/fragment.md` and run `just docs-generate` so the root README stays accurate — never hand-edit an aggregated region of the root README directly.

## What this skill does NOT do
- Assume a framework, Expo SDK, or version without checking.
- Assume a stack — detect it; don't impose Expo on a project that isn't Expo, or React Navigation on an Expo Router app.
- Import web components into React Native, or reuse web DOM/`localStorage`/cookie patterns on native.
- Enable session or cookie mode on native, or omit `mode` when configuring `@repo/api-client` — mobile is bearer mode, configured explicitly, with the refresh token in SecureStore.
- Build the backend/API (that's the backend skill — this skill consumes the API via `@repo/api-client`, it doesn't define it).
- Claim a device/native build passed when only the hermetic JS checks ran in-sandbox.
