<!--
library: react-native
versions-covered: "0.86 (via Expo SDK 57)"
last-verified: 2026-07-23
provenance: manual
versions-pinned-to: references/compatibility-matrix.md
sources:
  - https://reactnative.dev/docs/getting-started
  - https://reactnative.dev/docs/appstate
  - https://docs.expo.dev/versions/latest/sdk/securestore/
  - https://reactnative.dev/architecture/landing-page
  - references/wiring/auth-end-to-end.md
-->

# React Native conventions

Granular guidance for writing React Native (RN) app code inside an Expo-managed project. Read this after detecting RN (an `expo`/`react-native` dependency). RN is React with a different host: the same component model and hooks, but the primitives, the storage story, and the app lifecycle differ from the web. Everything here is subordinate to the project's existing conventions. Pairs with `expo.md`, `navigation.md`, and `native-modules.md`, and with the frontend skill's `react.md` for the React fundamentals that carry over unchanged.

## Contents
- Version check (do this first)
- What carries over from React, what doesn't
- Core primitives
- Storage: memory vs SecureStore vs AsyncStorage
- Why bearer + SecureStore on native (never cookies)
- AppState and the lifecycle
- Networking

## Version check (do this first)
RN's version is pinned **indirectly through the Expo SDK** — read `expo` in `package.json`, not `react-native` directly, and don't bump `react-native` on its own. This kit is RN **0.86** via Expo SDK **57**, which ships React **19.2.x** (the same React major the web side is on, so hooks/idioms match `references/frontend/react.md`). The New Architecture (Fabric/TurboModules) is the default in this line — write against it; don't reach for legacy-bridge patterns.

## What carries over from React, what doesn't
Carries over: components, `useState`/`useEffect`/`useContext`/`useReducer`, context providers, the rules of hooks, controlled inputs, and — because `@repo/api-client` is React Query hooks — the exact same data-fetching model as the web app. Does **not** carry over: there is no DOM. No `<div>`/`<span>`/`<button>`, no `className`, no CSS files, no `window`/`document`/`localStorage`. Styling is JS objects via `StyleSheet.create` (Flexbox by default, `flexDirection: "column"`). Anything that branches on `typeof document`/`typeof window` is a **web** code path that no-ops on native — which is exactly why `@repo/api-client`'s cookie/CSRF seam is inert here (see below).

## Core primitives
- **`View`** ≈ a non-scrolling container (the `<div>` analog).
- **`Text`** — all text must be inside a `<Text>`; bare strings in a `<View>` throw.
- **`TextInput`** — controlled input (`value` + `onChangeText`); set `secureTextEntry` for passwords, `autoCapitalize="none"` + `keyboardType="email-address"` for email.
- **`Pressable`** — the touch primitive (prefer over the older `TouchableOpacity`); give every actionable control an `accessibilityRole` and an accessible label.
- **`ScrollView`** (small, static content) / **`FlatList`** (long/virtualized lists).
- **`ActivityIndicator`** for loading; render explicit loading/empty/error states exactly as on web — a spinner that never resolves is the native equivalent of a blank page.
- Accessibility is not optional: `accessibilityRole`, `accessibilityLabel`, `accessibilityState`, adequate contrast, and touch targets ≥ 44pt.

## Storage: memory vs SecureStore vs AsyncStorage
Three tiers, and the choice is a security decision, not a convenience one:
- **In-memory** (a module variable / React state) — ephemeral, cleared when the JS context tears down. The **access token** lives here and only here.
- **`expo-secure-store`** — OS-backed encrypted storage (iOS Keychain / Android Keystore). The **refresh token** lives here. Values are per-app, encrypted at rest, and not readable by other apps.
- **`AsyncStorage`** — a plain, unencrypted key-value store. Fine for non-sensitive UI state (last tab, theme). **Never** put a token or any secret in AsyncStorage — it's readable on a rooted/jailbroken device and by anything with filesystem access to the app sandbox.

**Rule:** access token → memory; refresh token → SecureStore; never a token in AsyncStorage.

## Why bearer + SecureStore on native (never cookies)
This is the mobile half of `references/wiring/auth-end-to-end.md`, and the reasoning is worth internalizing:
- A native app has a **real OS-backed secret store** (Keychain/Keystore) the app controls — unlike a browser, which has no secret store JS can own. So the refresh token goes in SecureStore, not a cookie.
- A native app has **no ambient-cookie problem**: there is no browser auto-attaching credentials to a forged cross-site request, so **CSRF does not exist as a class here**. Cookies would add friction (native HTTP clients handle them poorly) for zero security gain.
- Therefore native uses **bearer mode**: access token in memory, attached explicitly as `Authorization: Bearer <access>`; refresh token in SecureStore, sent in the request **body** on refresh/logout. No cookies, no `credentials`, no CSRF. `configureApiClient({ baseUrl, mode: "bearer" })` — `mode` passed EXPLICITLY, not omitted: the backend's own default is now a server-side session (the browser posture), so an unconfigured client would silently get the wrong credential shape. Session/cookie mode is never enabled on native.
- Refresh is **single-use with rotation + reuse detection**: every refresh returns a *new* refresh token; overwrite SecureStore immediately. A refresh that 401s is terminal (the family was revoked) → clear SecureStore + memory → redirect to login.

## AppState and the lifecycle
Unlike a web tab, a mobile app is routinely **backgrounded** for minutes to days and resumed, with its JS context often kept alive the whole time — so an access token held in memory can be long-expired by the time the user returns, and a screen mid-mount can fire a request with a stale token. Use RN's **`AppState`** to react to `active` transitions: subscribe with `AppState.addEventListener("change", handler)`, and on the `background`/`inactive` → `active` transition **proactively refresh** the access token if it's near expiry, so the first post-resume request already carries a fresh token instead of eating a 401 + silent-refresh round trip. This is a real lifecycle event with no web analog; the auth context owns the subscription and cleans it up on unmount.

## Networking
- **`fetch`** is the platform HTTP primitive (same API as web); `@repo/api-client`'s mutator uses it. Don't add axios — the kit's client is fetch-based by design.
- The API base URL comes from `EXPO_PUBLIC_API_BASE_URL` (see `expo.md`) — on a physical device `localhost` is the device, not your dev machine, so use the machine's LAN IP or a tunnel; that's a documented-manual dev detail, not something the template can bake in.
- Silent refresh on 401 is a **single-flight** concern: concurrent requests that all 401 must trigger **one** refresh, not N — guard it (a shared in-flight promise) and give each request **one** retry with the new token. The auth context owns this; screens just call hooks.
