<!--
wiring: mobile-backend
covers: Expo mobile app (templates/mobile/expo) <-> backend auth/account endpoints, bearer/SecureStore mode specifics
last-verified: 2026-07-23
provenance: manual
versions-pinned-to: references/compatibility-matrix.md
sources:
  - https://docs.expo.dev/versions/latest/sdk/securestore/
  - https://docs.expo.dev/guides/linking/
  - https://docs.expo.dev/router/reference/authentication/
  - references/mobile/react-native.md
  - references/mobile/navigation.md
  - references/wiring/auth-end-to-end.md
-->

# Mobile, backend-specific

**Everything about how `apps/mobile` (the Expo block) talks to the backend that's *specific* to being a native app** — bearer/SecureStore auth (as opposed to web's session-cookie default), the single-flight refresh engine, deep links for the account-lifecycle endpoints, and where push notifications would slot in. `references/wiring/auth-end-to-end.md` covers the shared backend contract and the web-vs-mobile mode split in full; this doc goes one level deeper into the mobile side only. Subordinate to a project's existing conventions where one already exists.

## Contents
- Bearer mode, restated for this doc's scope
- The `expo-secure-store` refresh-token seam
- Single-flight refresh, restated
- Deep links: verify-email and password-reset
- Push notifications: not yet built
- Wiring checklist
- Related canon

## Bearer mode, restated for this doc's scope
`templates/mobile/expo/` (materialized to `apps/mobile/`) configures `@repo/api-client` in **bearer mode, explicitly** — the *only* mode ever enabled on native:

```ts
// apps/mobile/app/_layout.tsx
configureApiClient({ baseUrl: process.env.EXPO_PUBLIC_API_BASE_URL ?? "", mode: "bearer" });
```

`mode: "bearer"` must be passed, not omitted — the backend's own default is now a server-side session (the browser posture), so an unconfigured client would silently be handed a session cookie it cannot properly store instead of the token pair `authEngine.ts` expects. Session/cookie mode is never enabled on native. There is no browser, so there is no ambient-cookie CSRF class to defend against (`references/wiring/auth-end-to-end.md`'s "Where CSRF applies" covers why bearer mode needs none) — cookies would only add friction native HTTP clients handle poorly, for zero security gain. The access token stays in memory (in `src/auth/authEngine.ts`'s state); the refresh token goes to a real OS-backed secret store instead of a cookie.

## The `expo-secure-store` refresh-token seam
`templates/mobile/expo/src/auth/secureStore.ts` is the **only** place `expo-secure-store` is touched — it implements the auth engine's `TokenStorage` interface (`get`/`set`/`clear`) so `authEngine.ts` itself stays framework-free and unit-testable against a fake storage (`authEngine.test.ts`). Two deliberate hardening choices in `secureStore.ts`'s `SecureStoreOptions`:

- `keychainAccessible: SecureStore.WHEN_UNLOCKED_THIS_DEVICE_ONLY` — the refresh token is only readable while the device is unlocked, **and** it's marked device-only so it's never synced to iCloud Keychain (a stronger default than the library's own default accessibility level).
- The refresh token is stored under key `auth.refresh_token`; it is **never** written to `AsyncStorage` and never sent as a cookie — SecureStore (iOS Keychain / Android Keystore) is the one place the long-lived credential lives on-device.

## Single-flight refresh, restated
`templates/mobile/expo/src/auth/authEngine.ts`'s `authorizedRequest` is the engine's core: it attaches `Authorization: Bearer <access>` to a protected call and, on a 401, runs a **single-flight** refresh (concurrent callers share one in-flight refresh rather than each firing their own) followed by **exactly one** retry of the original call. Refresh itself carries the refresh token in the request **body** (`POST /auth/refresh`, no cookie, no CSRF header — the bearer-mode half of `references/wiring/auth-end-to-end.md`'s "Login → refresh → logout" section). Every successful refresh returns a **new** refresh token, and SecureStore is overwritten immediately (rotation / reuse-detection, mode-independent on the backend). A refresh that does **not** return 200 is terminal: the engine clears SecureStore and memory and settles to `unauthenticated`, which the React layer (`AuthProvider`) turns into a redirect to the login screen. `maybeProactiveRefresh()` is the `AppState -> active` hook: when the app resumes from background, it refreshes proactively if the access token is near expiry, so the first post-resume request doesn't have to eat a synchronous 401-then-retry round trip.

## Deep links: verify-email and password-reset
The backend's account-lifecycle endpoints are single-use-token flows built for exactly this pattern — a link (web or deep) that carries a token, landing on a screen that submits it:

- `POST /auth/verify-email` — body `{ token }`, 204 on success (`templates/backend/fastapi/app/api/routers/auth.py`).
- `POST /auth/request-password-reset` — body `{ email }`, always 202 with an empty body regardless of whether the email has an account (anti-enumeration — mirrors `AuthService.login`'s generic `InvalidCredentials`).
- `POST /auth/reset-password` — body `{ token, new_password }`, 204 on success; revokes **every** refresh-token family the account has (all devices/sessions logged out, not just the requester's), and lifts any lockout.

All three reject an unknown/expired/already-used/wrong-purpose token as a generic 401 `unauthenticated` (`InvalidSingleUseToken`) — deliberately indistinguishable from any other auth failure, so a client can't probe token validity.

`templates/mobile/expo/app.json` already declares the deep-link scheme Expo Router requires for any linking to work at all (`"scheme": "mobile"`, with an inline reminder to rename it to the app's real scheme before shipping) and the `expo-router` plugin. **What is not yet built**: this template ships no route/screen that receives an incoming `mobile://verify-email?token=...` or `mobile://reset-password?token=...` link, parses the token, and calls the corresponding endpoint — the `(auth)` route group currently only has `login.tsx`. Wiring this in a real project means:
1. Adding a route under `app/(auth)/` (e.g. `verify-email.tsx`, `reset-password.tsx`) that reads the incoming link's query param (Expo Router exposes it as a route param on the matching screen — see `references/mobile/navigation.md`).
2. Calling the matching generated hook from the `auth` tag of `@repo/api-client` (`references/wiring/api-client-generation.md`) with the token.
3. For password reset, following a successful `reset-password` call with a forced re-login (every session was just revoked server-side, including the current one).

The web side of the same flow is unbuilt too — this is a real backend contract with no consuming screen yet on either client, not a mobile-specific gap.

## Push notifications: not yet built
`references/mobile/native-modules.md` lists `expo-notifications` alongside `expo-camera`/`expo-image-picker` as an example of a first-party, SDK-governed Expo module (install via `npx expo install expo-notifications`, never `pnpm add`) — but no push-notification code, device-token registration endpoint, or recipe exists in this kit yet. `templates/mobile/expo`'s only native module today is `expo-secure-store`. A future push-notification recipe would need, at minimum: a backend endpoint to register/deregister a device's push token against the authenticated principal (an `authorizedRequest`-wrapped call, same as any other protected mobile call), and the Expo-side `expo-notifications` permission/token flow. Until that recipe exists, treat push as unimplemented rather than assuming a wiring path this doc could point at.

## Wiring checklist
1. **Backend** — `/auth/*` (login/refresh/logout/me) plus the account-lifecycle trio (verify-email/request-password-reset/reset-password) exposed and generating single-use tokens (`templates/backend/fastapi/app/models/single_use_token.py` or the Django equivalent).
2. **Mobile** — `configureApiClient({ baseUrl, mode: "bearer" })` at `app/_layout.tsx`, `mode` passed EXPLICITLY; `EXPO_PUBLIC_API_BASE_URL` set per environment (LAN IP or tunnel for a physical device — `localhost` on-device means the device itself).
3. **Storage** — confirm `secureStore.ts`'s `TokenStorage` is the only place the refresh token is written; never persist it via `AsyncStorage`.
4. **Deep links** — set `app.json`'s `scheme` to the project's real scheme before any build that needs linking to work; add the verify/reset screens per "Deep links" above if the project needs those flows on mobile.
5. Run `templates/mobile/expo/src/auth/authEngine.test.ts` after touching the engine — it's the hermetic proof of login/401-refresh-retry/rotation/terminal-refresh/logout behavior.

## Related canon
- `references/wiring/auth-end-to-end.md` — the full backend auth contract, including the bearer-mode lifecycle this doc only restates the mobile-specific parts of.
- `templates/mobile/expo/README.md` — the block's own composition contract and verification boundary (hermetic vs documented-manual).
- `references/mobile/react-native.md` — "Why bearer + SecureStore."
- `references/mobile/navigation.md` — the Expo Router auth-gate pattern and `scheme` configuration.
- `references/mobile/native-modules.md` — the SDK-governed vs config-plugin module split (where `expo-notifications` is currently just an example, not a built integration).
- `references/wiring/api-client-generation.md` — how mobile consumes the same generated `auth` tag as web.
