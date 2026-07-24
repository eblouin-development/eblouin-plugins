<!--
library: expo
versions-covered: "SDK 57 (React Native 0.86)"
last-verified: 2026-07-23
provenance: manual
versions-pinned-to: references/compatibility-matrix.md
sources:
  - https://docs.expo.dev/
  - https://docs.expo.dev/workflow/overview/
  - https://docs.expo.dev/guides/environment-variables/
  - https://docs.expo.dev/build/introduction/
  - https://expo.dev/changelog/sdk-57
-->

# Expo conventions

Granular guidance for building an Expo-managed React Native app. Read this after detecting the project uses Expo (an `expo` dependency + `app.json`/`app.config.*`). Everything here is subordinate to the project's existing conventions — when they conflict, the project wins. Pairs with `react-native.md` (runtime fundamentals), `navigation.md` (Expo Router), and `native-modules.md` (the managed-workflow module boundary).

## Contents
- Version check (do this first)
- The managed workflow (and why the kit uses it)
- Config: `app.json` / `app.config.*`
- Environment variables (`EXPO_PUBLIC_*`)
- Prebuild, dev clients, and native code
- Building & submitting (EAS)
- What runs where in this kit

## Version check (do this first)
The Expo **SDK** version governs everything else — React Native, the `expo-*` packages, and the peer libraries navigation needs — as a single coordinated set. Confirm it from `package.json` (`expo`) and never hand-pin a `react-native`, `expo-router`, `react-native-screens`, or `react-native-safe-area-context` version against it. Install/upgrade SDK-governed packages with **`npx expo install <pkg>`** (which resolves the version the installed SDK bundles) rather than `pnpm add <pkg>` (which grabs `latest` and can pull a version RN's native side hasn't been built against). This kit pins Expo SDK **57** / RN **0.86**; the exact resolved peer versions are in `references/compatibility-matrix.md`'s Mobile section.

## The managed workflow (and why the kit uses it)
Expo has two workflows: **managed** (you write only JS/TS; Expo owns the `android/`/`ios/` native projects, generating them on demand) and **bare** (the native projects are committed and hand-edited). This kit's `templates/mobile/expo` block is **managed**: no `android/`/`ios/` directories are committed, the native projects are a build-time artifact (`npx expo prebuild` / EAS), and everything the app needs is either an Expo SDK module or a config-plugin-compatible library. Managed is the right default — it keeps the app upgradable (SDK bumps regenerate native code) and reviewable (the diff is JS, not two parallel native trees). Drop to bare only when a required native dependency has no config plugin, and treat that as a deliberate, documented exit from managed.

## Config: `app.json` / `app.config.*`
App identity and native build config live in **`app.json`** (static) or **`app.config.ts`/`app.config.js`** (dynamic — use when a value must be computed or read from the environment at build time). Key fields: `name`, `slug`, `scheme` (the deep-link/URL scheme Expo Router needs for routing), `ios.bundleIdentifier`, `android.package`, and `plugins` (config plugins that inject native config at prebuild). Expo Router requires `scheme` set and (for typed routes) the `expo-router` plugin. Prefer `app.json` for a static template; reach for `app.config.ts` only when you actually need dynamism — a static file is easier to diff and reason about.

## Environment variables (`EXPO_PUBLIC_*`)
Expo statically inlines **only** variables prefixed `EXPO_PUBLIC_` into the JS bundle at build time — a bare `API_BASE_URL` read via `process.env` in app code silently resolves to `undefined`/`""` even when the shell has it set. So:

- **Client config that's safe to ship** (the API base URL) → `EXPO_PUBLIC_API_BASE_URL`, read as `process.env.EXPO_PUBLIC_API_BASE_URL`. This is exactly what `@repo/api-client` expects the consumer to pass: `configureApiClient({ baseUrl: process.env.EXPO_PUBLIC_API_BASE_URL ?? "" })` at app entry.
- **`EXPO_PUBLIC_*` is NOT secret.** Inlining means the value is embedded in the shipped bundle and trivially extractable from the app binary. Never put an API secret, signing key, or private token in an `EXPO_PUBLIC_*` var. Secrets that must stay secret belong on the backend, or — for a per-user token minted at runtime — in **`expo-secure-store`** (see `react-native.md`), never in a build-time env var and never in the JS bundle.
- Values are inlined at **build** time, not read at runtime — changing an `EXPO_PUBLIC_*` var requires a rebuild, not just a restart.

## Prebuild, dev clients, and native code
`npx expo prebuild` generates the native `android/`/`ios/` projects from `app.json` + config plugins. In managed flow you rarely run it by hand — EAS runs it during a cloud build, and `npx expo run:ios`/`run:android` run it for a local native build. The plain **`npx expo start`** dev server (Metro bundler) serves JS to Expo Go or a dev client without any native build. **A pure-JS change never needs a native rebuild; adding or configuring a native module does.** In this sandbox there is no simulator/emulator and no Apple/Google toolchain, so `expo prebuild`, `expo run:*`, and any device/simulator build are **documented-manual** — they are verified on a real dev machine, not here (see the `templates/mobile/expo` README's verification section).

## Building & submitting (EAS)
Production/preview builds go through **EAS Build** (`eas build --profile <preview|production>`), which runs prebuild + the native compile in Expo's cloud and returns an `.ipa`/`.aab`; `eas submit` uploads to the App Store / Play Console. Build profiles live in `eas.json`. `EXPO_PUBLIC_*` vars for a build come from EAS environment variables (or `--env`), set per profile. This is a deploy concern — the block documents it in its fragment's Deployment/Secrets sections rather than wiring it into `just deploy` (which stays owned by the infra block).

## What runs where in this kit
- **JS/TS app code** — `app/` (Expo Router routes) + `src/` (auth context, the SecureStore seam). Bundled by Metro. This is the only code in the managed template.
- **`@repo/api-client`** — the shared workspace package, imported for the generated auth hooks; configured once at entry in **bearer mode, explicitly** (`configureApiClient({ baseUrl, mode: "bearer" })` — the backend's own default is now a server-side session, so `mode` must be passed, not omitted). Never enable session or cookie mode on native.
- **Hermetic checks** (`tsc`, `eslint`, `vitest`) run in CI and in-sandbox against the JS layer. **Device/native builds** (`expo prebuild`, EAS, live auth against a running backend) are documented-manual.
