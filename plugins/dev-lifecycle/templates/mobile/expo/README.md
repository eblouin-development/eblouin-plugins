<!--
block: mobile/expo
needs:
  - env vars: EXPO_PUBLIC_API_BASE_URL (the backend origin; read at app entry and passed to configureApiClient — inlined at build time, NOT a secret)
  - upstream API contract: @repo/api-client (the generated typed client) in BEARER mode; the backend's /auth/* + /auth/me endpoints
  - shared workspace packages: @repo/api-client (peers react + @tanstack/react-query, which this app supplies)
  - native: expo-secure-store (OS Keychain/Keystore) for the refresh token; a development build or EAS for any device/native run
exposes:
  - app: an Expo-managed React Native app (Expo Router) materialized at apps/mobile/, wired to the standard justfile targets
  - its co-located doc fragment: docs/fragment.md
versions-pinned-to: references/compatibility-matrix.md
last-verified: 2026-07-23
provenance: manual
-->

# Mobile — Expo template

An Expo-managed React Native app (Expo Router, file-based navigation) that scaffolding composes into a monorepo at `apps/mobile/`. It is the mobile consumer of the shared `@repo/api-client` in **bearer mode**, and it ships the smallest end-to-end proof of authenticated access: a login screen, a SecureStore-backed auth context, and a protected landing that calls `/auth/me` and renders the principal + roles. Lives at `templates/mobile/expo/` in this repo; scaffolding materializes it into `<project>/apps/mobile/` (the pnpm-workspace `apps/*` glob — no workspace-file edit needed). Everything here is **subordinate to the project's existing conventions** — when a scaffolded project has already diverged, the project wins.

## Contents
- Composition contract
- What it scaffolds
- Auth (bearer mode)
- Configuration & env
- Wiring into the justfile
- Verification boundary (hermetic vs documented-manual)
- Version pins

## Composition contract

**NEEDS**
- **Env vars** — `EXPO_PUBLIC_API_BASE_URL`: the backend origin the app talks to, read at app entry (`app/_layout.tsx`) as `process.env.EXPO_PUBLIC_API_BASE_URL` and passed to `configureApiClient({ baseUrl })`. Expo inlines only `EXPO_PUBLIC_*` vars into the bundle, at **build** time — this is **not a secret** (it ships in the binary); never put a real secret in an `EXPO_PUBLIC_*` var.
- **Upstream API contract** — `@repo/api-client`, the generated typed React Query client, configured in **bearer mode explicitly** (`mode: "bearer"` — the backend's own default is now server-side sessions for browser clients, so this app must opt out rather than rely on an omitted flag). It consumes the backend's `/auth/login`, `/auth/refresh`, `/auth/logout`, and `/auth/me` operations (the auth wiring's mobile half).
- **Shared workspace packages** — `@repo/api-client` (`workspace:*`). This app supplies `react` and `@tanstack/react-query` itself, satisfying that package's peer dependencies.
- **Native** — `expo-secure-store` (iOS Keychain / Android Keystore) for the refresh token; a development build or EAS build for any run on a device/simulator (the stock Expo Go client is fine for the pure-JS layer, but this app's native module set is device-build territory once composed into a real project).

**EXPOSES**
- **App** — an Expo-managed RN app at `apps/mobile/`, wired to the root `justfile`'s `test`/`lint`/`typecheck`/`build` targets via its own `package.json` scripts.
- **Its co-located doc fragment** — `docs/fragment.md`, aggregated into the root README's Setup / Secrets / Maintenance sections by `just docs-generate`.

## What it scaffolds
- `app/` — Expo Router routes: the root `_layout.tsx` (providers + api-client config + auth gate), a public `(auth)` group (`login.tsx`), and a protected `(app)` group (`index.tsx`, the landing).
- `src/auth/` — the auth context: a framework-free `authEngine` (the tested logic), a React `AuthProvider` + `useAuth` hook, and a `secureStore` seam over `expo-secure-store`.
- `src/auth/authEngine.test.ts` — the hermetic vitest suite (fake SecureStore + stubbed client).
- `app.json`, `tsconfig.json`, `eslint.config.mjs`, `.gitignore`, `package.json` — managed-workflow config, no committed `android/`/`ios/`.

## Auth (bearer mode)
This app implements the **bearer half** of `references/wiring/auth-end-to-end.md` verbatim:
- **Access token** in memory; **refresh token** in `expo-secure-store` (never AsyncStorage, never a cookie).
- Every authorized request attaches `Authorization: Bearer <access>`.
- **Silent refresh on 401** via the body-path `/auth/refresh` (no cookie, no CSRF), with a **single-flight** guard and **one** retry.
- Refresh returns a **new** refresh token every time → SecureStore is **overwritten immediately** (rotation / reuse-detection).
- A **refresh-401 is terminal** → clear SecureStore + memory → redirect to login.
- **Logout** POSTs the refresh token in the body, then **unconditionally** clears SecureStore + memory (idempotent).
- **AppState → active** triggers a proactive refresh when the access token is near expiry.
- `configureApiClient({ baseUrl })` runs once at entry in bearer mode; **cookie mode is never enabled on native** (the mutator's cookie/CSRF seam is inert without a `document` anyway).

See `references/mobile/react-native.md` ("Why bearer + SecureStore") and `references/mobile/navigation.md` (the auth-gate pattern) for the reasoning.

## Configuration & env
Set `EXPO_PUBLIC_API_BASE_URL` in the app's environment (a `.env` for local dev, EAS env vars for builds). On a physical device, `localhost` is the device — use your machine's LAN IP or a tunnel. Unset resolves to `""` (same-origin relative URLs), which is rarely what a device wants — set it explicitly for real runs.

## Wiring into the justfile
The app's `package.json` defines `typecheck` (`tsc --noEmit`), `lint` (`eslint .`), `test` (`vitest run`), and `build` (`expo export`), so the root `just typecheck`/`lint`/`test`/`build` targets pick them up through `pnpm -r --if-present`. `just dev` runs `expo start` via the parallel dev script. The block invents no task surface of its own.

## Verification boundary (hermetic vs documented-manual)
- **Hermetic** (run in CI / in-sandbox): `pnpm install`, `tsc --noEmit`, `eslint`, and **`vitest` on the auth-engine logic** (fake SecureStore + stubbed client — asserts login stores the token, Bearer is attached, a 401 triggers exactly one refresh + retry, rotation overwrites storage, a refresh-401 clears + logs out, and logout clears storage).
- **Documented-manual** (NOT run here — no simulator/toolchain in-sandbox): `npx expo prebuild`, `npx expo run:ios`/`run:android`, EAS builds, and live auth against a running backend. Verify these on a real dev machine before shipping.

### Recorded in-sandbox results (Stage 8 authoring)
Run at authoring time (2026-07-23) against the pinned versions:

| Check | Tool | Result |
| --- | --- | --- |
| `vitest run` (auth-engine suite) | vitest 4.1.10 | **12/12 pass** — every asserted behavior (login stores token / Bearer attached / 401→one refresh+retry / single-flight / rotation overwrites storage / refresh-401→clear+logout / logout idempotent / bootstrap / proactive refresh). |
| `tsc --noEmit` (auth engine + test) | TypeScript **6.0** (matrix pin) | **clean** — `types: []`, `strict`, `verbatimModuleSyntax`. |
| `tsc --noEmit` (full app shell: `app/` + `src/`) | TypeScript 5.9.3 | **clean** against the real Expo SDK 57 / RN 0.86 / React 19.2 / `@tanstack/react-query` 5.101 types (5.9 used only because `expo/tsconfig.base` resolves most cleanly on it; the engine layer is independently proven on TS 6.0 above). The generated `@repo/api-client` surface was supplied by a faithful `.d.ts` shim mirroring the template's `src/generated/endpoints/{auth,admin}.ts` signatures, since the workspace package isn't materialized in-sandbox. |
| `eslint` (`app/` + `src/`) | ESLint 10.7.0 + typescript-eslint 8.65.0 | **clean** (0 problems). |
| dependency resolution | npm (551 packages) | The pinned Expo/RN/React/react-query set **installs and resolves** in-sandbox. |

**Not run in-sandbox (flagged, needs verification):** a full **`pnpm install` of a materialized monorepo** under the kit's `minimumReleaseAge: 1440` gate (the deps were resolved via npm to obtain real types; the pnpm-workspace-level install with the supply-chain window and any native postinstall was not exercised), and everything in the documented-manual list above (`expo prebuild` / device / simulator / EAS / live backend auth).

## Version pins
All versions are governed by `references/compatibility-matrix.md`'s Mobile + Frontend/web + Kit-wide sections — this block does not restate them. The Expo-SDK-governed packages (`expo`, `expo-router`, `expo-secure-store`, `react-native`, `react-native-screens`, `react-native-safe-area-context`, `react`) are resolved via `npx expo install` from Expo SDK 57 (`expo@57.0.7`'s `bundledNativeModules.json`); `typescript`, `eslint`, `typescript-eslint`, `prettier`, and `vitest` follow the kit-wide pins. Never hand-bump an SDK-governed package off the SDK — upgrade the SDK.
